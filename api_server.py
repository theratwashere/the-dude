#!/usr/bin/env python3
"""The Dude — AI avatar backend powered by Perplexity Computer via Comet CDP.

Architecture:
  - Comet browser running Perplexity Computer on localhost (CDP port 9222)
  - The Dude sends prompts to Computer, polls for responses, streams via SSE
  - ElevenLabs TTS for audio
  - No API keys needed for LLM — Computer IS the brain
"""

import asyncio
import base64
import json
import logging
import os
import time
from typing import List, Optional

from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from comet_bridge import CometBridge

# Audio modules depend on internal pplx SDK — gracefully degrade if unavailable
try:
    from generate_audio import generate_audio
    HAS_TTS = True
except ImportError:
    HAS_TTS = False
    logging.getLogger("the-dude").warning("TTS unavailable (pplx SDK not installed)")

try:
    from transcribe_audio import transcribe_audio
    HAS_STT = True
except ImportError:
    HAS_STT = False
    logging.getLogger("the-dude").warning("STT unavailable (pplx SDK not installed)")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

# ── CONFIGURATION ──
COMET_CDP_PORT = int(os.environ.get("COMET_CDP_PORT", "9222"))
DUDE_PERSONA = os.environ.get("DUDE_PERSONA", "")
DUDE_MODE = os.environ.get("DUDE_MODE", "chat")

bridge = CometBridge(port=COMET_CDP_PORT)

MAX_MESSAGE_LEN = 2000

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
    return f"data: {json.dumps(data)}\n\n"


def _build_prompt(user_message: str) -> str:
    """Optionally prefix the user message with the Dude persona instruction."""
    if DUDE_PERSONA:
        return f"{DUDE_PERSONA}\n\n{user_message}"
    return user_message


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

    async def on_status(status: dict):
        nonlocal status_idx, last_status_time, last_step
        now = time.time()
        step = status.get("currentStep", "")
        state = status.get("status", "idle")

        if state == "working":
            if step and step != last_step:
                last_step = step
                # Don't yield from callback — we handle it in the polling loop

    # Send initial thinking status
    yield sse({"type": "status", "message": "Computer's on it, man..."})

    try:
        # Ensure connected and on home page before sending
        try:
            await bridge.ensure_connected()
            await bridge._ensure_home_page()
        except ConnectionError as e:
            yield sse({"type": "text", "chunk": str(e)})
            yield sse({"type": "done"})
            return

        # Type and submit the prompt
        try:
            from comet_bridge import JS_CHECK_INPUT, JS_FOCUS_INPUT
            from comet_bridge import JS_CHECK_SUBMITTED, JS_CLICK_SUBMIT
            from comet_bridge import JS_GET_STATUS, JS_EXTRACT_RESPONSE

            result = await bridge._clear_and_type(prompt)
            if not result or not result.get("success"):
                yield sse({"type": "text", "chunk": "Couldn't type into Perplexity, man. Is the page loaded?"})
                yield sse({"type": "done"})
                return

            has_content = await bridge._evaluate(JS_CHECK_INPUT)
            if not has_content:
                yield sse({"type": "text", "chunk": "Typing failed, man. Try again."})
                yield sse({"type": "done"})
                return

            await bridge._evaluate(JS_FOCUS_INPUT)
            await bridge._press_key("Enter", "Enter", 13)
            await asyncio.sleep(0.5)

            submitted = await bridge._evaluate(JS_CHECK_SUBMITTED)
            if not submitted:
                await bridge._evaluate(JS_CLICK_SUBMIT)
                await asyncio.sleep(0.5)
                submitted = await bridge._evaluate(JS_CHECK_SUBMITTED)
                if not submitted:
                    await bridge._press_key("Enter", "Enter", 13)

        except Exception as e:
            log.error(f"Submit error: {e}")
            yield sse({"type": "text", "chunk": "The Dude got disconnected from Computer, man."})
            yield sse({"type": "done"})
            return

        # Poll for response, yielding status updates via SSE
        elapsed = 0.0
        poll_interval = 2.0
        timeout = 300.0
        idle_count = 0
        ever_saw_working = False
        full_text = ""

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await bridge._evaluate(JS_GET_STATUS)
            except Exception as e:
                log.warning(f"Status poll error: {e}")
                continue

            state = status.get("status", "idle")
            step = status.get("currentStep", "")
            log.info(f"Poll [{elapsed:.0f}s]: state={state} step={step}")

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
                if response and len(response.strip()) > 5:
                    full_text = response.strip()
                else:
                    await asyncio.sleep(1)
                    response = await bridge._evaluate(JS_EXTRACT_RESPONSE)
                    full_text = response.strip() if response else ""
                if not full_text:
                    full_text = "(Computer finished but returned no text, man.)"
                break

            elif state == "idle":
                idle_count += 1
                # After submission, Perplexity takes several seconds to start
                # showing working indicators (especially after page navigation).
                # Be patient: wait up to 60s (30 polls) before giving up.
                # If we already saw "working", use a shorter threshold.
                idle_patience = 10 if ever_saw_working else 30
                if idle_count > 5:
                    response = await bridge._evaluate(JS_EXTRACT_RESPONSE)
                    if response and len(response.strip()) > 5:
                        full_text = response.strip()
                        break
                    if idle_count > idle_patience:
                        full_text = "(Computer didn't respond. Try again, man.)"
                        break

        if not full_text:
            full_text = "(Timed out waiting for Computer, man. That's a bummer.)"

    except Exception as e:
        log.error(f"Bridge error: {e}")
        full_text = "The Dude got disconnected from Computer, man."

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

    # Generate TTS (if pplx SDK available)
    if HAS_TTS:
        try:
            audio_bytes = await asyncio.wait_for(
                generate_audio(full_text.strip(), voice="clyde", model="elevenlabs_tts_v3"),
                timeout=15,
            )
            b64 = base64.b64encode(audio_bytes).decode()
            yield sse({"type": "audio", "data": b64})
            tts_ms = int((time.time() - t0) * 1000) - llm_ms
            log.info(f"TTS ({tts_ms}ms)")
        except asyncio.TimeoutError:
            log.error("TTS timeout")
        except Exception as e:
            log.error(f"TTS error: {e}")

    total = int((time.time() - t0) * 1000)
    log.info(f"Total: {total}ms")
    yield sse({"type": "done"})


# ── FastAPI ──
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    return StreamingResponse(
        stream_response(req.message.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/voice")
async def voice(request: Request, audio: UploadFile = File(...)):
    audio_bytes = await audio.read()
    if len(audio_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"error": "Audio too large, man"}, status_code=413)
    content_type = audio.content_type or "audio/webm"
    log.info(f"Voice: {len(audio_bytes)} bytes")

    if not HAS_STT:
        return JSONResponse({"error": "Voice not available — pplx SDK not installed"}, status_code=501)

    try:
        result = await transcribe_audio(audio_bytes, media_type=content_type)
        user_text = result["text"].strip()
        log.info(f"STT: {user_text[:80]}")
    except Exception as e:
        log.error(f"STT failed: {e}")
        return JSONResponse({"error": "Could not understand audio"}, status_code=400)

    if not user_text:
        return JSONResponse({"error": "No speech detected"}, status_code=400)

    prefix = [sse({"type": "transcription", "text": user_text})]
    return StreamingResponse(
        stream_response(user_text, prefix_events=prefix),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    return {"status": "The Dude abides", "mode": DUDE_MODE}


@app.get("/api/status")
async def status():
    """Check Comet connectivity."""
    try:
        connected = await bridge.is_connected()
        if connected:
            return {"status": "connected", "message": "Computer is online, man."}
        # Try to connect
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
app.mount("/", StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
