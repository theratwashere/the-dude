"""Comet Bridge — CDP interface to Perplexity Computer via Comet browser.

Connects to the locally-running Comet browser via Chrome DevTools Protocol,
finds the Perplexity tab, sends prompts, and extracts responses. Zero API keys
needed — The Dude is just a voice/visual frontend for Computer.
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

# JavaScript: focus and clear the Perplexity input (works with Lexical editor)
JS_FOCUS_AND_CLEAR = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el) {
        el.focus();
        el.click();
        // Select all content for deletion
        const range = document.createRange();
        range.selectNodeContents(el);
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(range);
        return { success: true, method: 'contenteditable' };
    }
    const textarea = document.querySelector('textarea');
    if (textarea) {
        textarea.focus();
        textarea.select();
        return { success: true, method: 'textarea' };
    }
    return { success: false };
})()
"""

# JavaScript: check if text was typed into the input (ignore placeholder)
JS_CHECK_INPUT = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el) {
        const text = el.innerText.trim();
        // Lexical empty state is just a newline; placeholder is in aria-placeholder
        return text.length > 0 && text !== '\n';
    }
    const textarea = document.querySelector('textarea');
    if (textarea && textarea.value.trim().length > 0) return true;
    return false;
})()
"""

# JavaScript: focus input element before pressing Enter
JS_FOCUS_INPUT = """
(() => {
    const el = document.querySelector('[contenteditable="true"]') ||
               document.querySelector('textarea');
    if (el) el.focus();
})()
"""

# JavaScript: check submission status (input cleared or loading started)
JS_CHECK_SUBMITTED = """
(() => {
    const el = document.querySelector('[contenteditable="true"]');
    if (el && el.innerText.trim().length < 5) return true;
    const hasLoading = document.querySelector('[class*="animate"]') !== null;
    return hasLoading;
})()
"""

# JavaScript: click submit button as fallback
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

# JavaScript: poll agent status — is Computer still working?
JS_GET_STATUS = """
(() => {
    const body = document.body.innerText;

    let hasActiveStopButton = false;
    for (const btn of document.querySelectorAll('button')) {
        const rect = btn.querySelector('rect');
        const ariaLabel = (btn.getAttribute('aria-label') || '').toLowerCase();
        if ((rect || ariaLabel.includes('stop')) &&
            btn.offsetParent !== null && !btn.disabled) {
            hasActiveStopButton = true;
            break;
        }
    }

    const hasLoadingSpinner = document.querySelector(
        '[class*="animate-spin"], [class*="animate-pulse"]'
    ) !== null;
    const hasStepsCompleted = /\\d+ steps? completed/i.test(body);
    const hasFinishedMarker = body.includes('Finished') && !hasActiveStopButton;
    const hasReviewedSources = /Reviewed \\d+ sources?/i.test(body);
    const hasAskFollowUp = body.includes('Ask a follow-up');
    const hasProseContent = [...document.querySelectorAll('[class*="prose"]')].some(
        el => el.innerText.trim().length > 0
    );

    const workingPatterns = [
        'Working', 'Searching', 'Reviewing sources', 'Preparing to assist',
        'Clicking', 'Typing:', 'Navigating to', 'Reading', 'Analyzing'
    ];
    const hasWorkingText = workingPatterns.some(p => body.includes(p));

    let status = 'idle';
    if (hasActiveStopButton || hasLoadingSpinner) {
        status = 'working';
    } else if (hasStepsCompleted || hasFinishedMarker) {
        status = 'completed';
    } else if (hasReviewedSources && !hasWorkingText) {
        status = 'completed';
    } else if (hasWorkingText) {
        status = 'working';
    } else if (hasAskFollowUp && hasProseContent && !hasActiveStopButton) {
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

# JavaScript: extract the response text from prose elements
JS_EXTRACT_RESPONSE = """
(() => {
    const mainContent = document.querySelector('main') || document.body;
    const allProseEls = mainContent.querySelectorAll('[class*="prose"]');
    const validProseTexts = [];

    for (const el of allProseEls) {
        if (el.closest('nav, aside, header, footer, form')) continue;
        const text = el.innerText.trim();
        const isUIText = ['Library', 'Discover', 'Spaces', 'Finance', 'Account',
                          'Upgrade', 'Home', 'Search', 'Ask a follow-up'].some(
            ui => text.startsWith(ui)
        );
        if (isUIText) continue;
        if (text.endsWith('?') && text.length < 100) continue;
        if (text.length > 5) validProseTexts.push(text);
    }

    let response = '';
    if (validProseTexts.length > 0) {
        response = validProseTexts[validProseTexts.length - 1];
    }

    if (response) {
        response = response.replace(
            /View All|Show more|Ask a follow-up|\\d+ sources?/gi, ''
        ).trim();
        response = response.replace(/\\s+/g, ' ').trim();
    }

    return response.substring(0, 8000);
})()
"""


class CometBridge:
    """Bridge to Perplexity Computer via Comet browser's CDP interface."""

    def __init__(self, port: int = 9222):
        self.port = port
        self.ws = None
        self._msg_id = 0

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
        """Send a CDP JSON-RPC message and wait for its response."""
        if not self.ws:
            raise ConnectionError("Not connected to Comet. Call connect() first.")
        msg_id = self._next_id()
        msg = {"id": msg_id, "method": method}
        if params:
            msg["params"] = params
        await self.ws.send(json.dumps(msg))

        # Wait for matching response (skip events)
        while True:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=30)
            data = json.loads(raw)
            if data.get("id") == msg_id:
                if "error" in data:
                    raise RuntimeError(f"CDP error: {data['error']}")
                return data.get("result", {})

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
        # Focus and select all existing content
        from comet_bridge import JS_FOCUS_AND_CLEAR
        result = await self._evaluate(JS_FOCUS_AND_CLEAR)
        if not result or not result.get("success"):
            return {"success": False}

        method = result.get("method", "unknown")
        await asyncio.sleep(0.1)

        # Delete selected content
        await self._press_key("Backspace", "Backspace", 8)
        await asyncio.sleep(0.2)

        # Type the new text
        await self._insert_text(text)
        await asyncio.sleep(0.3)

        return {"success": True, "method": method}

    # ── Connection ──

    async def connect(self) -> str:
        """Connect to Comet's CDP interface. Find the Perplexity tab."""
        try:
            targets = await self._http_get("/json/list")
        except Exception as e:
            raise ConnectionError(
                "The Dude can't find Computer, man. Make sure Comet is running "
                f"with --remote-debugging-port={self.port}."
            ) from e

        # Find the best Perplexity tab — prefer home page over search pages
        home_target = None
        search_target = None
        fallback_target = None
        for t in targets:
            if t.get("type") != "page":
                continue
            url = t.get("url", "")
            if "perplexity.ai" in url and "sidecar" not in url:
                # Prefer the home page (fresh input) over existing searches
                if url.rstrip("/").endswith("perplexity.ai") or "perplexity.ai/?" in url:
                    home_target = t
                elif not search_target:
                    search_target = t
            elif url != "about:blank" and not url.startswith("chrome"):
                fallback_target = t

        target = home_target or search_target or fallback_target
        if not target:
            raise ConnectionError(
                "Navigate to perplexity.ai in Comet first, man."
            )

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            raise ConnectionError("No WebSocket URL for target tab.")

        self.ws = await websockets.connect(ws_url, max_size=16 * 1024 * 1024)

        # Enable required CDP domains
        await self._send_cdp("Runtime.enable")
        # Note: Input domain doesn't need explicit enable in most CDP implementations

        tab_url = target.get("url", "unknown")
        log.info(f"Connected to Comet tab: {tab_url}")
        return tab_url

    async def disconnect(self) -> None:
        """Clean up WebSocket connection."""
        if self.ws:
            await self.ws.close()
            self.ws = None
            log.info("Disconnected from Comet")

    async def is_connected(self) -> bool:
        """Check if the CDP connection is alive."""
        if not self.ws:
            return False
        try:
            val = await self._evaluate("1+1")
            return val == 2
        except Exception:
            return False

    async def ensure_connected(self) -> None:
        """Reconnect if needed."""
        if not await self.is_connected():
            await self.connect()

    async def _ensure_home_page(self) -> None:
        """Navigate to Perplexity home if we're on a search/thread page."""
        try:
            current_url = await self._evaluate("window.location.href")
            if current_url and "/search/" in current_url:
                log.info("On a search page, navigating to home for fresh prompt...")
                await self._evaluate("window.location.href = 'https://www.perplexity.ai/'")
                # Wait for navigation and page load
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    try:
                        url = await self._evaluate("window.location.href")
                        ready = await self._evaluate("document.readyState")
                        if url and "/search/" not in url and ready == "complete":
                            # Wait a bit more for React to hydrate
                            await asyncio.sleep(1)
                            return
                    except Exception:
                        continue
                log.warning("Navigation to home page timed out")
        except Exception as e:
            log.warning(f"Could not check/navigate URL: {e}")

    # ── Prompt Interaction ──

    async def send_prompt(self, text: str) -> str:
        """Send a prompt to Computer and wait for the response.

        Returns the response text from Perplexity.
        """
        await self.ensure_connected()
        await self._ensure_home_page()

        # 1. Clear input and type text via CDP (works with Lexical editor)
        result = await self._clear_and_type(text)
        if not result or not result.get("success"):
            raise RuntimeError("Failed to type into Perplexity input. Is the page loaded?")
        log.info(f"Typed prompt ({result.get('method')}): {text[:60]}")

        # 2. Verify text is in the input
        has_content = await self._evaluate(JS_CHECK_INPUT)
        if not has_content:
            raise RuntimeError("Prompt text not found in input — typing may have failed")

        # 4. Submit: focus + Enter
        await self._evaluate(JS_FOCUS_INPUT)
        await self._press_key("Enter")
        await asyncio.sleep(0.5)

        # 5. Check if submission worked
        submitted = await self._evaluate(JS_CHECK_SUBMITTED)
        if not submitted:
            # Fallback: click submit button
            await self._evaluate(JS_CLICK_SUBMIT)
            await asyncio.sleep(0.5)
            submitted = await self._evaluate(JS_CHECK_SUBMITTED)
            if not submitted:
                # Last resort: Enter again
                await self._press_key("Enter")

        log.info("Prompt submitted, waiting for response...")

        # 6. Poll for completion
        response = await self._poll_for_response()
        return response

    async def get_status(self) -> dict:
        """Check if Computer is still working on a response.

        Returns dict with keys: status ('idle'|'working'|'completed'),
        currentStep (str), hasStopButton (bool).
        """
        await self.ensure_connected()
        return await self._evaluate(JS_GET_STATUS)

    async def _poll_for_response(
        self,
        poll_interval: float = 1.5,
        timeout: float = 300,
        on_status=None,
    ) -> str:
        """Poll Perplexity until the response is complete.

        Args:
            poll_interval: Seconds between polls.
            timeout: Max seconds to wait.
            on_status: Optional async callback(status_dict) called each poll.
        """
        elapsed = 0.0
        idle_after_submit = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await self._evaluate(JS_GET_STATUS)
            except Exception as e:
                log.warning(f"Status poll error: {e}")
                continue

            if on_status:
                await on_status(status)

            state = status.get("status", "idle")

            if state == "completed":
                # Extract and return the response
                response = await self._evaluate(JS_EXTRACT_RESPONSE)
                if response and len(response.strip()) > 5:
                    log.info(f"Response received ({len(response)} chars)")
                    return response.strip()
                # Completed but empty — wait a bit more
                await asyncio.sleep(1)
                response = await self._evaluate(JS_EXTRACT_RESPONSE)
                if response and len(response.strip()) > 5:
                    return response.strip()
                return "(Computer finished but returned no text)"

            if state == "idle":
                idle_after_submit += 1
                # Give it a few polls to start working
                if idle_after_submit > 4:
                    # Check if there's prose content anyway (quick response)
                    response = await self._evaluate(JS_EXTRACT_RESPONSE)
                    if response and len(response.strip()) > 5:
                        return response.strip()
                    if idle_after_submit > 8:
                        return "(Computer didn't respond. Try again, man.)"

        return "(Timed out waiting for Computer, man. That's a bummer.)"

    async def send_prompt_streaming(self, text: str, status_callback=None):
        """Send a prompt and yield status updates while waiting.

        This is the main entry point used by the API server.

        Args:
            text: The prompt to send.
            status_callback: Optional async callable(status_dict) for progress updates.

        Returns:
            The complete response text.
        """
        await self.ensure_connected()
        await self._ensure_home_page()

        # Clear and type via CDP
        result = await self._clear_and_type(text)
        if not result or not result.get("success"):
            raise RuntimeError("Failed to type into Perplexity input. Is the page loaded?")

        has_content = await self._evaluate(JS_CHECK_INPUT)
        if not has_content:
            raise RuntimeError("Prompt text not found in input — typing may have failed")

        # Submit via Enter
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

        # Poll with status callback
        return await self._poll_for_response(on_status=status_callback)
