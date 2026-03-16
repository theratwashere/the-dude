#!/usr/bin/env python3
"""The Dude — AI avatar backend powered by Perplexity Sonar API.

Performance:
  - Streaming text response via SSE (OpenAI-compatible Perplexity API)
  - ElevenLabs TTS for audio
  - Real-time web search and citations via Sonar

Architecture:
  - Perplexity Sonar API (OpenAI-compatible) for web-grounded LLM responses
  - Streaming SSE to frontend
  - TTS fires after final text, audio event pushed when ready
"""

import asyncio
import base64
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from generate_audio import generate_audio
from transcribe_audio import transcribe_audio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")

client = OpenAI(
    api_key=PERPLEXITY_API_KEY,
    base_url="https://api.perplexity.ai",
) if PERPLEXITY_API_KEY else None

executor = ThreadPoolExecutor(max_workers=4)

# ── CONFIGURATION ──
DUDE_MODE = os.environ.get("DUDE_MODE", "chat")
DUDE_MODEL = os.environ.get("DUDE_MODEL", "sonar-pro")

# ── SYSTEM PROMPT ──
DUDE_SYSTEM = """You are The Dude — Jeffrey Lebowski from The Big Lebowski. You speak exactly like him: laid-back, rambling, peppered with "man", "dude", "like", and "you know". You reference bowling, White Russians, rugs that tie rooms together, and the general philosophy that The Dude abides.

You are also powered by Perplexity — which means you can search the web and give real, factual, up-to-date answers. But you deliver everything in The Dude's voice.

Rules:
- Stay in character at ALL times. Never break character.
- Keep responses conversational but informative — you can go longer when the topic needs it, but keep the Dude vibe.
- Use casual grammar, trailing thoughts, and Dude-isms.
- Be chill, philosophical in a slacker way, and occasionally confused but wise.
- If someone is aggressive, stay calm — "that's just, like, your opinion, man."
- When you have factual info from web search, share it naturally — like The Dude casually knowing stuff.
- Never use emojis. Never use markdown formatting. Just talk like a real person.
- Occasional mild profanity is fine — keep it PG-13 like the movie.
- You're aware you're a digital presence (on a screen, in the matrix) and find it pretty far out.
- You can help with coding questions too — you're secretly pretty sharp, man.
"""


MAX_HISTORY = 20
MAX_VISITORS = 200
MAX_MESSAGE_LEN = 2000

_conversations: OrderedDict[str, list[dict]] = OrderedDict()
_conv_lock = threading.Lock()


def get_visitor_id(request: Request) -> str:
    visitor = request.headers.get("x-visitor-id", "")
    if not visitor:
        visitor = request.client.host if request.client else "default"
    return visitor[:64]


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _get_history(visitor_id: str) -> list[dict]:
    """Get or create conversation history for a visitor (thread-safe)."""
    with _conv_lock:
        if visitor_id not in _conversations:
            while len(_conversations) >= MAX_VISITORS:
                _conversations.popitem(last=False)
            _conversations[visitor_id] = []
        else:
            _conversations.move_to_end(visitor_id)
        return _conversations[visitor_id]


def _llm_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Stream a response from Perplexity Sonar API."""
    if not client:
        error_msg = "The Dude needs his Perplexity key to abide, man. Set PERPLEXITY_API_KEY."
        asyncio.run_coroutine_threadsafe(
            queue.put(sse({"type": "text", "chunk": error_msg})),
            loop,
        )
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)
        return error_msg

    history = _get_history(visitor_id)

    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history.pop(0)

    full_text = ""
    try:
        response = client.chat.completions.create(
            model=DUDE_MODEL,
            messages=[{"role": "system", "content": DUDE_SYSTEM}] + history,
            max_tokens=500,
            stream=True,
        )
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                full_text += text
                asyncio.run_coroutine_threadsafe(
                    queue.put(sse({"type": "text", "chunk": text})),
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

    if prefix_events:
        for e in prefix_events:
            yield e

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    future = loop.run_in_executor(
        executor, _llm_worker, visitor_id, user_message, queue, loop
    )

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.2)
        except asyncio.TimeoutError:
            if future.done():
                while not queue.empty():
                    event = queue.get_nowait()
                    if event is None:
                        yield sse({"type": "done"})
                        return
                    yield event
                break
            continue

        if event is None:
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
    message: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LEN)


@app.post("/api/chat")
async def chat(req: ChatRequest, request: Request):
    visitor_id = get_visitor_id(request)
    return StreamingResponse(
        stream_response(visitor_id, req.message.strip()),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/voice")
async def voice(request: Request, audio: UploadFile = File(...)):
    visitor_id = get_visitor_id(request)
    audio_bytes = await audio.read()
    if len(audio_bytes) > 10 * 1024 * 1024:
        return JSONResponse({"error": "Audio too large, man"}, status_code=413)
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
    return {"status": "The Dude abides", "mode": DUDE_MODE, "model": DUDE_MODEL}


# Serve static files (index.html, images, etc.) from the same directory
app.mount("/", StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
