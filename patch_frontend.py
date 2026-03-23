#!/usr/bin/env python3
"""Patch index.html to handle insecure context (HTTP) for LiveKit."""

import sys

with open(sys.argv[1], 'r') as f:
    content = f.read()

# 1. Replace the Room constructor to not include audioCaptureDefaults on insecure context
old_room = """    // Create room
    lkRoom = new LK.Room({
      adaptiveStream: true,
      dynacast: true,
      audioCaptureDefaults: { echoCancellation: true, noiseSuppression: true },
    });"""

new_room = """    // Create room - skip audio capture defaults on insecure origins (no mediaDevices)
    const hasMediaDevices = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
    const roomOpts = { adaptiveStream: true, dynacast: true };
    if (hasMediaDevices) {
      roomOpts.audioCaptureDefaults = { echoCancellation: true, noiseSuppression: true };
    }
    lkRoom = new LK.Room(roomOpts);"""

if old_room not in content:
    print("ERROR: Could not find Room constructor to patch")
    sys.exit(1)
content = content.replace(old_room, new_room)
print("Patched Room constructor")

# 2. Replace the mic enablement to gracefully handle insecure context
old_mic = """    // Step 3: Enable microphone
    setStatus('Enabling mic...');
    await lkRoom.localParticipant.setMicrophoneEnabled(true);
    console.log('Microphone published');

    lkConnected = true;
    lkConnecting = false;
    setStatus('Listening...');"""

new_mic = """    // Step 3: Enable microphone (only on secure contexts)
    if (hasMediaDevices) {
      setStatus('Enabling mic...');
      await lkRoom.localParticipant.setMicrophoneEnabled(true);
      console.log('Microphone published');
      lkConnected = true;
      lkConnecting = false;
      setStatus('Listening...');
    } else {
      // HTTP / insecure context: mic unavailable, listen-only voice mode
      console.warn('No mediaDevices (insecure context): listen-only voice mode');
      lkConnected = true;
      lkConnecting = false;
      setStatus('Voice connected (type to talk)');
      setTimeout(() => setStatus(''), 4000);
    }"""

if old_mic not in content:
    print("ERROR: Could not find mic enablement to patch")
    sys.exit(1)
content = content.replace(old_mic, new_mic)
print("Patched mic enablement")

with open(sys.argv[1], 'w') as f:
    f.write(content)

print("Done - saved patched file")
