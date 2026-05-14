"""All hard-coded knobs for the TTS pipeline live here.

Anything that's an env-overridable runtime setting (URLs, API keys,
host/port) stays in .env. Anything that's a tuneable constant or a
mapping/list/template lives in this file.
"""

# ──────────────────────────────────────────────────────────────────────
# Audio output
# ──────────────────────────────────────────────────────────────────────

# How many generated audio files to keep on disk (oldest beyond this
# count are pruned after each request).
MAX_AUDIO_FILES = 1

# Silence inserted between consecutive chunks when joining their audio.
INTER_CHUNK_SILENCE_SEC = 0.18


# ──────────────────────────────────────────────────────────────────────
# Provider list (must match keys in app routing + UI buttons)
# ──────────────────────────────────────────────────────────────────────

PROVIDERS = ("parler", "elevenlabs", "bark")


# ──────────────────────────────────────────────────────────────────────
# Parler-TTS
# ──────────────────────────────────────────────────────────────────────

PARLER_MODEL_ID = "ai4bharat/indic-parler-tts"

# Per-chunk token budget. Sized generously so a single long sentence
# has room to complete without hitting the model's default ~2580
# token cap.
PARLER_TOKENS_PER_CHAR = 8
PARLER_MIN_NEW_TOKENS = 256
PARLER_MAX_NEW_TOKENS_CAP = 5000

# Voice presets exposed to the UI.
PARLER_SPEAKERS = [
    {"id": "rohit", "label": "Rohit (deep male)"},
    {"id": "aman", "label": "Aman (young male)"},
    {"id": "divya", "label": "Divya (warm female)"},
    {"id": "rani", "label": "Rani (news anchor female)"},
]

# Phrase fragments used to build the description prompt.
PARLER_SPEAKER_DESCRIPTIONS = {
    "rohit": "deep, mature male",
    "aman": "energetic young male",
    "divya": "warm, clear female",
    "rani": "formal news-anchor female",
}

PARLER_SPEED_PHRASES = {
    "slow": "at a slow, deliberate pace",
    "moderate": "at a moderate, natural pace",
    "fast": "at a fast, brisk pace",
}

PARLER_PITCH_PHRASES = {
    "low": "with a low, deep pitch",
    "normal": "with a natural pitch",
    "high": "with a slightly higher pitch",
}

PARLER_EXPRESSIVITY_PHRASES = {
    "expressive": "in a very expressive, engaging tone like a professional documentary narrator",
    "neutral": "in a neutral, balanced tone",
    "calm": "in a calm, steady, soothing tone",
}

PARLER_EMOTION_PHRASES = {
    "none": "",
    "happy": "with a cheerful, upbeat, happy mood",
    "sad": "with a sad, melancholic, sorrowful mood",
    "angry": "with an angry, forceful, intense mood",
    "excited": "with enthusiastic, energetic, excited emotion",
    "fearful": "with a fearful, tense, hesitant mood",
    "whisper": "in a quiet, intimate, whispering voice",
    "serious": "in a serious, formal, authoritative mood",
}


# ──────────────────────────────────────────────────────────────────────
# Bark
# ──────────────────────────────────────────────────────────────────────

# ~13s of audio per Bark generation call. Keep chunks small enough to
# fit under that budget.
BARK_MAX_CHARS_PER_CHUNK = 200

BARK_VOICES = [
    {"id": "v2/hi_speaker_0", "label": "Hindi 0 (male)"},
    {"id": "v2/hi_speaker_1", "label": "Hindi 1 (female)"},
    {"id": "v2/hi_speaker_2", "label": "Hindi 2 (male)"},
    {"id": "v2/hi_speaker_3", "label": "Hindi 3 (female)"},
    {"id": "v2/hi_speaker_4", "label": "Hindi 4 (male)"},
    {"id": "v2/hi_speaker_5", "label": "Hindi 5 (female)"},
    {"id": "v2/en_speaker_6", "label": "English narrator (male)"},
    {"id": "v2/en_speaker_9", "label": "English narrator (female)"},
]

# Inline tags for Bark — produce the actual non-speech sound. Off by
# default; opt-in via BARK_USE_TAGS=1 env var.
BARK_EMOTION_TAGS = {
    "none": "",
    "happy": "[laughs]",
    "sad": "[sighs]",
    "excited": "[gasps]",
    "angry": "",
    "fearful": "[breathes shakily]",
    "whisper": "[whispers]",
    "serious": "",
}


# ──────────────────────────────────────────────────────────────────────
# ElevenLabs
# ──────────────────────────────────────────────────────────────────────

ELEVEN_API_BASE = "https://api.elevenlabs.io/v1"

# Curated multilingual voice list shown when /v1/voices fetch fails.
ELEVEN_CURATED_VOICES = [
    {"id": "pNInz6obpgDQGBDnXBQb", "label": "Adam (male, deep)"},
    {"id": "ErXwobaYiN019PkySvjV", "label": "Antoni (male, warm)"},
    {"id": "VR6AewLTigWG4xSOukaG", "label": "Arnold (male, narrator)"},
    {"id": "21m00Tcm4TlvDq8ikWAM", "label": "Rachel (female, calm)"},
    {"id": "EXAVITQu4vr4xnSDxMaL", "label": "Sarah (female, soft)"},
    {"id": "AZnzlk1XvdvUeBnXmlld", "label": "Domi (female, strong)"},
    {"id": "MF3mGyEYCl7XYWbV9V6O", "label": "Elli (female, young)"},
    {"id": "TxGEqnHWrfWFTfGW9XjX", "label": "Josh (male, deep)"},
]

# Emotion → ElevenLabs voice settings (stability + style).
# Lower stability + higher style = more expressive output.
ELEVEN_EMOTION_SETTINGS = {
    "none":     {"stability": 0.50, "style": 0.00},
    "happy":    {"stability": 0.30, "style": 0.75},
    "sad":      {"stability": 0.35, "style": 0.65},
    "excited":  {"stability": 0.20, "style": 0.95},
    "angry":    {"stability": 0.20, "style": 0.85},
    "fearful":  {"stability": 0.30, "style": 0.65},
    "whisper":  {"stability": 0.45, "style": 0.30},
    "serious":  {"stability": 0.65, "style": 0.20},
}

ELEVEN_DEFAULT_SIMILARITY_BOOST = 0.75

# Inline emotion tags for the v3 model only (v2 reads them literally).
ELEVEN_V3_EMOTION_TAGS = {
    "happy":   "[happy]",
    "sad":     "[sad]",
    "excited": "[excited]",
    "angry":   "[angry]",
    "fearful": "[scared]",
    "whisper": "[whispering]",
    "serious": "[serious]",
}


# ──────────────────────────────────────────────────────────────────────
# Whisper aligner
# ──────────────────────────────────────────────────────────────────────

# If whisper found more than this fraction of the expected word count,
# treat the tail as repetition and trim at expected_word_count.
WHISPER_REPETITION_RATIO = 1.3

# If whisper found less than this fraction, assume it missed words at
# the end and skip the trim entirely (preserve audio).
WHISPER_MIN_RATIO = 0.8

# Padding kept after the last detected word when trimming.
WHISPER_TAIL_PAD_SEC = 0.20

