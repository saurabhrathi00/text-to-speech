"""Google Gemini LLM provider — alternative to local Ollama/Qwen.

Used in cloud/production mode where a managed API is preferred over a
local model. Same chat-completion interface as the Ollama path so
callers can swap providers via the LLM_PROVIDER env var.
"""
import os
import json
import time
import requests

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")


class GeminiError(Exception):
    pass


def is_configured() -> bool:
    return bool(os.getenv("GEMINI_API_KEY"))


def chat(system_prompt: str, user_text: str, timeout: int = 120,
          temperature: float = 0.2, model: str | None = None) -> str:
    """Single-turn chat completion against Gemini. Returns the model's
    text output. Raises GeminiError on any failure.
    """
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise GeminiError("GEMINI_API_KEY not set in .env")

    model = model or DEFAULT_MODEL
    url = f"{API_BASE}/models/{model}:generateContent?key={api_key}"

    payload = {
        "contents": [
            {"role": "user", "parts": [{"text": user_text}]}
        ],
        "systemInstruction": {
            "parts": [{"text": system_prompt}]
        },
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": 8192,
        },
    }

    t0 = time.time()
    try:
        r = requests.post(url, json=payload, timeout=timeout,
                          headers={"Content-Type": "application/json"})
    except requests.RequestException as e:
        raise GeminiError(f"network error: {e}") from e

    if r.status_code != 200:
        try:
            err_body = r.json()
            err = err_body.get("error", {})
            msg = err.get("message", str(err_body))
        except ValueError:
            msg = r.text[:300]
        raise GeminiError(f"API error {r.status_code}: {msg}")

    try:
        data = r.json()
    except ValueError as e:
        raise GeminiError(f"bad JSON response: {e}") from e

    candidates = data.get("candidates") or []
    if not candidates:
        block = data.get("promptFeedback", {}).get("blockReason")
        if block:
            raise GeminiError(f"prompt blocked: {block}")
        raise GeminiError(f"no candidates returned: {data}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        finish = candidates[0].get("finishReason")
        raise GeminiError(f"empty completion (finishReason={finish})")

    print(f"[gemini] {model} → {len(text)} chars in {time.time() - t0:.1f}s")
    return text
