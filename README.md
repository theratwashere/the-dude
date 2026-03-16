# The Dude

Interactive AI avatar with The Dude persona — green Matrix-style portrait with live conversational AI, powered by Perplexity Sonar for web-grounded answers.

## Architecture

- **Frontend**: Single-page HTML/JS with spring-physics mouth animation, mic + text input
- **Backend**: FastAPI (Python) — Perplexity Sonar API (web-grounded AI), ElevenLabs TTS (clyde voice), SSE streaming
- **Portrait**: 1080x1920 (rotated monitor), green Matrix-style Dude face

## Files

| File | Description |
|------|-------------|
| `index.html` | Frontend — portrait display, audio capture, spring-physics lip sync |
| `api_server.py` | FastAPI backend — Perplexity Sonar LLM, TTS, SSE streaming |
| `generate_audio.py` | ElevenLabs TTS helper |
| `transcribe_audio.py` | ElevenLabs Scribe STT helper |
| `dude-idle.png` | Portrait face, mouth closed (1024x1536) |
| `dude-talk.png` | Portrait face, mouth open (1024x1536) |

## Running

```bash
pip install fastapi uvicorn openai httpx
export PERPLEXITY_API_KEY="your-perplexity-api-key"
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PERPLEXITY_API_KEY` | Yes | — | Perplexity API key for Sonar access |
| `DUDE_MODEL` | No | `sonar-pro` | Model to use: `sonar`, `sonar-pro`, `sonar-reasoning-pro`, `sonar-deep-research` |
| `DUDE_MODE` | No | `chat` | Operating mode |
| `ELEVENLABS_API_KEY` | Yes | — | ElevenLabs API key for TTS and transcription |

## Target Hardware

- **Display**: Portrait monitor (1080x1920)
- **Compute**: NVIDIA Jetson / edge device
- **GPU Server**: RTX 2000 Ada (hsvr) for heavy inference

## Roadmap

- [ ] NVIDIA Audio2Face integration for realistic lip sync
- [ ] Riva ASR/TTS on Jetson
- [ ] Full Jetson deployment
