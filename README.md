# YouTube Narrator TTS App — Requirements

## Project Overview
A local web application for a YouTube content creator who types scripts in Hindi, Hinglish, or English — and gets back a professional Indian narrator voice audio file. Fully offline, no API keys, runs on local machine.

---

## Tech Stack
- **Backend:** Python + Flask
- **LLM:** Qwen 2.5 7B via Ollama (running at `http://192.168.1.13:11434`)
- **TTS:** Indic Parler-TTS (by AI4Bharat)
- **Frontend:** HTML + CSS + Vanilla JS (single page, browser-based)
- **Audio output:** `.wav` file, downloadable

---

## Hardware
- OS: Windows
- GPU: NVIDIA RTX 3060 12GB VRAM
- RAM: 16GB

---

## Core Workflow

```
User types script (Hindi / Hinglish / English)
        ↓
Qwen normalizes text:
  - Roman Hindi → Devanagari Hindi
  - English technical/scientific words → keep AS IS
  - Proper nouns, brand names → keep AS IS
  - Numbers → Hindi spelled out form
  - Fix grammar for natural speech
        ↓
Indic Parler-TTS generates audio
  - Indian narrator voice
  - Clear, professional, expressive
        ↓
User previews and downloads .wav file
```

---

## Text Normalization Rules (Qwen)

**IMPORTANT:** Qwen's ONLY job is to make the text TTS-friendly. It should NOT translate English words.

### Qwen System Prompt:
```
You are a text normalizer for Indian TTS (Text-to-Speech).
Convert the input to clean, speakable text for an Indian narrator.

STRICT RULES:
1. Roman Hindi words → convert to Devanagari script
2. English technical terms (photosynthesis, mitochondria, DNA, WiFi, etc.) → keep EXACTLY as is
3. Proper nouns, names, brand names (YouTube, Google, iPhone, etc.) → keep EXACTLY as is
4. Numbers → convert to Hindi words (5 → पाँच)
5. Fix grammar for natural, flowing speech
6. Do NOT translate any English words — only transliterate Roman Hindi
7. Output ONLY the normalized text, nothing else

Examples:
Input:  "Aaj hum discuss karenge photosynthesis ke baare mein"
Output: "आज हम discuss करेंगे photosynthesis के बारे में"

Input:  "YouTube pe 5 million subscribers hain"
Output: "YouTube पर पाँच million subscribers हैं"

Input:  "mitochondria is the powerhouse of the cell"
Output: "mitochondria is the powerhouse of the cell"
```

---

## TTS Voice Description (Indic Parler-TTS)

```python
voice_description = """
Arjun speaks in clear, natural Hindi with a warm Indian accent. 
His voice is expressive, confident, and engaging like a professional 
YouTube narrator. The recording is studio quality, close-sounding, 
with no background noise.
"""
```

---

## API Endpoints (Flask Backend)

### `POST /generate`
Request:
```json
{
  "text": "Aaj hum baat karenge photosynthesis ke baare mein"
}
```
Response:
```json
{
  "normalized_text": "आज हम बात करेंगे photosynthesis के बारे में",
  "audio_url": "/audio/output_1234.wav"
}
```

### `GET /audio/<filename>`
Returns the generated `.wav` audio file for preview/download.

---

## Frontend UI

### Layout (Single Page):
```
┌─────────────────────────────────────────┐
│  🎙️  YouTube Narrator                   │
│       Papa ke liye — Local & Free       │
├─────────────────────────────────────────┤
│                                         │
│  [Large textarea]                       │
│  Hindi / Hinglish / English             │
│  sab type kar sakte hain               │
│                                         │
├─────────────────────────────────────────┤
│  [ 🎵 Awaaz Banao ]   <- main button    │
├─────────────────────────────────────────┤
│  Normalized text preview (collapsible)  │
├─────────────────────────────────────────┤
│  ▶️  Audio Player (preview)             │
│  📥  Download .wav button              │
└─────────────────────────────────────────┘
```

### UI Requirements:
- Clean, simple interface — easy for non-technical user (papa)
- Large textarea (minimum 10 rows)
- Loading spinner/animation while processing
- Show normalized text so user can verify before listening
- Audio player for preview before downloading
- Download button for final .wav
- Error messages in Hindi (e.g., "कुछ गलत हो गया, दोबारा कोशिश करें")
- Works on same WiFi network (accessible via `http://192.168.1.13:5000`)

---

## Python Dependencies

```
flask
torch
torchaudio
transformers
parler-tts
soundfile
requests
```

Install command:
```bash
pip install flask torch torchaudio transformers parler-tts soundfile requests
```

---

## Project Structure

```
youtube-narrator/
├── app.py              # Flask backend
├── tts_engine.py       # Indic Parler-TTS logic
├── normalizer.py       # Qwen text normalization via Ollama API
├── templates/
│   └── index.html      # Frontend UI
├── static/
│   └── style.css       # Styles
├── audio/              # Generated audio files (temp)
└── requirements.txt
```

---

## Important Notes for Implementation

1. **Ollama API URL:** `http://192.168.1.13:11434/api/chat` — Qwen is already running here
2. **Model name:** `qwen2.5:7b`
3. **Indic Parler-TTS model:** `ai4bharat/indic-parler-tts`
4. **Language code for Hindi:** `hi`
5. **Audio files:** Save to `/audio/` folder, serve via Flask static route
6. **GPU acceleration:** Use `cuda` device for TTS if available (RTX 3060)
7. **Long scripts:** Handle chunking if text > 500 characters (split on sentence boundaries, merge audio)
8. **Cleanup:** Auto-delete old audio files after 1 hour to save disk space

---

## Error Handling

- Ollama not reachable → show message "Qwen server se connect nahi ho paya"
- TTS fails → show message "Awaaz generate nahi ho payi, dobara try karein"
- Empty input → show message "Pehle kuch type karein"
- GPU OOM → fallback to CPU automatically

---

## What Success Looks Like

Papa types a YouTube script in any mix of Hindi/Hinglish/English.
Clicks one button.
Hears a clear, professional Indian narrator voice reading the script back.
Downloads the audio and uses it in his YouTube video.
Zero cost. Zero internet needed. Zero API keys.
```