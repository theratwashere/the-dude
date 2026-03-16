#!/bin/bash
# Deploy script for The Dude on the Rat Mac mini
# Run this on the Rat: bash deploy.sh

set -e

cd ~/Projects/the-dude

echo "=== Pulling latest code ==="
git fetch origin
git checkout dude-2-polish
git pull origin dude-2-polish

echo "=== Installing dependencies ==="
pip3 install -r requirements.txt --user 2>/dev/null || pip3 install -r requirements.txt

echo "=== Stopping existing server ==="
pkill -f "uvicorn api_server:app" 2>/dev/null || true
sleep 2

echo "=== Starting server ==="
PYTHONUNBUFFERED=1 nohup python3 -m uvicorn api_server:app \
  --host 0.0.0.0 --port 8443 \
  --ssl-keyfile=tls.key --ssl-certfile=tls.crt \
  > /tmp/dude-server.log 2>&1 &

echo "Server PID: $!"
sleep 3

echo "=== Health check ==="
curl -sk https://127.0.0.1:8443/api/health && echo ""

echo ""
echo "=== Deploy complete ==="
echo "View logs: tail -f /tmp/dude-server.log"
echo "Access: https://100.77.205.27:8443"
