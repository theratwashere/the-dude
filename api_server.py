#!/usr/bin/env python3
"""The Dude — AI avatar backend powered by Perplexity Computer via Comet CDP.

Architecture:
  - Comet browser running Perplexity Computer on localhost (CDP port 9222)
  - The Dude sends prompts to Computer, polls for responses, streams via SSE
  - TTS via edge-tts (free) or macOS say (built-in) — no API keys needed
  - STT via browser-native Web Speech API (client-side, no server needed)
  - No API keys needed for LLM — Computer IS the brain
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from comet_bridge import CometBridge

# TTS engine — graceful degradation if no engine available
try:
    from tts_engine import generate_tts, HAS_TTS, get_audio_content_type
except ImportError:
    HAS_TTS = False
    logging.getLogger("the-dude").warning("TTS module not found")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("the-dude")

# ── CONFIGURATION ──
COMET_CDP_PORT = int(os.environ.get("COMET_CDP_PORT", "9222"))
DUDE_MODE = os.environ.get("DUDE_MODE", "chat")

# The Dude's persona — prepended to every prompt so Computer responds in character
DUDE_PERSONA = os.environ.get("DUDE_PERSONA", (
    "You are The Dude (from The Big Lebowski). Respond in character: "
    "laid-back, uses 'man' and 'dude' naturally, references bowling/White Russians "
    "occasionally, rambles a bit but gets to the point. Keep answers helpful and "
    "accurate — you have Computer's knowledge but deliver it in The Dude's voice. "
    "Keep responses concise (2-4 sentences for simple questions, more for complex ones). "
    "Don't break character or mention being an AI."
))

bridge = CometBridge(port=COMET_CDP_PORT)

MAX_MESSAGE_LEN = 2000

# Concurrency lock — only one prompt at a time to Perplexity
_prompt_lock = asyncio.Lock()

# Status messages shown while Computer is working
WORKING_MESSAGES = [
    "Computer's on it, man...",
    "Still thinking, man...",
    "Searching the web, man...",
    "Almost there, man...",
    "Working on it, dude...",
    "Computer's doing its thing, man...",
]


def sse(data: dict) -> str:
    """Format a dict as an SSE data line."""
    return f"data: {json.dumps(data)}\n\n"


def _build_prompt(user_message: str) -> str:
    """Build the prompt to send to Perplexity.

    NOTE: We do NOT inject the Dude persona here. Perplexity is a search
    engine — it works best with clean, short queries. The Dude persona is
    applied as a system-level instruction only when Perplexity supports it
    (sidecar), or the persona flavoring happens post-response via TTS voice.
    """
    return user_message


def _clean_response(text: str) -> str:
    """Clean Computer's response — strip source citations, tool-use noise, etc.

    Perplexity responses often include:
    - Source reference numbers like [1], [2], etc.
    - "No tools were needed..." boilerplate
    - Source attribution lines (zeitverschiebung, reddit, etc.)
    - "Reviewed X sources" / "X steps completed" artifacts
    - Markdown formatting that's fine to keep
    """
    if not text:
        return text

    # Remove source reference numbers [1], [2], etc.
    text = re.sub(r'\[\d+\]', '', text)

    # Remove "No tools were needed..." type boilerplate
    text = re.sub(r'No tools were needed[^.]*\.?\s*', '', text, flags=re.IGNORECASE)

    # Remove "Reviewed N sources" / "N steps completed" artifacts
    text = re.sub(r'Reviewed \d+ sources?\.?\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\d+ steps? completed\.?\s*', '', text, flags=re.IGNORECASE)

    # Remove standalone source names that appear at start of text
    # (e.g., "reddit vocabulary +1" that leak from Perplexity UI)
    text = re.sub(r'^(reddit|vocabulary|wikipedia|\+\d+)\s*', '', text,
                  flags=re.IGNORECASE | re.MULTILINE)

    # Remove "View All" / "Show more" UI artifacts
    text = re.sub(r'(View All|Show more|Ask a follow-up)\s*', '', text, flags=re.IGNORECASE)

    # Remove trailing source URLs that sometimes leak
    text = re.sub(r'\n\s*Sources?:?\s*\n.*$', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Remove null bytes (seen in logs)
    text = text.replace('\x00', '')

    # Collapse excessive whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {2,}', ' ', text)

    return text.strip()


async def stream_response(user_message: str, prefix_events: Optional[List[str]] = None):
    """SSE generator: sends status updates while Computer works, then text + audio."""
    t0 = time.time()
    log.info(f"User: {user_message[:100]}")

    if prefix_events:
        for e in prefix_events:
            yield e

    prompt = _build_prompt(user_message)

    # Track status for SSE updates
    status_idx = 0
    last_status_time = time.time()
    last_step = ""

    # Send initial thinking status
    yield sse({"type": "status", "message": "Computer's on it, man..."})

    # Only one prompt at a time — wait for the lock
    if _prompt_lock.locked():
        yield sse({"type": "status", "message": "Hold on, man, still working on the last thing..."})

    full_text = ""

    async with _prompt_lock:
        try:
            # Ensure connected and on clean page before sending
            try:
                await bridge.ensure_connected()
                await bridge._ensure_home_page()
            except ConnectionError as e:
                log.error(f"Connection error: {e}")
                yield sse({"type": "text", "chunk": str(e)})
                yield sse({"type": "done"})
                return

            # Type and submit the prompt
            try:
                from comet_bridge import (
                    JS_CHECK_INPUT, JS_FOCUS_INPUT,
                    JS_CHECK_SUBMITTED, JS_CLICK_SUBMIT,
                    JS_GET_STATUS, JS_EXTRACT_RESPONSE,
                )

                result = await bridge._clear_and_type(prompt)
                if not result or not result.get("success"):
                    yield sse({"type": "text", "chunk": "Couldn't type into Perplexity, man. Is the page loaded?"})
                    yield sse({"type": "done"})
                    return
                log.info(f"Typed prompt via {result.get('method')}")

                has_content = await bridge._evaluate(JS_CHECK_INPUT)
                log.info(f"Input has content: {has_content}")
                if not has_content:
                    yield sse({"type": "text", "chunk": "Typing failed, man. Try again."})
                    yield sse({"type": "done"})
                    return

                # Try clicking the Submit button directly first (more reliable)
                await bridge._evaluate(JS_FOCUS_INPUT)
                click_result = await bridge._evaluate(JS_CLICK_SUBMIT)
                log.info(f"Submit click result: {click_result}")
                await asyncio.sleep(1.0)

                submitted = await bridge._evaluate(JS_CHECK_SUBMITTED)
                log.info(f"Submit check (after click): {submitted}")
                if not submitted:
                    # Fallback: try Enter key
                    await bridge._evaluate(JS_FOCUS_INPUT)
                    await bridge._press_key("Enter", "Enter", 13)
                    await asyncio.sleep(0.5)
                    submitted = await bridge._evaluate(JS_CHECK_SUBMITTED)
                    log.info(f"Submit check (after Enter): {submitted}")
                    if not submitted:
                        # Last resort: click again
                        await bridge._evaluate(JS_CLICK_SUBMIT)
                        log.info("Sent second click as last resort")

            except Exception as e:
                log.error(f"Submit error: {e}")
                yield sse({"type": "text", "chunk": "The Dude got disconnected from Computer, man."})
                yield sse({"type": "done"})
                return

            # Wait for Perplexity to navigate to /search/ after submission.
            # IMPORTANT: During page navigation, the Runtime context is destroyed
            # and Runtime.evaluate hangs until the new page loads. Instead of
            # evaluating JS (which can block for CDP_TIMEOUT=30s per attempt),
            # we poll the CDP HTTP /json/list endpoint which returns tab URLs
            # without needing an active execution context.
            submit_wait = 0
            search_url_found = False
            while submit_wait < 15:
                await asyncio.sleep(1)
                submit_wait += 1
                try:
                    # Poll tab URLs via HTTP — doesn't depend on Runtime context
                    targets = await bridge._http_get("/json/list")
                    for t in targets:
                        url = t.get("url", "")
                        if "perplexity.ai" in url and ("/search/" in url or "/thread/" in url):
                            # Check if this is a FRESH search (not an old one)
                            # by looking for the query slug in the URL
                            slug = prompt[:30].lower().replace(" ", "-")
                            slug_words = prompt.lower().split()[:3]
                            url_lower = url.lower()
                            if any(w in url_lower for w in slug_words if len(w) > 2):
                                log.info(f"Post-submit URL [{submit_wait}s]: {url}")
                                log.info("Submission confirmed — on response page")
                                search_url_found = True
                                # Reconnect bridge to this search tab
                                await bridge.disconnect()
                                await bridge.connect()
                                await asyncio.sleep(1)
                                break
                    if search_url_found:
                        break
                    # Also try direct JS evaluation as a fast path (may work if
                    # navigation is instant and context is already available)
                    try:
                        await bridge._send_cdp("Runtime.enable")
                        current_url = await asyncio.wait_for(
                            bridge._evaluate("window.location.href"),
                            timeout=3,
                        )
                        if current_url and ("/search/" in current_url or "/thread/" in current_url):
                            log.info(f"Post-submit URL [{submit_wait}s]: {current_url}")
                            log.info("Submission confirmed — on response page (JS)")
                            search_url_found = True
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        pass  # Expected during navigation — HTTP poll handles it
                except Exception as e:
                    log.warning(f"Post-submit check error [{submit_wait}s]: {e}")
                    continue

            if not search_url_found:
                log.warning("Never saw /search/ URL — trying to proceed anyway")
                # Try to reconnect to whatever Perplexity tab exists
                try:
                    await bridge.disconnect()
                    await bridge.connect()
                except Exception as e:
                    log.error(f"Reconnect failed: {e}")

            # Ensure bridge is connected before polling
            try:
                await bridge.ensure_connected()
            except Exception as e:
                log.error(f"Bridge not connected for polling: {e}")
                full_text = "Lost connection to Computer, man."
                yield sse({"type": "text", "chunk": full_text})
                yield sse({"type": "done"})
                return

            # Poll for response, yielding status updates via SSE
            elapsed = 0.0
            poll_interval = 2.0
            timeout = 120.0
            idle_count = 0
            ever_saw_working = False
            consecutive_errors = 0

            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                try:
                    status = await asyncio.wait_for(
                        bridge._evaluate(JS_GET_STATUS),
                        timeout=8,
                    )
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    log.warning(f"Status poll error [{elapsed:.0f}s] (#{consecutive_errors}): {e}")
                    if consecutive_errors >= 3:
                        log.info("Too many poll errors, reconnecting bridge...")
                        try:
                            await bridge.disconnect()
                            await bridge.connect()
                            consecutive_errors = 0
                        except Exception as re_err:
                            log.error(f"Reconnect failed: {re_err}")
                    continue

                state = status.get("status", "idle") if isinstance(status, dict) else "idle"
                step = status.get("currentStep", "") if isinstance(status, dict) else ""
                log.info(f"Poll [{elapsed:.0f}s]: state={state} step={step[:60]}")

                # Send periodic status updates
                if state == "working":
                    idle_count = 0
                    ever_saw_working = True
                    if step and step != last_step:
                        last_step = step
                        yield sse({"type": "status", "message": f"{step}..."})
                    elif time.time() - last_status_time > 4:
                        msg = WORKING_MESSAGES[status_idx % len(WORKING_MESSAGES)]
                        status_idx += 1
                        last_status_time = time.time()
                        yield sse({"type": "status", "message": msg})

                elif state == "completed":
                    response = await bridge._evaluate(JS_EXTRACT_RESPONSE)
                    if response and len(str(response).strip()) > 0:
                        full_text = str(response).strip()
                    else:
                        await asyncio.sleep(1)
                        response = await bridge._evaluate(JS_EXTRACT_RESPONSE)
                        full_text = str(response).strip() if response else ""
                    if not full_text:
                        full_text = "(Computer finished but returned no text, man.)"
                    break

                elif state == "idle":
                    idle_count += 1
                    idle_patience = 10 if ever_saw_working else 30
                    if idle_count > 5:
                        response = await bridge._evaluate(JS_EXTRACT_RESPONSE)
                        if response and len(str(response).strip()) > 0:
                            full_text = str(response).strip()
                            break
                        if idle_count > idle_patience:
                            full_text = "(Computer didn't respond. Try again, man.)"
                            break

            if not full_text:
                full_text = "(Timed out waiting for Computer, man. That's a bummer.)"

        except Exception as e:
            log.error(f"Bridge error: {e}", exc_info=True)
            full_text = "The Dude got disconnected from Computer, man."

    # Clean the response
    full_text = _clean_response(full_text)

    # Stream the response text to frontend in chunks
    yield sse({"type": "status", "message": ""})
    chunk_size = 12
    words = full_text.split(" ")
    for i in range(0, len(words), chunk_size):
        chunk = " ".join(words[i:i + chunk_size])
        if i > 0:
            chunk = " " + chunk
        yield sse({"type": "text", "chunk": chunk})
        await asyncio.sleep(0.03)

    llm_ms = int((time.time() - t0) * 1000)
    log.info(f"Computer ({llm_ms}ms): {full_text[:80]}")

    # Generate TTS if available
    if HAS_TTS:
        try:
            audio_bytes = await asyncio.wait_for(
                generate_tts(full_text),
                timeout=30,
            )
            b64 = base64.b64encode(audio_bytes).decode()
            content_type = get_audio_content_type()
            yield sse({"type": "audio", "data": b64, "format": content_type})
            tts_ms = int((time.time() - t0) * 1000) - llm_ms
            log.info(f"TTS ({tts_ms}ms, {len(audio_bytes)} bytes)")
        except asyncio.TimeoutError:
            log.error("TTS timeout")
        except Exception as e:
            log.error(f"TTS error: {e}")

    total = int((time.time() - t0) * 1000)
    log.info(f"Total: {total}ms")
    yield sse({"type": "done"})


# ── FastAPI ──
app = FastAPI(title="The Dude", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    return StreamingResponse(
        stream_response(req.message.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    return {
        "status": "The Dude abides",
        "mode": DUDE_MODE,
        "tts": HAS_TTS,
    }


@app.get("/api/status")
async def status():
    """Check Comet connectivity."""
    try:
        connected = await bridge.is_connected()
        if connected:
            return {"status": "connected", "message": "Computer is online, man."}
        tab_url = await bridge.connect()
        return {"status": "connected", "message": f"Connected to {tab_url}"}
    except ConnectionError as e:
        return JSONResponse(
            {"status": "disconnected", "message": str(e)},
            status_code=503,
        )
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": f"Something went wrong, man: {e}"},
            status_code=500,
        )


# Serve static files (index.html, images, etc.) from the same directory
app.mount(
    "/",
    StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True),
    name="static",
)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
