#!/usr/bin/env bash
# ====================================================
# YouTube Narrator — Start server (Mac/Linux)
# ====================================================
set -e

if [ ! -d .venv ]; then
  echo "Virtual environment not found. Run ./setup.sh first."
  exit 1
fi

if [ ! -f .env ]; then
  echo ".env not found. Copy .env.example to .env and add your HF_TOKEN."
  exit 1
fi

source .venv/bin/activate

echo "Starting YouTube Narrator..."
echo "Open http://localhost:5000 in your browser"
echo "Phone (same WiFi): http://$(ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}'):5000"
echo ""
python app.py
