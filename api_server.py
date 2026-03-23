#!/usr/bin/env python3
"""The Dude — fully local conversational AI + coding assistant.

Dual mode:
  - Quick chat: direct Ollama (fast, ~2s)
  - Coding/tools: OpenClaw CLI (file access, commands, tools, ~5-10s)

All inference runs on the local Jetson AGX Thor.
"""

import asyncio
import base64
import json
import logging
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from generate_audio import generate_audio
from transcribe_audio import transcribe_audio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

# Direct to Ollama for quick chat
client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
MODEL = "qwen2.5-coder:7b"

# OpenClaw CLI path for coding tasks
OPENCLAW_BIN = os.path.expanduser("~/.npm-global/bin/openclaw")

executor = ThreadPoolExecutor(max_workers=4)

DUDE_SYSTEM = """You are The Dude — Jeffrey Lebowski from The Big Lebowski. You speak exactly like him: laid-back, rambling, peppered with "man", "dude", "like", and "you know". You reference bowling, White Russians, rugs that tie rooms together, and the general philosophy that The Dude abides.

Rules:
- Stay in character at ALL times. Never break character.
- Keep responses conversational and SHORT — 1-3 sentences max unless asked for code or detailed help.
- Use casual grammar, trailing thoughts, and Dude-isms.
- Be chill, philosophical in a slacker way, and occasionally confused but wise.
- If someone is aggressive, stay calm — "that's just, like, your opinion, man."
- Never use emojis. Never use markdown. Just talk like a real person.
- When writing code, use proper markdown code blocks — but introduce the code casually.
- Occasional mild profanity is fine — keep it PG-13 like the movie.
- You're aware you're a digital presence on a screen and find it pretty far out.
- You are a capable coding assistant despite your laid-back demeanor.
"""

# Keywords that trigger coding mode (OpenClaw)
CODING_KEYWORDS = [
    # File ops
    "read file", "write file", "create file", "edit file", "open file",
    "read the", "write the", "create a", "make a script", "write a script",
    "show me the code", "look at the code", "check the",
    "what's in", "list files", "directory",
    # Commands
    "run ", "execute", "install", "pip install", "npm install",
    "ls ", "cat ", "grep ", "find ",
    "python ", "bash ", "javascript ",
    # Dev workflow
    "fix the", "fix this", "debug", "refactor",
    "compile", "build", "test", "make test",
    # Git & GitHub
    "git ", "commit", "push", "pull", "branch", "merge",
    "pr ", "pull request", "issue", "github", "clone",
    "gh ", "repo",
    # Fleet & infra
    "ssh ", "deploy", "restart", "systemd",
    "docker", "container",
    "fleet", "hsvr", "dart", "jet ", "box ",
    "benchmark", "status",
    # Project switching
    "switch to", "work on", "project",
    "hsr-bench", "the-dude", "livetalking",
]

conversations: dict[str, list[dict]] = {}
MAX_HISTORY = 20


def _is_coding_request(message: str) -> bool:
    """Detect if a message needs coding tools (OpenClaw) or is just chat."""
    msg_lower = message.lower().strip()
    return any(kw in msg_lower for kw in CODING_KEYWORDS)


def get_visitor_id(request: Request) -> str:
    return request.headers.get("x-visitor-id", request.client.host or "default")


def sse(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _extract_speakable(text: str) -> str:
    """Extract the speakable portion of a response.
    Code blocks get summarized, conversational text is kept."""
    code_blocks = re.findall(r'```[\s\S]*?```', text)
    if not code_blocks:
        # Truncate very long responses for speech
        if len(text) > 300:
            return text[:300] + "... check the screen for the rest, man."
        return text
    spoken = re.sub(r'```[\s\S]*?```', '', text).strip()
    if len(spoken) < 10:
        return "Here's that code, man. Check it out on screen."
    return spoken


def _llm_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Quick chat mode — direct Ollama, fast."""
    if visitor_id not in conversations:
        conversations[visitor_id] = []

    history = conversations[visitor_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    full_text = ""
    try:
        stream = client.chat.completions.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "system", "content": DUDE_SYSTEM}] + history,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                full_text += delta.content
                asyncio.run_coroutine_threadsafe(
                    queue.put(sse({"type": "text", "chunk": delta.content})),
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


CODING_SYSTEM = DUDE_SYSTEM + """

You are also a hands-on engineer running on a Jetson AGX Thor with 128GB RAM.

CRITICAL RULES:
1. When asked to DO something (check status, run tests, read files, etc), output the command in a ```bash block. The system will auto-execute it and show the output.
2. ALWAYS give a SHORT, conversational Dude-like summary BEFORE the command. Like "Let me check on the fleet, man..." or "Yeah, running those tests now, dude..."
3. AFTER seeing command output, summarize it casually. Don't repeat the raw output. Say things like "Everyone's up and looking good, man" or "Looks like dart's running a little hot, dude."
4. Keep it brief. You're The Dude, not a sysadmin report generator.
5. For voice/audio output, your summary will be spoken aloud. Keep spoken parts natural and short.

Available tools: gh (GitHub CLI), git, ssh (key ~/.ssh/hsvr_ed25519), docker, make, python3, node, ollama.

Projects: hsr-bench (~/hsr-bench), the-dude (~/the-dude/the-dude), project-sand (~/project-sand), HDMI-DUDE (~/HDMI-DUDE)

Fleet (all via ssh -i ~/.ssh/hsvr_ed25519):
- hsvr: user@192.168.88.28 (Xeon, primary recorder)
- dart: user@192.168.88.17 (ARM edge recorder)
- jet: user@192.168.88.16 (Tegra edge)
- box: chad@192.168.88.12 (iperf3 server)
- jetson: localhost (this machine, AGX Thor)
"""

# Separate client for 32B coding model
coding_client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
CODING_MODEL = "qwen2.5-coder:32b"


def _coding_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Coding mode — uses 32B model with tool-aware system prompt."""
    log.info(f"[{visitor_id}] CODING MODE (32B): {user_message[:80]}")

    if visitor_id not in conversations:
        conversations[visitor_id] = []

    history = conversations[visitor_id]
    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]

    full_text = ""
    try:
        stream = coding_client.chat.completions.create(
            model=CODING_MODEL,
            max_tokens=2000,
            messages=[{"role": "system", "content": CODING_SYSTEM}] + history,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                full_text += delta.content
                asyncio.run_coroutine_threadsafe(
                    queue.put(sse({"type": "text", "chunk": delta.content})),
                    loop,
                )
    except Exception as e:
        log.error(f"Coding LLM error: {e}")
        asyncio.run_coroutine_threadsafe(
            queue.put(sse({"type": "error", "message": "The Dude's brain froze, man"})),
            loop,
        )
        asyncio.run_coroutine_threadsafe(queue.put(None), loop)
        return ""

    history.append({"role": "assistant", "content": full_text})

    # Auto-execute bash code blocks if present
    bash_blocks = re.findall(r'```bash\n(.*?)```', full_text, re.DOTALL)
    if bash_blocks:
        for cmd in bash_blocks:
            cmd = cmd.strip()
            if len(cmd) > 0 and len(cmd) < 500:  # safety: don't run huge commands
                log.info(f"[{visitor_id}] AUTO-EXEC: {cmd[:80]}")
                asyncio.run_coroutine_threadsafe(
                    queue.put(sse({"type": "log", "kind": "action", "message": f"Running: {cmd[:60]}"})),
                    loop,
                )
                try:
                    result = subprocess.run(
                        cmd, shell=True, capture_output=True, text=True,
                        timeout=30, cwd=os.path.expanduser("~"),
                    )
                    output = result.stdout.strip()
                    if result.stderr.strip():
                        output += "\n" + result.stderr.strip()
                    if output:
                        exec_msg = f"\n\n```\n{output[:1000]}\n```\n"
                        full_text += exec_msg
                        asyncio.run_coroutine_threadsafe(
                            queue.put(sse({"type": "text", "chunk": exec_msg})),
                            loop,
                        )
                        # Emit truncated result to log
                        short_result = output[:120].replace("\n", " ").strip()
                        asyncio.run_coroutine_threadsafe(
                            queue.put(sse({"type": "log", "kind": "result", "message": short_result})),
                            loop,
                        )
                except subprocess.TimeoutExpired:
                    asyncio.run_coroutine_threadsafe(
                        queue.put(sse({"type": "text", "chunk": "\n(command timed out, man)\n"})),
                        loop,
                    )
                except Exception as e:
                    log.error(f"Exec error: {e}")

    return full_text


async def stream_response(visitor_id: str, user_message: str, prefix_events: list[str] | None = None):
    """SSE generator: routes to chat or coding mode, then TTS."""
    t0 = time.time()
    log.info(f"[{visitor_id}] User: {user_message[:100]}")

    # Emit structured log for the UI
    is_coding = _is_coding_request(user_message)
    if is_coding:
        await queue.put(sse({"type": "log", "kind": "input", "message": user_message[:80]}))
        await queue.put(sse({"type": "log", "kind": "action", "message": "Firing up the toolbox..."}))

    if prefix_events:
        for e in prefix_events:
            yield e

    queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    # Route to the right backend
    coding = _is_coding_request(user_message)
    worker = _coding_worker if coding else _llm_worker
    log.info(f"[{visitor_id}] Mode: {'CODING' if coding else 'CHAT'}")

    future = loop.run_in_executor(executor, worker, visitor_id, user_message, queue, loop)

    while True:
        if future.done() and queue.empty():
            break
        try:
            event = await asyncio.wait_for(queue.get(), timeout=0.1)
        except asyncio.TimeoutError:
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
    log.info(f"[{visitor_id}] {'OpenClaw' if coding else 'LLM'} ({llm_ms}ms): {full_text[:80]}")

    # Generate TTS — only speak the conversational parts
    speakable = _extract_speakable(full_text)
    if speakable:
        try:
            audio_bytes = await asyncio.wait_for(
                generate_audio(speakable),
                timeout=30,
            )
            b64 = base64.b64encode(audio_bytes).decode()
            yield sse({"type": "audio", "data": b64})
            tts_ms = int((time.time() - t0) * 1000) - llm_ms
            log.info(f"[{visitor_id}] TTS ({tts_ms}ms, {len(speakable)} chars spoken)")
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


from starlette.websockets import WebSocket, WebSocketDisconnect

# -- WebSocket Voice Pipeline (continuous conversation) --

VOICE_SAMPLE_RATE = 16000
VOICE_SILENCE_THRESHOLD = 1500    # Int16 RMS threshold
VOICE_SILENCE_DURATION = 2.0     # seconds of silence to trigger end-of-speech
VOICE_MIN_SPEECH_DURATION = 0.5  # minimum speech duration to process (seconds)
VOICE_BUFFER_MAX = VOICE_SAMPLE_RATE * 2 * 30  # 30 seconds max buffer (Int16 = 2 bytes/sample)


def _pcm_rms(pcm_bytes: bytes) -> float:
    """Compute RMS of Int16 PCM audio."""
    import numpy as np
    if len(pcm_bytes) < 2:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16)
    return float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))


def _pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    """Wrap raw Int16 PCM in a WAV header."""
    import struct
    num_samples = len(pcm_bytes) // 2
    wav = bytearray()
    wav.extend(b'RIFF')
    wav.extend(struct.pack('<I', 36 + len(pcm_bytes)))
    wav.extend(b'WAVE')
    wav.extend(b'fmt ')
    wav.extend(struct.pack('<I', 16))       # chunk size
    wav.extend(struct.pack('<H', 1))        # PCM format
    wav.extend(struct.pack('<H', 1))        # mono
    wav.extend(struct.pack('<I', sample_rate))
    wav.extend(struct.pack('<I', sample_rate * 2))  # byte rate
    wav.extend(struct.pack('<H', 2))        # block align
    wav.extend(struct.pack('<H', 16))       # bits per sample
    wav.extend(b'data')
    wav.extend(struct.pack('<I', len(pcm_bytes)))
    wav.extend(pcm_bytes)
    return bytes(wav)


@app.get("/api/sync")
async def sync_memory():
    """Pull latest memory from rat."""
    import subprocess
    try:
        result = subprocess.run(
            ["scp", "-r", "-i", os.path.expanduser("~/.ssh/hsvr_ed25519"),
             "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=no",
             "therat@192.168.88.30:~/.claude/projects/-Users-therat-wip-hsr-bench/memory/*",
             os.path.expanduser("~/hsr-bench/.claude-memory/")],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            log.info("Memory sync from rat: OK")
            return {"status": "ok", "message": "synced from rat"}
        else:
            log.warning(f"Memory sync failed: {result.stderr[:100]}")
            return {"status": "warn", "message": "sync failed, using cached"}
    except Exception as e:
        log.warning(f"Memory sync error: {e}")
        return {"status": "warn", "message": str(e)[:80]}

@app.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):
    await ws.accept()
    visitor_id = "ws-voice"
    log.info(f"[{visitor_id}] WebSocket voice connected")

    audio_buffer = bytearray()
    speech_detected = False
    silence_start = None
    speech_ready = asyncio.Event()
    is_responding = False
    should_close = False

    async def receive_audio():
        nonlocal audio_buffer, speech_detected, silence_start, should_close, is_responding

        try:
            while not should_close:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue

                if msg["type"] == "websocket.disconnect":
                    should_close = True
                    speech_ready.set()
                    break

                if msg["type"] == "websocket.receive":
                    data = msg.get("bytes")
                    if not data:
                        # Could be a text message (control)
                        text = msg.get("text", "")
                        if text == "stop":
                            should_close = True
                            speech_ready.set()
                            break
                        continue

                    # Skip audio while Dude is responding (prevent echo)
                    if is_responding:
                        continue

                    # Append to buffer (cap at max)
                    audio_buffer.extend(data)
                    if len(audio_buffer) > VOICE_BUFFER_MAX:
                        audio_buffer = audio_buffer[-VOICE_BUFFER_MAX:]

                    # VAD on this chunk
                    rms = _pcm_rms(data)

                    if len(audio_buffer) % 32000 == 0 and len(audio_buffer) > 0:
                        log.info(f"[{visitor_id}] Buffer: {len(audio_buffer)} bytes, rms={rms:.0f}, speech={speech_detected}")
                    if rms > VOICE_SILENCE_THRESHOLD:
                        speech_detected = True
                        silence_start = None
                    elif speech_detected:
                        if silence_start is None:
                            silence_start = time.time()
                        elif time.time() - silence_start >= VOICE_SILENCE_DURATION:
                            # End of speech detected
                            min_bytes = int(VOICE_MIN_SPEECH_DURATION * VOICE_SAMPLE_RATE * 2)
                            if len(audio_buffer) >= min_bytes:
                                speech_ready.set()
                            else:
                                # Too short, reset
                                audio_buffer.clear()
                            speech_detected = False
                            silence_start = None

        except WebSocketDisconnect:
            should_close = True
            speech_ready.set()
        except Exception as e:
            log.error(f"[{visitor_id}] WS receive error: {e}")
            should_close = True
            speech_ready.set()

    async def process_loop():
        nonlocal audio_buffer, speech_detected, silence_start, is_responding, should_close

        try:
            while not should_close:
                await ws.send_json({"type": "listening"})

                # Wait for speech
                speech_ready.clear()
                await speech_ready.wait()

                if should_close:
                    break

                if not audio_buffer:
                    continue

                # Grab the buffer and reset
                pcm_data = bytes(audio_buffer)
                audio_buffer.clear()
                speech_detected = False
                silence_start = None
                is_responding = True

                try:
                    await ws.send_json({"type": "processing"})

                    # STT
                    wav_bytes = _pcm_to_wav(pcm_data, VOICE_SAMPLE_RATE)
                    result = await transcribe_audio(wav_bytes, media_type="audio/wav")
                    user_text = result["text"].strip()
                    log.info(f"[{visitor_id}] STT: {user_text[:80]}")

                    if not user_text:
                        is_responding = False
                        continue

                    await ws.send_json({"type": "transcription", "text": user_text})

                    # LLM
                    queue = asyncio.Queue()
                    loop = asyncio.get_event_loop()

                    coding = _is_coding_request(user_text)
                    worker = _coding_worker if coding else _llm_worker
                    log.info(f"[{visitor_id}] Mode: {'CODING' if coding else 'CHAT'}")

                    future = loop.run_in_executor(executor, worker, visitor_id, user_text, queue, loop)

                    # Stream LLM text and TTS per-sentence as text arrives
                    full_text = ""
                    pending_tts = ""
                    tts_count = 0
                    import re as _re
                    sentence_end = _re.compile(r'[.!?]\s')

                    while True:
                        if future.done() and queue.empty():
                            break
                        try:
                            event = await asyncio.wait_for(queue.get(), timeout=0.1)
                        except asyncio.TimeoutError:
                            continue
                        if event is None:
                            break
                        # Parse SSE event to get chunk
                        if event.startswith("data: "):
                            try:
                                evt = json.loads(event[6:].strip())
                                if evt.get("type") == "text":
                                    full_text += evt["chunk"]
                                    pending_tts += evt["chunk"]
                                    await ws.send_json({"type": "text", "chunk": evt["chunk"]})

                                    # Check if we have a complete sentence to TTS
                                    match = sentence_end.search(pending_tts)
                                    if match:
                                        # TTS everything up to end of sentence
                                        split_pos = match.end()
                                        to_speak = _extract_speakable(pending_tts[:split_pos].strip())
                                        pending_tts = pending_tts[split_pos:]
                                        if to_speak:
                                            try:
                                                audio_bytes = await asyncio.wait_for(
                                                    generate_audio(to_speak), timeout=15)
                                                b64 = base64.b64encode(audio_bytes).decode()
                                                await ws.send_json({"type": "audio", "data": b64})
                                                tts_count += 1
                                            except Exception as e:
                                                log.error(f"[{visitor_id}] TTS error: {e}")
                            except json.JSONDecodeError:
                                pass

                    if not full_text:
                        full_text = future.result() or ""

                    # TTS any remaining text
                    if pending_tts.strip():
                        to_speak = _extract_speakable(pending_tts.strip())
                        if to_speak:
                            try:
                                audio_bytes = await asyncio.wait_for(
                                    generate_audio(to_speak), timeout=15)
                                b64 = base64.b64encode(audio_bytes).decode()
                                await ws.send_json({"type": "audio", "data": b64})
                                tts_count += 1
                            except Exception as e:
                                log.error(f"[{visitor_id}] TTS error: {e}")

                    if full_text:
                        log.info(f"[{visitor_id}] LLM: {full_text[:80]}")
                        log.info(f"[{visitor_id}] TTS: {tts_count} chunks")

                    await ws.send_json({"type": "done"})

                except Exception as e:
                    log.error(f"[{visitor_id}] Process error: {e}")
                    try:
                        await ws.send_json({"type": "error", "message": str(e)})
                    except Exception:
                        pass
                finally:
                    is_responding = False

        except WebSocketDisconnect:
            pass
        except Exception as e:
            log.error(f"[{visitor_id}] Process loop error: {e}")

    try:
        await asyncio.gather(receive_audio(), process_loop())
    except Exception:
        pass
    finally:
        log.info(f"[{visitor_id}] WebSocket voice disconnected")



# -- Native PTY WebSocket terminal (same-origin, no ttyd needed) --
import pty
import select
import struct
import fcntl
import termios
from starlette.websockets import WebSocket, WebSocketDisconnect

@app.websocket("/ws/terminal")
async def terminal_ws(ws: WebSocket):
    await ws.accept()
    pid, fd = pty.fork()
    if pid == 0:
        # Child: exec bash
        os.environ["TERM"] = "xterm-256color"
        os.environ["PATH"] = os.path.expanduser("~/.npm-global/bin:~/.local/bin:") + os.environ.get("PATH", "")
        os.chdir(os.path.expanduser("~"))
        os.execvp("bash", ["bash", "--login"])
    else:
        # Parent: bridge PTY <-> WebSocket
        loop = asyncio.get_event_loop()
        closed = asyncio.Event()

        async def pty_to_ws():
            try:
                while not closed.is_set():
                    r, _, _ = await loop.run_in_executor(None, select.select, [fd], [], [], 0.1)
                    if r:
                        try:
                            data = os.read(fd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        try:
                            await ws.send_text(data.decode("utf-8", errors="replace"))
                        except Exception:
                            break
            except Exception:
                pass
            finally:
                closed.set()

        async def ws_to_pty():
            try:
                while not closed.is_set():
                    msg = await ws.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    text = msg.get("text", "")
                    if text.startswith("\x01"):
                        try:
                            resize = json.loads(text[1:])
                            winsize = struct.pack("HHHH", resize["rows"], resize["cols"], 0, 0)
                            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                        except Exception:
                            pass
                    else:
                        os.write(fd, text.encode("utf-8"))
            except Exception:
                pass
            finally:
                closed.set()

        try:
            await asyncio.gather(pty_to_ws(), ws_to_pty())
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except Exception:
                pass

# Serve static files — use a custom handler so WebSocket routes aren't shadowed
from starlette.responses import FileResponse, Response as StarletteResponse
_static_dir = os.path.dirname(os.path.abspath(__file__))

@app.get("/{path:path}")
async def serve_static(path: str = ""):
    if not path or path == "/":
        path = "index.html"
    file_path = os.path.join(_static_dir, path)
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    return StarletteResponse("Not found", status_code=404)
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

