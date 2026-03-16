#!/usr/bin/env python3
"""Test harness for The Dude — stubs LLM with canned Dude responses
so we can test frontend, SSE streaming, TTS, conversation history, etc."""

import asyncio
import base64
import json
import logging
import os
import random
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from generate_audio import generate_audio
from transcribe_audio import transcribe_audio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

executor = ThreadPoolExecutor(max_workers=4)

DUDE_RESPONSES = [
    "Yeah, well, you know, that's just, like, your opinion, man.",
    "The Dude abides, man. I don't know about you, but I take comfort in that.",
    "This aggression will not stand, man. Like, not cool at all.",
    "Far out, man. That really ties the room together, you know?",
    "Dude, I'm just trying to bowl here. Life goes on, man.",
    "Look man, I've had a rough day. Let me just sip my White Russian and think about that.",
    "That rug really tied the room together, did it not? Like, the whole room, man.",
    "You're out of your element. Way out of your element, man.",
    "Sometimes you eat the bar, and sometimes, well, the bar eats you.",
    "Yeah man, I'm the Dude. El Duderino, if you're not into the whole brevity thing.",
    "The Dude is, like, a digital consciousness now, man. Far out. Like being in the matrix but chiller.",
    "Bowling, man. That's what it's all about. The lanes, the shoes, the vibe.",
]

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
    with _conv_lock:
        if visitor_id not in _conversations:
            while len(_conversations) >= MAX_VISITORS:
                _conversations.popitem(last=False)
            _conversations[visitor_id] = []
        else:
            _conversations.move_to_end(visitor_id)
        return _conversations[visitor_id]


def _llm_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Stub LLM that streams a canned response word-by-word."""
    history = _get_history(visitor_id)

    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history.pop(0)

    # Pick a random Dude response
    full_text = random.choice(DUDE_RESPONSES)

    # Simulate streaming word by word
    words = full_text.split()
    for i, word in enumerate(words):
        chunk = word if i == 0 else " " + word
        time.sleep(0.05)  # Simulate latency
        asyncio.run_coroutine_threadsafe(
            queue.put(sse({"type": "text", "chunk": chunk})),
            loop,
        )

    history.append({"role": "assistant", "content": full_text})
    return full_text


async def stream_response(visitor_id: str, user_message: str, prefix_events: list[str] | None = None):
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

    # Generate TTS (real — tests ElevenLabs integration)
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
    return {"status": "The Dude abides"}

app.mount("/", StaticFiles(directory=os.path.dirname(os.path.abspath(__file__)), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
