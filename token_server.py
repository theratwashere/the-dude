"""LiveKit Token Server — generates access tokens for browser clients.

Runs alongside the LiveKit agent. The browser hits this to get a token,
then connects directly to LiveKit Cloud via WebRTC.
"""

import os
import time
from datetime import timedelta
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from livekit.api import AccessToken, VideoGrants
from livekit.protocol.room import RoomConfiguration
from livekit.protocol.agent_dispatch import RoomAgentDispatch

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_API_KEY = os.environ.get("LIVEKIT_API_KEY", "APILC8zHVQszYfj")
LIVEKIT_API_SECRET = os.environ.get("LIVEKIT_API_SECRET", "tkhGZPwIQIelDDe7zlXFu1mvLEvvDP80Xyv1QygvnkE")
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "wss://the-dude-qdzqmz67.livekit.cloud")


@app.get("/api/livekit-token")
async def get_token(room: str = None, identity: str = None):
    """Generate a LiveKit access token for the browser client.
    
    Each request gets a unique room name to ensure fresh agent dispatch.
    """
    ts = int(time.time())
    if room is None:
        room = f"dude-{ts}"
    if identity is None:
        identity = f"user-{ts}"
    
    token = (
        AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(VideoGrants(
            room_join=True,
            room=room,
            room_create=True,
            can_publish=True,
            can_subscribe=True,
        ))
        .with_room_config(RoomConfiguration(
            agents=[RoomAgentDispatch(agent_name="the-dude")]
        ))
        .with_ttl(timedelta(hours=1))
    )
    
    jwt = token.to_jwt()
    
    return {
        "token": jwt,
        "url": LIVEKIT_URL,
        "room": room,
        "identity": identity,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8081)
