"""Local STT via faster-whisper. Drop-in replacement for the cloud STT module.

Usage:
    from transcribe_audio import transcribe_audio
    result = await transcribe_audio(audio_bytes, media_type="audio/webm")
    print(result["text"])
"""

import asyncio
import io
import logging
import tempfile

log = logging.getLogger("the-dude.stt")

# Lazy-loaded model
_model = None


def _get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model (base)...")
        # Use CUDA if available, fall back to CPU
        try:
            _model = WhisperModel("base", device="cuda", compute_type="float16")
            log.info("Whisper model loaded (CUDA float16)")
        except Exception:
            _model = WhisperModel("base", device="cpu", compute_type="int8")
            log.info("Whisper model loaded (CPU int8)")
    return _model


def _transcribe_sync(audio_bytes: bytes, media_type: str) -> dict:
    """Transcribe audio bytes using faster-whisper."""
    model = _get_model()

    # Write audio to temp file (faster-whisper needs a file path)
    suffix = ".webm" if "webm" in media_type else ".wav" if "wav" in media_type else ".mp3"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as f:
        f.write(audio_bytes)
        f.flush()

        segments, info = model.transcribe(f.name, beam_size=5, vad_filter=True)
        text = " ".join(seg.text.strip() for seg in segments)

    return {
        "text": text,
        "language_code": info.language,
        "words": [],
    }


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    media_type: str = "audio/mpeg",
    timestamps: str = "none",
    diarize: bool = False,
    num_speakers: int | None = None,
    language: str | None = None,
    model: str = "whisper-base",
) -> dict:
    """Transcribe audio using local faster-whisper.
    Returns dict with 'text', 'language_code', 'words' keys.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _transcribe_sync, audio_bytes, media_type)
