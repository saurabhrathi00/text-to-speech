#!/usr/bin/env bash
# ====================================================
# YouTube Narrator — One-time setup for Mac/Linux
# ====================================================
set -e

echo "[1/3] Creating virtual environment..."
python3 -m venv .venv

echo "[2/3] Upgrading pip..."
source .venv/bin/activate
python -m pip install --upgrade pip

echo "[3/3] Installing dependencies..."
pip install -r requirements.txt

if [ ! -f .env ]; then
  echo "Creating .env from template..."
  cp .env.example .env
  echo ""
  echo "  IMPORTANT: Edit .env and set your HF_TOKEN"
  echo "  Get token from: https://huggingface.co/settings/tokens"
  echo "  Also accept license at: https://huggingface.co/ai4bharat/indic-parler-tts"
fi

echo ""
echo "Setup complete! Run ./run.sh to start the server."
