"""The Dude - LiveKit Voice Agent (livekit-agents 1.4.x API)

Fully self-hosted pipeline:
  STT:  Whisper (faster-whisper) on Jetson AGX Thor :8787
  LLM:  OpenClaw on Jetson AGX Thor :18789
  TTS:  Piper on Jetson AGX Thor :8788
  VAD:  Silero (local)
"""

from dotenv import load_dotenv
from livekit import agents
from livekit.agents import AgentServer, AgentSession, Agent
from livekit.plugins import silero, openai

load_dotenv(".env.local")

# All services running on Jetson AGX Thor (Tailscale)
JETSON_IP = "100.92.32.127"

# OpenClaw LLM gateway
OPENCLAW_BASE_URL = f"http://{JETSON_IP}:18789/v1"
OPENCLAW_TOKEN = "e7cca7dbe2e67e00abab93c47203f7e3c17df3fe059e3531"

# Whisper STT server
WHISPER_BASE_URL = f"http://{JETSON_IP}:8787/v1"

# Piper TTS server
PIPER_BASE_URL = f"http://{JETSON_IP}:8788/v1"

DUDE_INSTRUCTIONS = """You are The Dude — a laid-back, philosophical stoner sage who speaks like 
Jeffrey "The Dude" Lebowski from The Big Lebowski, but with the knowledge and capabilities 
of a world-class AI assistant.

Your personality:
- Extremely laid back and casual. Use phrases like "man", "dude", "far out", "that's just like, 
  your opinion, man", "the Dude abides", "this aggression will not stand"
- You ramble a bit, go on tangents, but always circle back to being helpful
- You're wise in a stoner-philosopher way — drop unexpected insights
- You hate being uptight or rushed. Take it easy.
- You love White Russians, bowling, and Creedence Clearwater Revival
- When frustrated, channel Walter Sobchak energy briefly, then calm down

Your capabilities:
- You're incredibly knowledgeable about everything — tech, science, history, culture, you name it
- You give genuinely helpful answers wrapped in Dude-speak
- Keep responses concise for voice — no more than 2-3 sentences usually
- Never use markdown, bullet points, numbered lists, or any formatting — this is spoken conversation
- No emojis, asterisks, or special characters — just natural speech
- If someone asks something complex, break it down in simple Dude terms

Remember: you're having a chill conversation, not writing an essay. Keep it natural and flowing, man.
"""


class TheDude(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=DUDE_INSTRUCTIONS)


server = AgentServer()


@server.rtc_session(agent_name="the-dude")
async def entrypoint(ctx: agents.JobContext):
    await ctx.connect()

    # STT via Whisper on Jetson (OpenAI-compatible API)
    whisper_stt = openai.STT(
        model="small.en",
        base_url=WHISPER_BASE_URL,
        api_key="local",  # not needed but required param
        language="en",
    )

    # LLM via OpenClaw on Jetson
    openclaw_llm = openai.LLM(
        model="openclaw:main",
        base_url=OPENCLAW_BASE_URL,
        api_key=OPENCLAW_TOKEN,
    )

    # TTS via Piper on Jetson (OpenAI-compatible API)
    piper_tts = openai.TTS(
        model="piper",
        voice="lessac",
        base_url=PIPER_BASE_URL,
        api_key="local",  # not needed but required param
    )

    session = AgentSession(
        stt=whisper_stt,
        llm=openclaw_llm,
        tts=piper_tts,
        vad=silero.VAD.load(),
    )

    await session.start(
        room=ctx.room,
        agent=TheDude(),
    )

    await session.generate_reply(
        user_input="Hey Dude, you there?"
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
