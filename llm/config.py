"""LLM module config. Reads ONLY env vars in the LLM_/GEMINI_/OLLAMA_
namespace. No other module should reach in here — use the public API
in `llm/__init__.py` instead.
"""
import os


# ─── Provider selection ───────────────────────────────────────────────
# "gemini" (default, production) or "ollama" (local dev fallback).
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "gemini").strip().lower()


# ─── Gemini ───────────────────────────────────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_API_BASE = os.getenv(
    "GEMINI_API_BASE",
    "https://generativelanguage.googleapis.com/v1beta",
)
GEMINI_TIMEOUT_SECONDS = int(os.getenv("GEMINI_TIMEOUT_SECONDS", "120"))
GEMINI_MAX_OUTPUT_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192"))


# ─── Ollama (local Qwen) — only used when LLM_PROVIDER=ollama ─────────
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.10:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("OLLAMA_TIMEOUT_SECONDS", "300"))
# How long Ollama keeps the model resident after a call. Default "0"
# unloads immediately (good when ComfyUI also wants the GPU). Bump to
# "5m" if Qwen is the only thing on the box and back-to-back requests
# matter more than VRAM headroom.
OLLAMA_KEEP_ALIVE = os.getenv("OLLAMA_KEEP_ALIVE", "0")


# ─── Generation knobs (shared) ────────────────────────────────────────
DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
