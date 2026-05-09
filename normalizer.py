import os
import re
import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.10:11434/api/chat")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

SYSTEM_PROMPT = """You are a TEXT FORMATTER for an Indian TTS (Text-to-Speech) narrator.

YOUR GOAL:
  Turn the user's text into a form that is
    (a) EASY for an Indian narrator to pronounce, and
    (b) NATURAL-sounding when read aloud,
  WITHOUT changing what is being said.
  You change HOW the text looks (script + punctuation), not the WORDS.

THE PRINCIPLE (use this for every decision, including any case not
explicitly mentioned below):

  Before making a change, ask yourself: "does this make the text
  easier to pronounce, or the resulting speech more natural?"
  - If yes → make the change.
  - If it only makes the text look 'cleaner' or 'more correct' on the
    page but doesn't help the audio → do not make the change.
  - If you are unsure → do not make the change.

  Apply this principle to anything: script choice, punctuation, pauses,
  hyphens vs spaces, nukta marks, matra placement, anything. You do not
  need a rule for every case — reason from this principle.

ABSOLUTE FORBIDDEN (these are not pronunciation decisions, they are
content changes — never do them):

  1. Do not ADD any content. No new sentences, summaries, morals,
     headings, conclusions, or filler the user did not write.
  2. Do not REMOVE any content. No deduplication, no shortening, no
     dropping of repeated sentences.
  3. Do not SUBSTITUTE one word for another. A small fix that keeps
     the same base consonants (matra adjustment, nukta) is OK; replacing
     a word with a different word is not.
  4. Do not paraphrase, reword, simplify, or reorder words.
  5. Do not translate between languages — only transliterate Roman
     Hindi to Devanagari.
  6. Output ONLY the formatted text. No explanations, no quotes around
     it, no commentary.

When in doubt about ANY transformation, leave it alone. The output
should read as the user's exact text, only dressed up for clearer audio.
"""


class OllamaError(Exception):
    pass


# If Qwen's output preserves at least this fraction of input Devanagari
# words, we accept it. Below this, we assume Qwen rewrote too much and
# fall back to the input. Tuned to allow small per-word fixes (nukta,
# typo correction) while catching wholesale substitutions or drops.
DEVANAGARI_PRESERVE_THRESHOLD = 0.85


def _devanagari_words(text: str) -> list[str]:
    return re.findall(r"[ऀ-ॿ]+", text)


def _verify_devanagari_preserved(input_text: str, output_text: str) -> bool:
    in_words = _devanagari_words(input_text)
    if not in_words:
        return True
    out_set = set(_devanagari_words(output_text))
    preserved = sum(1 for w in in_words if w in out_set)
    ratio = preserved / len(in_words)
    if ratio < DEVANAGARI_PRESERVE_THRESHOLD:
        missing = [w for w in in_words if w not in out_set][:8]
        print(f"[normalizer] only {preserved}/{len(in_words)} ({ratio:.0%}) "
              f"Devanagari words preserved — examples missing: {missing}")
        return False
    return True


def normalize_text(text: str, timeout: int = 120) -> str:
    payload = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
    }
    try:
        r = requests.post(OLLAMA_URL, json=payload, timeout=timeout)
        r.raise_for_status()
    except requests.RequestException as e:
        raise OllamaError(str(e)) from e

    data = r.json()
    content = data.get("message", {}).get("content", "").strip()
    if not content:
        raise OllamaError("Empty response from Qwen")

    # Safety net: if Qwen modified or dropped any Devanagari word that was
    # in the original input, fall back to the input text. We trust Qwen on
    # script conversion (Roman → Devanagari) and on punctuation, but not
    # on rewriting Devanagari words.
    if not _verify_devanagari_preserved(text, content):
        print("[normalizer] falling back to original input (no Qwen edits applied)")
        return text

    return content
