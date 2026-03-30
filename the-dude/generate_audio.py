"""Local TTS via Kokoro. High-quality neural voice synthesis.

Usage:
    from generate_audio import generate_audio
    audio_bytes = await generate_audio("Hello world")
"""

import asyncio
import io
import logging
import wave

log = logging.getLogger("the-dude.tts")

# Lazy-loaded Kokoro pipeline
_pipe = None
VOICE = "am_echo"


def _get_pipe():
    global _pipe
    if _pipe is None:
        from kokoro import KPipeline
        log.info("Loading Kokoro TTS pipeline...")
        _pipe = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
        log.info(f"Kokoro ready, voice={VOICE}")
    return _pipe


def _synthesize_sync(text: str) -> bytes:
    """Generate speech with Kokoro and return WAV bytes."""
    import soundfile as sf
    pipe = _get_pipe()

    buf = io.BytesIO()
    for gs, ps, audio in pipe(text, voice=VOICE):
        sf.write(buf, audio, 24000, format="WAV", subtype="PCM_16")
        break  # First chunk contains full audio for short text

    return buf.getvalue()


async def generate_audio(
    text: str,
    *,
    voice: str = "am_echo",
    model: str = "kokoro",
) -> bytes:
    """Generate speech audio from text using local Kokoro TTS.
    Returns WAV bytes.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _synthesize_sync, text)
