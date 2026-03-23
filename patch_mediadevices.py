#!/usr/bin/env python3
"""Add navigator.mediaDevices polyfill stub for insecure contexts."""

import sys

with open(sys.argv[1], 'r') as f:
    content = f.read()

# Add a mediaDevices stub right before the SDK URL constant
# This prevents the LiveKit SDK from crashing when it tries to access mediaDevices
old_sdk_const = "const LK_SDK_URL = 'https://cdn.jsdelivr.net/npm/livekit-client@2.17.3/dist/livekit-client.umd.min.js';"

polyfill = """// Polyfill navigator.mediaDevices for insecure contexts (HTTP).
// The LiveKit SDK crashes if mediaDevices is undefined.
// This stub prevents the crash; actual mic capture will still fail gracefully.
if (!navigator.mediaDevices) {
  console.warn('Insecure context: stubbing navigator.mediaDevices');
  navigator.mediaDevices = {
    getUserMedia: () => Promise.reject(new DOMException('getUserMedia requires HTTPS', 'NotAllowedError')),
    enumerateDevices: () => Promise.resolve([]),
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => true,
  };
}

const LK_SDK_URL = 'https://cdn.jsdelivr.net/npm/livekit-client@2.17.3/dist/livekit-client.umd.min.js';"""

if old_sdk_const not in content:
    print("ERROR: Could not find LK_SDK_URL constant to patch")
    sys.exit(1)

content = content.replace(old_sdk_const, polyfill)
print("Added mediaDevices polyfill")

with open(sys.argv[1], 'w') as f:
    f.write(content)

print("Done - saved patched file")
