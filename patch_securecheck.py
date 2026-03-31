#!/usr/bin/env python3
"""Fix hasMediaDevices check to use isSecureContext."""

import sys

with open(sys.argv[1], 'r') as f:
    content = f.read()

old_check = "    const hasMediaDevices = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);"
new_check = "    const hasMediaDevices = window.isSecureContext && !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);"

if old_check not in content:
    print("ERROR: Could not find hasMediaDevices check")
    sys.exit(1)

content = content.replace(old_check, new_check)
print("Fixed hasMediaDevices to include isSecureContext check")

with open(sys.argv[1], 'w') as f:
    f.write(content)

print("Done")
