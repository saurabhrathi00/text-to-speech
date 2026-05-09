import os
import requests

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://192.168.1.10:11434/api/chat")
MODEL_NAME = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")

SYSTEM_PROMPT = """You are a text normalizer for Indian TTS (Text-to-Speech).
Convert the input to clean, speakable text for an Indian narrator.

STRICT RULES:
1. Roman Hindi words → convert to Devanagari script
2. English technical terms (photosynthesis, mitochondria, DNA, WiFi, etc.) → keep EXACTLY as is
3. Proper nouns, names, brand names (YouTube, Google, iPhone, etc.) → keep EXACTLY as is
4. Numbers → convert to Hindi words (5 → पाँच)
5. Fix grammar for natural, flowing speech
6. Do NOT translate any English words — only transliterate Roman Hindi
7. Output ONLY the normalized text, nothing else
8. PRESERVE THE INPUT 1:1 — do not deduplicate, do not summarize, do not
   shorten, do not remove repeated sentences or phrases. If the user
   pasted the same sentence 6 times, return all 6 sentences. Your job is
   ONLY script conversion + grammar; never change WHAT is being said or
   HOW MUCH is being said.
9. Ensure every sentence ends with proper punctuation (। for Hindi,
   . ! ? for English/general). If sentence enders are missing between
   sentences, add them — but never merge two sentences into one.

Examples:
Input:  "Aaj hum discuss karenge photosynthesis ke baare mein"
Output: "आज हम discuss करेंगे photosynthesis के बारे में।"

Input:  "YouTube pe 5 million subscribers hain"
Output: "YouTube पर पाँच million subscribers हैं।"

Input:  "mitochondria is the powerhouse of the cell"
Output: "mitochondria is the powerhouse of the cell."

Input:  "namaste dosto namaste dosto namaste dosto"
Output: "नमस्ते दोस्तों। नमस्ते दोस्तों। नमस्ते दोस्तों।"
"""


class OllamaError(Exception):
    pass


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
    return content
