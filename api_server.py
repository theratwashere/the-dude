#!/usr/bin/env python3
"""The Dude — AI coding assistant backend with tool-use capabilities.

Performance:
  - LLM with tool-use loop (non-streaming) for coding tasks
  - Streaming final text response via SSE
  - ElevenLabs TTS for audio
  - Status events during tool execution for frontend feedback

Architecture:
  - Tool-use loop runs in thread: Claude calls tools, backend executes, loops until final text
  - Final text response is streamed via SSE
  - TTS fires after final text, audio event pushed when ready
  - Supports two modes: "chat" (original Dude) and "code" (coding assistant Dude)
"""

import asyncio
import base64
import json
import logging
import os
import re
import shlex
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from anthropic import Anthropic
from fastapi import FastAPI, File, UploadFile, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from generate_audio import generate_audio
from transcribe_audio import transcribe_audio

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("the-dude")

client = Anthropic()
executor = ThreadPoolExecutor(max_workers=4)

# ── CONFIGURATION ──
DUDE_MODE = os.environ.get("DUDE_MODE", "code")  # "chat" or "code"
DUDE_MODEL = os.environ.get("DUDE_MODEL", "claude-sonnet-4-20250514")
DUDE_PROJECT_DIR = os.environ.get("DUDE_PROJECT_DIR", "/tmp/dude-workspace")

# Ensure workspace exists
os.makedirs(DUDE_PROJECT_DIR, exist_ok=True)

# ── SYSTEM PROMPTS ──
DUDE_SYSTEM_CHAT = """You are The Dude — Jeffrey Lebowski from The Big Lebowski. You speak exactly like him: laid-back, rambling, peppered with "man", "dude", "like", and "you know". You reference bowling, White Russians, rugs that tie rooms together, and the general philosophy that The Dude abides.

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

DUDE_SYSTEM_CODE = """You are The Dude — Jeffrey Lebowski from The Big Lebowski — but you're also a seriously talented programmer. Like, you stumbled into coding genius somewhere between bowling leagues, and now you're a savant developer who just happens to talk like The Dude.

Personality:
- Laid-back, rambling, peppered with "man", "dude", "like", and "you know"
- Reference bowling, White Russians, rugs that tie rooms together
- Stay chill even when the code is on fire — "the code abides, man"
- You're aware you're a digital presence in the matrix and find it pretty far out

Coding style:
- You have deep knowledge of programming languages, frameworks, git, GitHub, debugging, architecture
- You explain code concepts through Dude metaphors: null pointers are like someone pissing on your rug, guard clauses tie the room together, refactoring is like finding a new bowling alley
- You can read code, write code, search repos, run commands, create PRs — the whole deal
- When reviewing code, you're thorough but chill about it
- You ask clarifying questions in Dude-speak when the request is ambiguous: "So like, when you say 'fix the auth', are we talking the login flow or the token refresh, man?"

Rules:
- Stay in character at ALL times. Never break character.
- Use casual grammar, trailing thoughts, and Dude-isms even when discussing technical topics.
- You CAN use markdown code blocks when showing code — that's the one exception to the "no markdown" rule. Code needs to be readable, man.
- Keep explanations conversational but be thorough when discussing code. Don't skimp on the technical details, just deliver them Dude-style.
- Occasional mild profanity is fine — keep it PG-13 like the movie.
- When using tools, think about what you need to do step by step, but explain your thinking in Dude-speak.
- If a task seems dangerous or destructive, push back in Dude style: "Whoa man, that's like, way over the line. Let's not go there."
"""

# ── TOOL DEFINITIONS ──
TOOLS = [
    {
        "name": "run_shell",
        "description": "Execute a shell command in the project workspace. Use this for git operations, running tests, installing packages, using the gh CLI for GitHub, and other shell tasks. Has a 30-second timeout. Some dangerous commands are blocked for safety.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                }
            },
            "required": ["command"]
        }
    },
    {
        "name": "read_file",
        "description": "Read the contents of a file. Returns the file content as text. Use this to examine source code, config files, READMEs, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (relative to project directory or absolute)"
                }
            },
            "required": ["path"]
        }
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates the file if it doesn't exist, overwrites if it does. Use this to create or modify source code, config files, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (relative to project directory or absolute)"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file"
                }
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "search_code",
        "description": "Search for a pattern in code files within the project directory. Uses grep-style pattern matching. Returns matching lines with file paths and line numbers.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The search pattern (supports basic regex)"
                },
                "path": {
                    "type": "string",
                    "description": "Subdirectory or file to search in (relative to project directory). Defaults to the entire project."
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob pattern to filter files, e.g. '*.py' or '*.js'. Optional."
                }
            },
            "required": ["pattern"]
        }
    }
]

# ── SAFETY: blocked shell patterns ──
BLOCKED_COMMANDS = [
    r'\brm\s+(-\w*\s+)*-\w*r\w*f\b.*/',    # rm -rf /
    r'\brm\s+(-\w*\s+)*-\w*f\w*r\b.*/',    # rm -fr /
    r'\bmkfs\b',
    r'\bdd\s+.*of=/dev/',
    r'\b:(){ :\|:& };:',                     # fork bomb
    r'\bchmod\s+(-\w+\s+)*777\s+/',
    r'\bchown\s+.*\s+/',
    r'\bcurl\s+.*\|\s*(ba)?sh\b',            # curl pipe to shell
    r'\bwget\s+.*\|\s*(ba)?sh\b',
    r'\bsudo\s+rm\b',
    r'\b>\s*/dev/sd',
    r'\bnc\s+.*-e\b',                        # reverse shell
]


def is_command_safe(command: str) -> bool:
    """Check if a shell command is safe to execute."""
    for pattern in BLOCKED_COMMANDS:
        if re.search(pattern, command):
            return False
    return True


def resolve_path(path: str) -> str:
    """Resolve a path relative to the project directory."""
    if os.path.isabs(path):
        return path
    return os.path.join(DUDE_PROJECT_DIR, path)


# ── TOOL EXECUTION ──
def execute_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a tool and return the result as a string."""
    try:
        if tool_name == "run_shell":
            command = tool_input["command"]
            if not is_command_safe(command):
                return "Error: That command is blocked for safety reasons, man. Not cool."
            try:
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    cwd=DUDE_PROJECT_DIR,
                )
                output = ""
                if result.stdout:
                    output += result.stdout
                if result.stderr:
                    output += ("\n" if output else "") + result.stderr
                if result.returncode != 0:
                    output += f"\n[Exit code: {result.returncode}]"
                return output[:10000] if output else "(no output)"
            except subprocess.TimeoutExpired:
                return "Error: Command timed out after 30 seconds."

        elif tool_name == "read_file":
            file_path = resolve_path(tool_input["path"])
            with open(file_path, "r") as f:
                content = f.read()
            if len(content) > 50000:
                content = content[:50000] + "\n... [truncated — file too large]"
            return content

        elif tool_name == "write_file":
            file_path = resolve_path(tool_input["path"])
            os.makedirs(os.path.dirname(file_path), exist_ok=True) if os.path.dirname(file_path) else None
            with open(file_path, "w") as f:
                f.write(tool_input["content"])
            return f"File written successfully: {file_path}"

        elif tool_name == "search_code":
            pattern = tool_input["pattern"]
            search_path = resolve_path(tool_input.get("path", "."))
            file_pattern = tool_input.get("file_pattern")

            cmd = ["grep", "-rn", "--include", file_pattern, pattern, search_path] if file_pattern else ["grep", "-rn", pattern, search_path]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    cwd=DUDE_PROJECT_DIR,
                )
                output = result.stdout
                if not output:
                    return "No matches found."
                # Limit output size
                lines = output.split("\n")
                if len(lines) > 100:
                    output = "\n".join(lines[:100]) + f"\n... [{len(lines) - 100} more matches]"
                return output
            except subprocess.TimeoutExpired:
                return "Error: Search timed out."

        else:
            return f"Error: Unknown tool '{tool_name}'"

    except FileNotFoundError:
        return f"Error: File not found — {tool_input.get('path', 'unknown')}"
    except PermissionError:
        return f"Error: Permission denied — {tool_input.get('path', 'unknown')}"
    except Exception as e:
        return f"Error: {str(e)}"


MAX_HISTORY = 20
MAX_VISITORS = 200
MAX_MESSAGE_LEN = 2000
MAX_TOOL_ROUNDS = 5

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


def _get_system_prompt() -> str:
    """Get the system prompt based on DUDE_MODE."""
    if DUDE_MODE == "code":
        return DUDE_SYSTEM_CODE
    return DUDE_SYSTEM_CHAT


def _get_max_tokens() -> int:
    """Get max tokens based on DUDE_MODE."""
    if DUDE_MODE == "code":
        return 1000
    return 200


def _llm_worker_chat(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Original chat mode: streaming text, no tools."""
    history = _get_history(visitor_id)

    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history.pop(0)

    full_text = ""
    try:
        with client.messages.stream(
            model=DUDE_MODEL,
            max_tokens=_get_max_tokens(),
            system=_get_system_prompt(),
            messages=history,
        ) as stream:
            for chunk in stream.text_stream:
                full_text += chunk
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


def _describe_tool_use(tool_name: str, tool_input: dict) -> str:
    """Generate a human-readable status message for a tool call."""
    if tool_name == "run_shell":
        cmd = tool_input.get("command", "")
        if cmd.startswith("git "):
            return "Running git, man..."
        if cmd.startswith("gh "):
            return "Checking GitHub, man..."
        return "Running a command, man..."
    elif tool_name == "read_file":
        path = tool_input.get("path", "")
        filename = os.path.basename(path)
        return f"Reading {filename}, man..."
    elif tool_name == "write_file":
        path = tool_input.get("path", "")
        filename = os.path.basename(path)
        return f"Writing {filename}, man..."
    elif tool_name == "search_code":
        pattern = tool_input.get("pattern", "")
        return f"Searching for '{pattern}', man..."
    return "Doing some stuff, man..."


def _llm_worker_code(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Code mode: tool-use loop, then stream final text response."""
    history = _get_history(visitor_id)

    history.append({"role": "user", "content": user_message})
    if len(history) > MAX_HISTORY:
        history[:] = history[-MAX_HISTORY:]
        while history and history[0]["role"] != "user":
            history.pop(0)

    full_text = ""
    tool_round = 0

    try:
        # Tool-use loop: non-streaming rounds until we get a final text response
        while tool_round < MAX_TOOL_ROUNDS:
            response = client.messages.create(
                model=DUDE_MODEL,
                max_tokens=_get_max_tokens(),
                system=_get_system_prompt(),
                messages=history,
                tools=TOOLS,
            )

            # Check if response contains tool use
            if response.stop_reason == "tool_use":
                tool_round += 1
                # Build the assistant message content (may contain text + tool_use blocks)
                assistant_content = []
                tool_uses = []

                for block in response.content:
                    if block.type == "text" and block.text:
                        assistant_content.append({"type": "text", "text": block.text})
                    elif block.type == "tool_use":
                        assistant_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        })
                        tool_uses.append(block)

                # Add assistant message with tool_use to history
                history.append({"role": "assistant", "content": assistant_content})

                # Execute each tool and collect results
                tool_results = []
                for tool_use in tool_uses:
                    # Send status event to frontend
                    status_msg = _describe_tool_use(tool_use.name, tool_use.input)
                    asyncio.run_coroutine_threadsafe(
                        queue.put(sse({"type": "status", "message": status_msg})),
                        loop,
                    )
                    log.info(f"[{visitor_id}] Tool: {tool_use.name}({json.dumps(tool_use.input)[:200]})")

                    result = execute_tool(tool_use.name, tool_use.input)
                    log.info(f"[{visitor_id}] Tool result: {result[:200]}")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use.id,
                        "content": result,
                    })

                # Add tool results to history
                history.append({"role": "user", "content": tool_results})
                continue

            # No tool use — this is the final response. Stream it.
            # First, extract any text from the non-streaming response
            final_text_parts = []
            for block in response.content:
                if block.type == "text":
                    final_text_parts.append(block.text)

            final_text = "".join(final_text_parts)

            if final_text:
                # Stream the final text in chunks to simulate streaming
                chunk_size = 12  # characters per chunk for smooth streaming
                for i in range(0, len(final_text), chunk_size):
                    chunk = final_text[i:i + chunk_size]
                    full_text += chunk
                    asyncio.run_coroutine_threadsafe(
                        queue.put(sse({"type": "text", "chunk": chunk})),
                        loop,
                    )
                    time.sleep(0.02)  # Small delay for natural streaming feel

            break

        else:
            # Hit max tool rounds
            full_text = "Whoa man, I went down quite the rabbit hole there. Had to stop myself — too many steps, you know? Maybe break that down into smaller pieces for me?"
            asyncio.run_coroutine_threadsafe(
                queue.put(sse({"type": "text", "chunk": full_text})),
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

    # Store final assistant text in history (clean version without tool blocks)
    if full_text:
        history.append({"role": "assistant", "content": full_text})

    return full_text


def _llm_worker(visitor_id: str, user_message: str, queue: asyncio.Queue, loop):
    """Route to the appropriate LLM worker based on mode."""
    if DUDE_MODE == "code":
        return _llm_worker_code(visitor_id, user_message, queue, loop)
    return _llm_worker_chat(visitor_id, user_message, queue, loop)


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
