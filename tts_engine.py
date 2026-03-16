"""TTS Engine — text-to-speech for The Dude.

Priority order:
  1. edge-tts (Microsoft Edge TTS, free, high quality, pip install edge-tts)
  2. macOS `say` command (built-in, converts to wav then base64)
  3. No TTS available (graceful degradation)

Zero API keys needed.
"""

import asyncio
import base64
import io
import logging
import shutil
import subprocess
import tempfile
import os

log = logging.getLogger("tts-engine")

# Check what's available
HAS_EDGE_TTS = False
HAS_MACOS_SAY = False

try:
    import edge_tts
    HAS_EDGE_TTS = True
    log.info("TTS engine: edge-tts available")
except ImportError:
    pass

if shutil.which("say"):
    HAS_MACOS_SAY = True
    log.info("TTS engine: macOS say available")

HAS_TTS = HAS_EDGE_TTS or HAS_MACOS_SAY

if not HAS_TTS:
    log.warning("No TTS engine available. Install edge-tts: pip3 install edge-tts")

# Default voice for edge-tts — a laid-back male voice
EDGE_VOICE = os.environ.get("DUDE_VOICE", "en-US-GuyNeural")
# macOS voice fallback
MACOS_VOICE = os.environ.get("DUDE_MACOS_VOICE", "Daniel")

# Max text length for TTS (avoid very long synthesis)
MAX_TTS_LENGTH = 2000


async def generate_tts(text: str) -> bytes:
    """Generate speech audio from text. Returns MP3 bytes (edge-tts) or WAV bytes (say).

    Raises RuntimeError if no TTS engine is available.
    """
    if not HAS_TTS:
        raise RuntimeError("No TTS engine available")

    # Truncate very long text
    if len(text) > MAX_TTS_LENGTH:
        text = text[:MAX_TTS_LENGTH] + "... that's enough for now, man."

    # Clean text for TTS — remove markdown artifacts
    clean = _clean_for_tts(text)
    if not clean.strip():
        raise RuntimeError("No speakable text after cleanup")

    if HAS_EDGE_TTS:
        return await _edge_tts(clean)
    elif HAS_MACOS_SAY:
        return await _macos_say(clean)
    else:
        raise RuntimeError("No TTS engine available")


def _clean_for_tts(text: str) -> str:
    """Clean text for natural TTS output."""
    import re
    # Remove code blocks entirely
    text = re.sub(r'```[\s\S]*?```', ' code block omitted ', text)
    # Remove inline code
    text = re.sub(r'`[^`]+`', '', text)
    # Remove URLs
    text = re.sub(r'https?://\S+', '', text)
    # Remove markdown bold/italic
    text = re.sub(r'\*{1,3}([^*]+)\*{1,3}', r'\1', text)
    # Remove markdown headers
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Remove bullet point markers
    text = re.sub(r'^[\-\*]\s+', '', text, flags=re.MULTILINE)
    # Remove numbered list markers
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    return text


async def _edge_tts(text: str) -> bytes:
    """Generate audio using edge-tts (returns MP3 bytes)."""
    communicate = edge_tts.Communicate(text, EDGE_VOICE)
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    if not audio_chunks:
        raise RuntimeError("edge-tts returned no audio")
    return b"".join(audio_chunks)


async def _macos_say(text: str) -> bytes:
    """Generate audio using macOS say command (returns WAV bytes)."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp_path = f.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "say", "-v", MACOS_VOICE, "-o", tmp_path,
            "--data-format=LEI16@22050",
            text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0:
            raise RuntimeError(f"say command failed with code {proc.returncode}")

        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def get_audio_content_type() -> str:
    """Return the MIME type for the audio format produced."""
    if HAS_EDGE_TTS:
        return "audio/mp3"
    elif HAS_MACOS_SAY:
        return "audio/wav"
    return "audio/mp3"
