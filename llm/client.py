"""Pure LLM transport layer.

Single entry point: `chat(system_prompt, user_text)` → str.

Dispatches to Gemini (default, production) or Ollama (local fallback)
based on `LLM_PROVIDER`. Callers should never reach in here directly —
go through the high-level helpers in `llm/__init__.py`.
"""
import time
import requests

from . import config


class LLMError(Exception):
    """Anything the upstream model can fail with — network, API error,
    blocked prompt, empty completion. Callers catch this one type."""


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────

def chat(system_prompt: str, user_text: str,
         temperature: float | None = None,
         timeout: int | None = None,
         keep_alive: str | int | None = None,
         provider: str | None = None) -> str:
    """One-shot chat completion. Returns the model's text.

    `provider` overrides config.LLM_PROVIDER for this single call —
    used by per-request role-based routing (e.g. free users on a
    LLM_PROVIDER=ollama box must still hit Gemini).

    `keep_alive` is Ollama-only. Pass "30s" / "5m" to keep the model
    resident in VRAM after the call (avoids cold-reload penalty when
    another call follows). None falls back to OLLAMA_KEEP_ALIVE config.
    Gemini ignores it.
    """
    temp = config.DEFAULT_TEMPERATURE if temperature is None else temperature
    effective = (provider or config.LLM_PROVIDER or "").lower()
    if effective == "gemini":
        return _gemini_chat(
            system_prompt, user_text,
            temperature=temp,
            timeout=timeout or config.GEMINI_TIMEOUT_SECONDS,
        )
    if effective == "ollama":
        return _ollama_chat(
            system_prompt, user_text,
            temperature=temp,
            timeout=timeout or config.OLLAMA_TIMEOUT_SECONDS,
            keep_alive=keep_alive if keep_alive is not None else config.OLLAMA_KEEP_ALIVE,
        )
    raise LLMError(f"unknown LLM provider={effective!r}")


# ──────────────────────────────────────────────────────────────────────
# Gemini
# ──────────────────────────────────────────────────────────────────────

def _gemini_chat(system_prompt: str, user_text: str,
                 temperature: float, timeout: int) -> str:
    if not config.GEMINI_API_KEY:
        raise LLMError("GEMINI_API_KEY not set")

    url = f"{config.GEMINI_API_BASE}/models/{config.GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": config.GEMINI_MAX_OUTPUT_TOKENS,
        },
    }

    t0 = time.time()
    try:
        r = requests.post(
            url,
            params={"key": config.GEMINI_API_KEY},
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )
    except requests.RequestException as e:
        raise LLMError(f"gemini network error: {e}") from e

    if r.status_code != 200:
        try:
            msg = r.json().get("error", {}).get("message", r.text[:300])
        except ValueError:
            msg = r.text[:300]
        raise LLMError(f"gemini API {r.status_code}: {msg}")

    try:
        data = r.json()
    except ValueError as e:
        raise LLMError(f"gemini bad JSON: {e}") from e

    candidates = data.get("candidates") or []
    if not candidates:
        blocked = data.get("promptFeedback", {}).get("blockReason")
        if blocked:
            raise LLMError(f"gemini prompt blocked: {blocked}")
        raise LLMError("gemini returned no candidates")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        finish = candidates[0].get("finishReason")
        raise LLMError(f"gemini empty completion (finishReason={finish})")

    print(f"[llm:gemini] {config.GEMINI_MODEL} → {len(text)} chars in {time.time() - t0:.1f}s")
    return text


# ──────────────────────────────────────────────────────────────────────
# Ollama (local dev fallback)
# ──────────────────────────────────────────────────────────────────────

def _ollama_chat(system_prompt: str, user_text: str,
                 temperature: float, timeout: int,
                 keep_alive: str | int) -> str:
    payload = {
        "model": config.OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "options": {"temperature": temperature},
        "keep_alive": keep_alive,
    }
    t0 = time.time()
    try:
        r = requests.post(config.OLLAMA_URL, json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise LLMError(f"ollama network error: {e}") from e

    try:
        data = r.json()
    except ValueError as e:
        raise LLMError(f"ollama bad JSON: {e}") from e

    text = (data.get("message", {}).get("content") or "").strip()
    if not text:
        raise LLMError("ollama returned empty content")
    print(f"[llm:ollama] {config.OLLAMA_MODEL} → {len(text)} chars in {time.time() - t0:.1f}s")
    return text
