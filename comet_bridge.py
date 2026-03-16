"""Comet Bridge — CDP interface to Perplexity Computer via Comet browser.

Connects to the locally-running Comet browser via Chrome DevTools Protocol,
finds the Perplexity tab (preferring the sidecar panel), sends prompts, and
extracts responses. Zero API keys needed — The Dude is just a voice/visual
frontend for Computer.
"""

import asyncio
import json
import logging
from typing import Optional, Union

import aiohttp
import websockets

log = logging.getLogger("comet-bridge")

# Selectors for Perplexity's input element (contenteditable primary, textarea fallback)
INPUT_SELECTORS = [
    '[contenteditable="true"]',
    'textarea[placeholder*="Ask"]',
    'textarea[placeholder*="Search"]',
    'textarea',
    'input[type="text"]',
]

# ── JavaScript Fragments ──
# These are evaluated in the Perplexity tab via CDP Runtime.evaluate

# Focus and FULLY clear the Perplexity input (works with Lexical editor)
# Lexical uses a contenteditable div with internal React state. We must:
# 1. Focus the element
# 2. Select ALL content (Cmd+A / Ctrl+A equivalent)
# 3. Delete it via execCommand or key events
# 4. Verify it's empty before returning
JS_FOCUS_AND_CLEAR = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el) {
        el.focus();
        el.click();
        // Method 1: selectAll via document.execCommand
        document.execCommand('selectAll', false, null);
        // Method 2: Range-based selectAll as backup
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        // Delete selected content
        document.execCommand('delete', false, null);
        // If still has content, try innerHTML clear (triggers React reconciliation)
        const remaining = el.innerText.trim();
        if (remaining.length > 1) {
            // Force clear via multiple approaches
            el.innerHTML = '';
            // Dispatch input event to notify React/Lexical of the change
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }
        return { success: true, method: 'contenteditable', cleared: el.innerText.trim().length <= 1 };
    }
    const textarea = document.querySelector('textarea');
    if (textarea) {
        textarea.focus();
        textarea.select();
        textarea.value = '';
        textarea.dispatchEvent(new Event('input', { bubbles: true }));
        return { success: true, method: 'textarea', cleared: true };
    }
    return { success: false };
})()
"""

# Check if text was typed into the input (ignore placeholder)
JS_CHECK_INPUT = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el) {
        const text = el.innerText.trim();
        return text.length > 0 && text !== String.fromCharCode(10);
    }
    const textarea = document.querySelector('textarea');
    if (textarea && textarea.value.trim().length > 0) return true;
    return false;
})()
"""

# Focus input element before pressing Enter
JS_FOCUS_INPUT = """
(() => {
    const el = document.querySelector('[contenteditable="true"]') ||
               document.querySelector('textarea');
    if (el) el.focus();
})()
"""

# Check submission status (input cleared or loading started)
JS_CHECK_SUBMITTED = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el && el.innerText.trim().length < 5) return true;
    const hasLoading = document.querySelector('[class*="animate"]') !== null;
    return hasLoading;
})()
"""

# Click submit button as fallback
JS_CLICK_SUBMIT = """
(() => {
    const selectors = [
        'button[aria-label*="Submit"]',
        'button[aria-label*="Send"]',
        'button[aria-label*="Ask"]',
        'button[type="submit"]',
    ];
    for (const sel of selectors) {
        const btn = document.querySelector(sel);
        if (btn && !btn.disabled && btn.offsetParent !== null) {
            btn.click();
            return true;
        }
    }
    const inputEl = document.querySelector('[contenteditable="true"]') ||
                    document.querySelector('textarea');
    if (inputEl) {
        const inputRect = inputEl.getBoundingClientRect();
        let parent = inputEl.parentElement;
        let candidates = [];
        for (let i = 0; i < 4 && parent; i++) {
            const btns = parent.querySelectorAll('button:not([disabled])');
            for (const btn of btns) {
                const btnRect = btn.getBoundingClientRect();
                const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
                if (ariaLabel.includes('search') || ariaLabel.includes('research') ||
                    ariaLabel.includes('labs') || ariaLabel.includes('learn') ||
                    ariaLabel.includes('attach') || ariaLabel.includes('voice')) continue;
                if (btn.querySelector('svg') && btn.offsetParent !== null &&
                    btnRect.left > inputRect.left && btnRect.width > 0) {
                    candidates.push({ btn, right: btnRect.right });
                }
            }
            parent = parent.parentElement;
        }
        if (candidates.length > 0) {
            candidates.sort((a, b) => b.right - a.right);
            candidates[0].btn.click();
            return true;
        }
    }
    return false;
})()
"""

# Poll agent status — is Computer still working?
# Perplexity can have persistent animate-spin elements (sidebar/UI chrome)
# that are NOT related to response generation. The stop button is the most
# reliable "working" signal.
JS_GET_STATUS = """
(() => {
    const body = document.body.innerText;

    // Check for an active stop/cancel button (most reliable "working" indicator)
    let hasActiveStopButton = false;
    for (const btn of document.querySelectorAll('button')) {
        const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
        const text = (btn.innerText || '').toLowerCase().trim();
        if ((ariaLabel.includes('stop') || ariaLabel.includes('cancel') ||
             text === 'stop' || text === 'cancel') &&
            btn.offsetParent !== null && !btn.disabled) {
            hasActiveStopButton = true;
            break;
        }
    }

    // Check for loading spinners ONLY within the main content area
    const mainArea = document.querySelector('main') || document.body;
    const mainSpinners = mainArea.querySelectorAll('[class*="animate-spin"]');
    let hasActiveSpinner = false;
    for (const el of mainSpinners) {
        if (!el.closest('nav, aside, header, footer') && el.offsetParent !== null) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 4 && rect.height > 4) {
                hasActiveSpinner = true;
                break;
            }
        }
    }

    const hasAskFollowUp = body.includes('Ask a follow-up');
    const hasProseContent = [...document.querySelectorAll('[class*="prose"]')].some(
        el => el.innerText.trim().length > 0 && !el.closest('nav, aside, header, footer, form')
    );
    const hasReviewedSources = /Reviewed \\d+ sources?/i.test(body);
    const hasStepsCompleted = /\\d+ steps? completed/i.test(body);
    const hasFinishedMarker = body.includes('Finished') && !hasActiveStopButton;
    const hasSourceCount = /\\d+ sources?/.test(body);

    const workingPatterns = [
        'Preparing to assist', 'Reviewing sources', 'Searching the web',
        'Clicking', 'Typing:', 'Navigating to', 'Analyzing'
    ];
    const hasWorkingText = workingPatterns.some(p => body.includes(p));

    // Priority ordering:
    // 1. Stop button = definitely working
    // 2. "Ask a follow-up" + prose content = completed (overrides spinners)
    // 3. Reviewed/Finished markers = completed
    // 4. Active working text = working
    // 5. Active content-area spinner = working
    // 6. Has prose content without working signals = completed
    // 7. Otherwise idle
    let status = 'idle';
    if (hasActiveStopButton) {
        status = 'working';
    } else if (hasAskFollowUp) {
        status = 'completed';
    } else if (hasStepsCompleted || hasFinishedMarker) {
        status = 'completed';
    } else if (hasReviewedSources && hasProseContent && !hasWorkingText) {
        status = 'completed';
    } else if (hasSourceCount && hasProseContent && !hasWorkingText && !hasActiveSpinner) {
        status = 'completed';
    } else if (hasWorkingText) {
        status = 'working';
    } else if (hasActiveSpinner) {
        status = 'working';
    } else if (hasProseContent) {
        status = 'completed';
    }

    let currentStep = '';
    const stepPatterns = [
        /Preparing to assist[^\\n]*/g, /Clicking[^\\n]*/g, /Typing:[^\\n]*/g,
        /Navigating[^\\n]*/g, /Reading[^\\n]*/g, /Searching[^\\n]*/g, /Found[^\\n]*/g
    ];
    const steps = [];
    for (const pattern of stepPatterns) {
        const matches = body.match(pattern);
        if (matches) steps.push(...matches.map(s => s.trim().substring(0, 100)));
    }
    if (steps.length > 0) currentStep = steps[steps.length - 1];

    return { status, currentStep, hasStopButton: hasActiveStopButton };
})()
"""

# Extract the response text from prose elements.
# Takes the LAST valid prose element (most recent response in a thread).
JS_EXTRACT_RESPONSE = """
(() => {
    const mainContent = document.querySelector('main') || document.body;
    const allProseEls = mainContent.querySelectorAll('[class*="prose"]');
    const validProseTexts = [];

    for (const el of allProseEls) {
        if (el.closest('nav, aside, header, footer, form')) continue;
        const text = el.innerText.trim();
        const isUIText = ['Library', 'Discover', 'Spaces', 'Finance', 'Account',
                          'Upgrade', 'Home', 'Search', 'Ask a follow-up',
                          'Sign in', 'Sign up', 'Continue with', 'Model'].some(
            ui => text.startsWith(ui)
        );
        if (isUIText) continue;
        if (text.length === 0) continue;
        const parent = el.parentElement;
        if (parent && parent.matches && parent.matches('[class*="prose"]')) continue;
        validProseTexts.push(text);
    }

    let response = '';
    if (validProseTexts.length > 0) {
        response = validProseTexts[validProseTexts.length - 1];
    }

    if (response) {
        response = response.replace(
            /View All|Show more|Ask a follow-up/gi, ''
        ).trim();
        response = response.replace(/^(reddit|vocabulary|wikipedia|\\+\\d+)\\s*/gim, '').trim();
        response = response.replace(/\\n{3,}/g, '\\n\\n').trim();
    }

    return response.substring(0, 8000);
})()
"""

# Maximum WebSocket message size (16MB)
WS_MAX_SIZE = 16 * 1024 * 1024

# CDP response timeout (seconds). Keep reasonably short so navigation-time
# polls don't block the event loop for ages.
CDP_TIMEOUT = 10

# Maximum reconnection attempts
MAX_RECONNECT_ATTEMPTS = 3


class CometBridge:
    """Bridge to Perplexity Computer via Comet browser's CDP interface."""

    def __init__(self, port: int = 9222):
        self.port = port
        self.ws = None
        self._msg_id = 0
        self._target_url = None  # Track which tab we're connected to
        self._is_sidecar = False  # Track if connected to sidecar tab

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _http_get(self, path: str) -> Union[dict, list]:
        """HTTP GET to CDP endpoint."""
        url = f"http://localhost:{self.port}{path}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return await resp.json()

    async def _send_cdp(self, method: str, params: Optional[dict] = None) -> dict:
        """Send a CDP JSON-RPC message and wait for its response.

        Handles WebSocket disconnection by attempting one reconnection.
        """
        for attempt in range(2):  # Try once, reconnect + retry if disconnected
            if not self.ws:
                if attempt == 0:
                    raise ConnectionError("Not connected to Comet. Call connect() first.")
                else:
                    break

            msg_id = self._next_id()
            msg = {"id": msg_id, "method": method}
            if params:
                msg["params"] = params

            try:
                await self.ws.send(json.dumps(msg))

                # Wait for matching response (skip events)
                while True:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=CDP_TIMEOUT)
                    data = json.loads(raw)
                    if data.get("id") == msg_id:
                        if "error" in data:
                            raise RuntimeError(f"CDP error: {data['error']}")
                        return data.get("result", {})

            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.ConnectionClosedError,
                    websockets.exceptions.ConnectionClosedOK,
                    ConnectionResetError,
                    BrokenPipeError) as e:
                log.warning(f"CDP WebSocket disconnected: {e}")
                self.ws = None
                if attempt == 0:
                    log.info("Attempting reconnection...")
                    try:
                        await self.connect()
                        continue  # Retry the CDP command
                    except Exception as re_err:
                        log.error(f"Reconnection failed: {re_err}")
                        raise ConnectionError(f"Lost connection to Comet: {e}") from e
                raise ConnectionError(f"Lost connection to Comet: {e}") from e

            except asyncio.TimeoutError:
                log.error(f"CDP command timed out: {method}")
                raise

        raise ConnectionError("Failed to send CDP command after reconnection attempt")

    async def _evaluate(self, expression: str) -> object:
        """Run Runtime.evaluate and return the value."""
        result = await self._send_cdp("Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
        })
        inner = result.get("result", {})
        if inner.get("subtype") == "error":
            raise RuntimeError(f"JS error: {inner.get('description', inner)}")
        return inner.get("value")

    async def _press_key(self, key: str, code: str = "", key_code: int = 0) -> None:
        """Dispatch a key press via CDP Input domain."""
        params = {"type": "keyDown", "key": key}
        if code:
            params["code"] = code
        if key_code:
            params["windowsVirtualKeyCode"] = key_code
        await self._send_cdp("Input.dispatchKeyEvent", params)
        params["type"] = "keyUp"
        await self._send_cdp("Input.dispatchKeyEvent", params)

    async def _insert_text(self, text: str) -> None:
        """Insert text via CDP Input.insertText — works with Lexical/React editors."""
        await self._send_cdp("Input.insertText", {"text": text})

    async def _clear_and_type(self, text: str) -> dict:
        """Focus the input, clear it, and type text using CDP.

        Returns dict with 'success' and 'method' keys.
        """
        result = await self._evaluate(JS_FOCUS_AND_CLEAR)
        if not result or not result.get("success"):
            return {"success": False}

        method = result.get("method", "unknown")
        cleared = result.get("cleared", False)
        await asyncio.sleep(0.1)

        if not cleared:
            # JS clear didn't fully work — try keyboard-based clear
            # Select all via Cmd+A (macOS) then delete
            await self._send_cdp("Input.dispatchKeyEvent", {
                "type": "keyDown", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "modifiers": 4,  # 4 = Meta (Cmd on Mac)
            })
            await self._send_cdp("Input.dispatchKeyEvent", {
                "type": "keyUp", "key": "a", "code": "KeyA",
                "windowsVirtualKeyCode": 65, "modifiers": 4,
            })
            await asyncio.sleep(0.1)
            await self._press_key("Backspace", "Backspace", 8)
            await asyncio.sleep(0.2)

            # Verify cleared
            check = await self._evaluate("""
                (() => {
                    const el = document.querySelector('[contenteditable="true"]');
                    if (el) return el.innerText.trim().length;
                    const ta = document.querySelector('textarea');
                    if (ta) return ta.value.trim().length;
                    return -1;
                })()
            """)
            if check and check > 1:
                log.warning(f"Input still has {check} chars after clear attempts")
                # Nuclear option: set innerHTML empty
                await self._evaluate("""
                    (() => {
                        const el = document.querySelector('[contenteditable="true"]');
                        if (el) {
                            el.innerHTML = '<p><br></p>';
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                        }
                    })()
                """)
                await asyncio.sleep(0.2)

        # Re-focus before typing
        await self._evaluate("""
            (() => {
                const el = document.querySelector('[contenteditable="true"]') || document.querySelector('textarea');
                if (el) { el.focus(); el.click(); }
            })()
        """)
        await asyncio.sleep(0.1)

        # Type the new text
        await self._insert_text(text)
        await asyncio.sleep(0.3)

        return {"success": True, "method": method}

    # ── Connection ──

    async def connect(self) -> str:
        """Connect to Comet's CDP interface. Find the Perplexity tab.

        Tab priority: sidecar > home > search/thread > any non-blank page.
        """
        try:
            targets = await self._http_get("/json/list")
        except Exception as e:
            raise ConnectionError(
                "The Dude can't find Computer, man. Make sure Comet is running "
                f"with --remote-debugging-port={self.port}."
            ) from e

        # Find the best Perplexity tab
        sidecar_target = None
        home_target = None
        search_target = None
        fallback_target = None

        for t in targets:
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            if "perplexity.ai" in url:
                if "sidecar" in url:
                    sidecar_target = t
                elif url.rstrip("/").endswith("perplexity.ai") or "perplexity.ai/?" in url:
                    home_target = t
                elif not search_target:
                    search_target = t
            elif url != "about:blank" and not url.startswith("chrome"):
                fallback_target = t

        target = sidecar_target or home_target or search_target or fallback_target
        if not target:
            raise ConnectionError(
                "Navigate to perplexity.ai in Comet first, man."
            )

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ConnectionError("No WebSocket URL for target tab.")

        # Close existing connection if any
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

        self.ws = await websockets.connect(ws_url, max_size=WS_MAX_SIZE)

        # Enable required CDP domains
        await self._send_cdp("Runtime.enable")

        tab_url = target.get("url", "unknown")
        self._target_url = tab_url
        self._is_sidecar = "sidecar" in tab_url
        log.info(f"Connected to Comet tab: {tab_url} (sidecar={self._is_sidecar})")
        return tab_url

    async def disconnect(self) -> None:
        """Clean up WebSocket connection."""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
            self._target_url = None
            self._is_sidecar = False
            log.info("Disconnected from Comet")

    async def is_connected(self) -> bool:
        """Check if the CDP connection is alive."""
        if not self.ws:
            return False
        try:
            val = await self._evaluate("1+1")
            return val == 2
        except Exception:
            self.ws = None
            return False

    async def ensure_connected(self) -> None:
        """Reconnect if needed, with retry logic."""
        if await self.is_connected():
            return

        last_error = None
        for attempt in range(MAX_RECONNECT_ATTEMPTS):
            try:
                await self.connect()
                return
            except ConnectionError as e:
                last_error = e
                if attempt < MAX_RECONNECT_ATTEMPTS - 1:
                    wait = (attempt + 1) * 2
                    log.warning(f"Connection attempt {attempt + 1} failed, retrying in {wait}s: {e}")
                    await asyncio.sleep(wait)

        raise last_error or ConnectionError("Failed to connect to Comet")

    async def _ensure_home_page(self) -> None:
        """Navigate to Perplexity home if we're on a search/thread page.

        Sidecar tabs don't use /search/ URLs — they stay on the sidecar URL.
        For sidecar, we just clear the input and we're good.
        """
        # Sidecar tabs don't navigate to /search/ — they have a different URL pattern
        if self._is_sidecar:
            log.debug("Sidecar tab — skipping home navigation (sidecar stays in-place)")
            return

        try:
            current_url = await self._evaluate("window.location.href")
            needs_nav = False
            if current_url:
                for pattern in ["/search/", "/thread/", "/t/"]:
                    if pattern in current_url:
                        needs_nav = True
                        break

            if needs_nav:
                log.info("On a search/thread page, navigating to home for fresh prompt...")
                await self._evaluate("window.location.href = 'https://www.perplexity.ai/'")
                # Wait for navigation and page load
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    try:
                        await self._send_cdp("Runtime.enable")
                        url = await self._evaluate("window.location.href")
                        ready = await self._evaluate("document.readyState")
                        if url and "/search/" not in url and "/thread/" not in url and ready == "complete":
                            await asyncio.sleep(1)  # Wait for React hydration
                            return
                    except Exception:
                        continue
                log.warning("Navigation to home page timed out")
        except Exception as e:
            log.warning(f"Could not check/navigate URL: {e}")

    async def _wait_for_response_page(self, timeout: float = 30) -> None:
        """Wait for Perplexity to navigate to a response page after submission.

        For sidecar tabs, the response appears in-place without URL navigation,
        so we just wait briefly for the response to start rendering.
        """
        if self._is_sidecar:
            # Sidecar doesn't navigate — just wait a moment for response to start
            await asyncio.sleep(2)
            return

        for i in range(int(timeout)):
            await asyncio.sleep(1)
            try:
                await self._send_cdp("Runtime.enable")
                url = await self._evaluate("window.location.href")
                if url and ("/search/" in url or "/thread/" in url):
                    log.info(f"On response page after {i+1}s: {url[:80]}")
                    await asyncio.sleep(1)
                    return
            except Exception as e:
                log.debug(f"Navigation wait [{i+1}s]: {e}")
                continue
        log.warning("Timed out waiting for navigation to response page")

    # ── Prompt Interaction ──

    async def send_prompt(self, text: str) -> str:
        """Send a prompt to Computer and wait for the response.

        Returns the response text from Perplexity.
        """
        await self.ensure_connected()
        await self._ensure_home_page()

        result = await self._clear_and_type(text)
        if not result or not result.get("success"):
            raise RuntimeError("Failed to type into Perplexity input. Is the page loaded?")
        log.info(f"Typed prompt ({result.get('method')}): {text[:60]}")

        has_content = await self._evaluate(JS_CHECK_INPUT)
        if not has_content:
            raise RuntimeError("Prompt text not found in input — typing may have failed")

        await self._evaluate(JS_FOCUS_INPUT)
        await self._press_key("Enter", "Enter", 13)
        await asyncio.sleep(0.5)

        submitted = await self._evaluate(JS_CHECK_SUBMITTED)
        if not submitted:
            await self._evaluate(JS_CLICK_SUBMIT)
            await asyncio.sleep(0.5)
            submitted = await self._evaluate(JS_CHECK_SUBMITTED)
            if not submitted:
                await self._press_key("Enter", "Enter", 13)

        log.info("Prompt submitted, waiting for response...")

        await self._wait_for_response_page()

        response = await self._poll_for_response()
        return response

    async def get_status(self) -> dict:
        """Check if Computer is still working on a response."""
        await self.ensure_connected()
        return await self._evaluate(JS_GET_STATUS)

    async def _poll_for_response(
        self,
        poll_interval: float = 2.0,
        timeout: float = 300,
        on_status=None,
    ) -> str:
        """Poll Perplexity until the response is complete."""
        elapsed = 0.0
        idle_after_submit = 0
        ever_saw_working = False

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await self._evaluate(JS_GET_STATUS)
            except Exception as e:
                log.warning(f"Status poll error: {e}")
                continue

            if on_status and asyncio.iscoroutinefunction(on_status):
                await on_status(status)
            elif on_status:
                on_status(status)

            state = status.get("status", "idle") if isinstance(status, dict) else "idle"
            log.debug(f"Poll [{elapsed:.0f}s]: state={state}")

            if state == "working":
                idle_after_submit = 0
                ever_saw_working = True

            elif state == "completed":
                response = await self._evaluate(JS_EXTRACT_RESPONSE)
                if response and len(str(response).strip()) > 0:
                    log.info(f"Response received ({len(str(response))} chars)")
                    return str(response).strip()
                await asyncio.sleep(1)
                response = await self._evaluate(JS_EXTRACT_RESPONSE)
                if response and len(str(response).strip()) > 0:
                    return str(response).strip()
                return "(Computer finished but returned no text)"

            elif state == "idle":
                idle_after_submit += 1
                idle_patience = 10 if ever_saw_working else 30
                if idle_after_submit > 5:
                    response = await self._evaluate(JS_EXTRACT_RESPONSE)
                    if response and len(str(response).strip()) > 0:
                        return str(response).strip()
                    if idle_after_submit > idle_patience:
                        return "(Computer didn't respond. Try again, man.)"

        return "(Timed out waiting for Computer, man. That's a bummer.)"

    async def send_prompt_streaming(self, text: str, status_callback=None):
        """Send a prompt and yield status updates while waiting.

        This is the main entry point used by the API server.
        """
        await self.ensure_connected()
        await self._ensure_home_page()

        result = await self._clear_and_type(text)
        if not result or not result.get("success"):
            raise RuntimeError("Failed to type into Perplexity input. Is the page loaded?")

        has_content = await self._evaluate(JS_CHECK_INPUT)
        if not has_content:
            raise RuntimeError("Prompt text not found in input — typing may have failed")

        await self._evaluate(JS_FOCUS_INPUT)
        await self._press_key("Enter", "Enter", 13)
        await asyncio.sleep(0.5)

        submitted = await self._evaluate(JS_CHECK_SUBMITTED)
        if not submitted:
            await self._evaluate(JS_CLICK_SUBMIT)
            await asyncio.sleep(0.5)
            submitted = await self._evaluate(JS_CHECK_SUBMITTED)
            if not submitted:
                await self._press_key("Enter", "Enter", 13)

        await self._wait_for_response_page()

        return await self._poll_for_response(on_status=status_callback)
