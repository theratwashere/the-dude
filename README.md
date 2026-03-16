# The Dude

Interactive AI avatar with The Dude persona — green Matrix-style portrait that serves as a voice/visual interface for Perplexity Computer running in Comet browser. No API keys needed for LLM — The Dude talks to Computer via Chrome DevTools Protocol.

## Architecture

- **Frontend**: Single-page HTML/JS with spring-physics mouth animation, mic + text input
- **Backend**: FastAPI (Python) — Comet CDP bridge to Perplexity Computer, ElevenLabs TTS (clyde voice), SSE streaming
- **Portrait**: 1080x1920 (rotated monitor), green Matrix-style Dude face
- **Brain**: Perplexity Computer running in Comet browser on the same machine

```
User ──> The Dude (FastAPI) ──CDP──> Comet Browser ──> Perplexity Computer
                │                                              │
                │<──────── SSE text + TTS audio <──────────────┘
```

## Files

| File | Description |
|------|-------------|
| `index.html` | Frontend — portrait display, audio capture, spring-physics lip sync |
| `api_server.py` | FastAPI backend — Comet CDP bridge, TTS, SSE streaming |
| `comet_bridge.py` | CDP client — connects to Comet, sends prompts, extracts responses |
| `generate_audio.py` | ElevenLabs TTS helper |
| `transcribe_audio.py` | ElevenLabs Scribe STT helper |
| `dude-idle.png` | Portrait face, mouth closed (1024x1536) |
| `dude-talk.png` | Portrait face, mouth open (1024x1536) |

## Prerequisites

1. **Comet browser** installed and running with remote debugging enabled:
   ```bash
   /Applications/Comet.app/Contents/MacOS/Comet --remote-debugging-port=9222
   ```
2. **Navigate to perplexity.ai** in Comet and log in to your account
3. Perplexity Computer should be active (The Dude sends prompts through it)

## Running

```bash
pip install fastapi uvicorn aiohttp websockets
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in a browser (separate from Comet).

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `COMET_CDP_PORT` | No | `9222` | CDP port for Comet browser |
| `DUDE_PERSONA` | No | *(empty)* | Optional persona prefix for prompts (e.g. "Respond casually like The Dude.") |
| `DUDE_MODE` | No | `chat` | Operating mode |
| `ELEVENLABS_API_KEY` | Yes | — | ElevenLabs API key for TTS and transcription |

## How It Works

1. User speaks or types a message
2. The Dude backend connects to Comet browser via CDP (WebSocket)
3. Finds the Perplexity tab and types the message into Computer's input
4. Polls for Computer's response (checking for loading indicators, stop buttons, prose content)
5. Extracts the response text and streams it to the frontend via SSE
6. Generates TTS audio and sends it for lip-synced playback

## Target Hardware

- **Host**: Mac mini ("the Rat") running both Comet and The Dude
- **Display**: Portrait monitor (1080x1920)

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/chat` | POST | Send a text message, get SSE stream back |
| `/api/voice` | POST | Send audio, get transcription + SSE stream back |
| `/api/health` | GET | Health check |
| `/api/status` | GET | Check Comet/CDP connectivity |
