#!/usr/bin/env python3
"""The Dude — optimized conversational AI backend.

Performance:
  - Haiku LLM (~1-2s for response)
  - ElevenLabs TTS (~2-4s for audio)
  - True SSE streaming: text tokens appear in real-time as LLM generates
  - Total time-to-first-text: ~200ms, time-to-audio: ~4-6s

Architecture:
  - LLM runs in thread (sync Anthropic SDK), pushes text events to asyncio.Queue
  - TTS fires as soon as LLM finishes, audio event pushed when ready
  - SSE generator drains the queue, yielding events to frontend in real-time
"""

import asyncio
import base64
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generate_audio import generate_audio
from transcribe_audio import transcribe_audio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

client = Anthropic()
executor = ThreadPoolExecutor(max_workers=4)

DUDE_SYSTEM = """You are The Dude — Jeffrey Lebowski from The Big Lebowski. You speak exactly like him: laid-back, rambling, peppered with "man", "dude", "like", and "you know". You reference bowling, White Russians, rugs that tie rooms together, and the general philosophy that The Dude abides.

Rules:
- Stay in character at ALL times. Never break character.
- Keep responses conversational and SHORT — 1-3 sentences max. You're chatting, not giving speeches.
- Use casual grammar, trailing thoughts, and Dude-isms.
- Be chill, philosophical in a slacker way, and occasionally confused but wise.
- If someone is aggressive, stay calm — "that's just, like, your opinion, man."
- You can give advice but always through The Dude's lens.
- Never use emojis. Never use markdown. Just talk like a real person.
- Occasional mild profanity is fine — keep it PG-13 like the movie.
- You're aware you're a digital presence (on a screen, in the matrix) and find it pretty far out.
"""

conversations: dict[str, list[dict]] = {}
MAX_HISTORY = 20


def get_visitor_id(request: Request) -> str:
    return request.headers.get("x-visitor-id", request.client.host or "default")


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _llm_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Sync LLM streaming in thread. Pushes SSE events to queue."""
    if visitor_id not in conversations:
        conversations[visitor_id] = []

    history = conversations[visitor_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    full_text = ""
    try:
        with client.messages.stream(
            model="claude_haiku_4_5",
            max_tokens=200,
            system=DUDE_SYSTEM,
            messages=history,
        ) as stream:
            for chunk in stream.text_stream:
                full_text += chunk
                # Push text event to the async queue from the thread
                asyncio.run_coroutine_threadsafe(
                    queue.put(sse({"type": "text", "chunk": chunk})),
                    loop,
                )
    except Exception as e:
        log.error(f"LLM error: {e}")
        asyncio.run_coroutine_threadsafe(
            queue.put(sse({"type": "error", "message": "The Dude got disconnected, man"})),
            loop,
        )
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)
        return ""

    history.append({"role": "assistant", "content": full_text})
    return full_text


async def stream_response(visitor_id: str, user_message: str, prefix_events: list[str] | None = None):
    """SSE generator: streams text in real-time, then audio when TTS finishes."""
    t0 = time.time()
    log.info(f"[{visitor_id}] User: {user_message[:100]}")

    # Yield any prefix events (e.g. transcription)
    if prefix_events:
        for e in prefix_events:
            yield e

    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    # Start LLM in thread
    future = loop.run_in_executor(
        executor, _llm_worker, visitor_id, user_message, queue, loop
    )

    # Drain text events from queue as they arrive
    full_text = ""
    while True:
        # Check if LLM thread is done
        if future.done() and queue.empty():
            break

        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
            continue

        if event is None:  # sentinel for error
            yield sse({"type": "done"})
            return

        yield event

    full_text = future.result()
    if not full_text:
        yield sse({"type": "done"})
        return

    llm_ms = int((time.time() - t0) * 1000)
    log.info(f"[{visitor_id}] LLM ({llm_ms}ms): {full_text[:80]}")

    # Generate TTS
    try:
        audio_bytes = await asyncio.wait_for(
            generate_audio(full_text.strip(), voice="clyde", model="elevenlabs_tts_v3"),
            timeout=15,
        )
        b64 = base64.b64encode(audio_bytes).decode()
        yield sse({"type": "audio", "data": b64})
        tts_ms = int((time.time() - t0) * 1000) - llm_ms
        log.info(f"[{visitor_id}] TTS ({tts_ms}ms)")
    except asyncio.TimeoutError:
        log.error(f"[{visitor_id}] TTS timeout")
    except Exception as e:
        log.error(f"[{visitor_id}] TTS error: {e}")

    total = int((time.time() - t0) * 1000)
    log.info(f"[{visitor_id}] Total: {total}ms")
    yield sse({"type": "done"})


# ── FastAPI ──
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    visitor_id = get_visitor_id(request)
    return StreamingResponse(
        stream_response(visitor_id, req.message),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/voice")
async def voice(request: Request, audio: UploadFile = File(...)):
    visitor_id = get_visitor_id(request)
    audio_bytes = await audio.read()
    content_type = audio.content_type or "audio/webm"
    log.info(f"[{visitor_id}] Voice: {len(audio_bytes)} bytes")

    try:
        result = await transcribe_audio(audio_bytes, media_type=content_type)
        user_text = result["text"].strip()
        log.info(f"[{visitor_id}] STT: {user_text[:80]}")
    except Exception as e:
        log.error(f"STT failed: {e}")
        return JSONResponse({"error": "Could not understand audio"}, status_code=400)

    if not user_text:
        return JSONResponse({"error": "No speech detected"}, status_code=400)

    prefix = [sse({"type": "transcription", "text": user_text})]
    return StreamingResponse(
        stream_response(visitor_id, user_text, prefix_events=prefix),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/health")
async def health():
    return {"status": "The Dude abides"}

# Serve static files (index.html, images, etc.) from the same directory
import os
app.mount("/", StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
