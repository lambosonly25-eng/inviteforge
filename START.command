#!/bin/bash
# InviteForge — Start Everything
cd "$(dirname "$0")"
echo "🚀 Starting InviteForge..."

# Install deps if needed
cd backend
source /Users/pen9/lambosonlybot/venv/bin/activate
pip install -r requirements.txt -q

echo "✅ Starting backend on http://localhost:8080"
set -a && source .env && set +a
python main.py &

sleep 2

echo "✅ Opening InviteForge in browser..."
open http://localhost:8080

echo ""
echo "InviteForge is live at http://localhost:8080"
echo "Press Ctrl+C to stop."
wait
