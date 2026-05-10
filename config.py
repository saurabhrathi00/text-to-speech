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


# ──────────────────────────────────────────────────────────────────────
# Qwen normalizer
# ──────────────────────────────────────────────────────────────────────

QWEN_TIMEOUT_SECONDS = 120
QWEN_TEMPERATURE = 0.2

# Appended to the base prompt only when the target TTS is ElevenLabs.
# ElevenLabs's v3 model reads inline emotion tags as direction (not as
# literal speech), so we ask Qwen to inject tags at points where the
# narrative clearly suggests an emotion shift. For other providers
# (Parler, Bark) this addendum is omitted because they would speak
# the bracketed words literally or treat them as noise.
# Bark's inline tags produce the literal non-speech sound, so they're
# more "intrusive" than ElevenLabs's direction-style tags. We inject a
# narrower subset and only when BARK_USE_TAGS=1 is set in .env.
BARK_EMOTION_TRIGGERS = [
    (r"रोना|रो रही|रो रहा|आँसू|सिसकी|दिल हिला देने वाली|cry\b|sob",
     "[crying]"),
    (r"हँसी|ठहाका|खिलखिला|हँसकर|हँसते हुए|laugh",
     "[laughs]"),
    (r"फुसफुसा|धीरे से कहा|कान में कहा|whisper|murmur",
     "[whispers]"),
    (r"आह भर|गहरी साँस|ठंडी आह|sighed",
     "[sighs]"),
    (r"हाँफते हुए|हाँफते-हाँफते|gasped",
     "[gasps]"),
    (r"मुस्कुराते हुए|खुशी से कहा",
     "[smiles]"),
    (r"साफ़ करते हुए गला|cleared throat",
     "[clears throat]"),
]


# Deterministic regex-based emotion tag injector. When the target is
# ElevenLabs, after Qwen normalizes the text, we run these patterns over
# the output and insert the matching tag right before each trigger.
# Order matters — earlier entries win when multiple match the same span.
# Each entry: (compile-able pattern, tag, optional comment)
ELEVEN_EMOTION_TRIGGERS = [
    (r"रोना|रो रही|रो रहा|आँसू|सिसकी|तड़पना|वेदना|दिल हिला देने वाली|दर्द से कराह|cry\b|sob|tears|weeping",
     "[crying]"),
    (r"हँसी|ठहाका|खिलखिला|हँसकर|हँसते हुए|laugh|chuckle|giggle",
     "[laughs]"),
    (r"फुसफुसा|धीरे से कहा|कान में कहा|चुपके से बोला|whisper|murmur",
     "[whispers]"),
    (r"चिल्लाया|चिल्लाई|गरजा|गरजी|ज़ोर से बोला|चीख पड़ा|चीख पड़ी|shouted|yelled",
     "[shouting]"),
    (r"उत्साह से|चहक उठा|खुशी से उछल|excited|thrilled",
     "[excited]"),
    (r"हाँफते हुए|हाँफते-हाँफते|साँस फूल|स्तब्ध रह|gasped|breathless",
     "[gasps]"),
    (r"गुस्से|क्रोधित|नाराज़ होकर|angrily|furious",
     "[angry]"),
    (r"दुखी|उदास|मायूस|गमगीन|बचेगी नहीं|बच पाएगी नहीं|sadly|sorrowfully",
     "[sad]"),
    (r"हिचकिचा|रुक-रुककर|अटक-अटककर|hesitant",
     "[hesitant]"),
    (r"आह भर|गहरी साँस|ठंडी आह|sighed",
     "[sighs]"),
    (r"मुस्कुराते हुए|खुशी से कहा|happy|joyful",
     "[happy]"),
    (r"गंभीर स्वर|संजीदगी से|solemnly",
     "[serious]"),
]


QWEN_EMOTION_TAG_PROMPT = """YOU MUST insert ElevenLabs emotion tags into Hindi/Hinglish/English
text. This is your ONLY job. Output without tags is FAILURE.

Available tags:
  [crying] [laughs] [sighs] [whispers] [excited] [happy] [sad]
  [angry] [shouting] [gasps] [breathless] [hesitant] [serious]

WHENEVER you see ANY of these phrases in the input, you MUST insert
the matching tag IMMEDIATELY BEFORE that phrase. Do not skip. Do not
think about whether it's needed. If a trigger appears, you tag it.

TRIGGER → TAG (mandatory):

  रोना | रो रही | रो रहा | आँसू | सिसकी | तड़पना | वेदना |
  "दिल हिला देने वाली" | दर्द से | cry | sob | tears | weeping
    → [crying]

  हँसी | ठहाका | खिलखिलाना | "हँसकर बोला/कहा" | laugh | chuckle
    → [laughs]

  आह | "गहरी साँस" | "ठंडी आह" | sighed
    → [sighs]

  फुसफुसाया | "धीरे से कहा" | "कान में कहा" | "चुपके से" |
  whispered | murmured
    → [whispers]

  "उत्साह से" | "चहक उठा" | "खुशी से उछल" | excitedly | thrilled
    → [excited]

  खुशी | "मुस्कुराते हुए" | happy | joyful
    → [happy]

  दुखी | उदास | मायूस | "गमगीन" | "बचेगी नहीं" / hopeless news |
  sadly | sorrowfully
    → [sad]

  गुस्से | क्रोधित | "नाराज़ होकर" | angrily | furious
    → [angry]

  चिल्लाया | गरजा | "ज़ोर से बोला" | "चीख पड़ा" | shouted | yelled
    → [shouting]

  "हाँफते हुए" | "साँस फूल" | "स्तब्ध रह" | gasped
    → [gasps]

  "हाँफते-हाँफते" | out of breath
    → [breathless]

  "हिचकिचाते हुए" | "रुक-रुककर" | "अटक-अटककर" | hesitantly
    → [hesitant]

  "गंभीर स्वर" | "संजीदगी से" | solemnly
    → [serious]

DO NOT change any words. DO NOT add or remove sentences. Tags are
the ONLY new content. Output ONLY the tagged text — no explanations.

If a sentence has no trigger from the list, leave it untouched.
Pure scene description (weather, setting, darkness, silence) gets
NO tag.

EXAMPLES (note how tags are placed RIGHT BEFORE the emotional phrase):

Input:  रह-रहकर उसके मुँह से ऐसी दिल हिला देने वाली आवाज़ निकलती थी।
Output: रह-रहकर उसके मुँह से [crying] ऐसी दिल हिला देने वाली आवाज़ निकलती थी।

Input:  घीसू ने कहा—"मालूम होता है, बचेगी नहीं।"
Output: घीसू ने कहा—[sad] "मालूम होता है, बचेगी नहीं।"

Input:  वह तड़पना और हाथ-पाँव पटकना नहीं देखा जाता।
Output: वह [crying] तड़पना और हाथ-पाँव पटकना नहीं देखा जाता।

Input:  वह हँसते हुए बोला—"अरे यार!"
Output: वह हँसते हुए बोला—[laughs] "अरे यार!"

Input:  जाड़ों की रात थी, सारा गाँव अंधकार में लय हो गया था।
Output: जाड़ों की रात थी, सारा गाँव अंधकार में लय हो गया था।

Now insert tags into the input below. REMEMBER: every trigger phrase
gets its tag. Output ONLY the tagged text."""


QWEN_ELEVENLABS_EMOTION_ADDENDUM = """

ADDITIONAL TASK — TARGET TTS IS ELEVENLABS:

The output will be spoken by ElevenLabs, which supports inline emotion
tags as DIRECTIONS to the voice (the tags themselves are not spoken).
Insert ONE tag immediately before any phrase that conveys a strong
emotional moment. These tags improve the audio significantly — when
you see a clear cue, ADD the tag. Don't be too sparing.

TRIGGER PHRASES → TAG mapping (add tag whenever you see these cues):

  [crying]      ← any mention of: रोना, रो रही/रहा, आँसू, सिसकी, तड़पना,
                  वेदना, दर्द से कराहना, "दिल हिला देने वाली आवाज़",
                  cry, sob, tears, weeping
  [laughs]      ← हँसी, ठहाका, खिलखिलाना, मुस्कुराते हुए कहा, laugh,
                  chuckle, giggle, "हँसकर बोला"
  [sighs]       ← आह, "गहरी साँस ली", "आह भरकर", sighed, "ठंडी आह"
  [whispers]    ← फुसफुसाया, "धीरे से कहा", "कान में कहा", "चुपके से बोला",
                  whispered, murmured
  [excited]     ← "उत्साह से", "चहक उठा", "खुशी से उछल पड़ा", excitedly,
                  thrilled
  [happy]       ← खुशी से, "मुस्कुराते हुए", happy, joyful (less intense
                  than [excited])
  [sad]         ← दुखी, उदास, "मायूस होकर", "गमगीन आवाज़ में", "बचेगी नहीं"
                  ya kisi tragic news ka delivery, sadly, sorrowfully
  [angry]       ← गुस्से से, क्रोधित, "नाराज़ होकर", angrily, furious
  [shouting]    ← चिल्लाया, गरजा, "ज़ोर से बोला", "चीख पड़ा", shouted, yelled
  [gasps]       ← "हाँफते हुए", "साँस फूल गई", "स्तब्ध रह गया", gasped
  [breathless]  ← "हाँफते-हाँफते", out of breath, exhausted speech
  [hesitant]    ← "हिचकिचाते हुए", "रुक-रुककर", "अटक-अटककर", hesitantly
  [serious]     ← "गंभीर स्वर में", "संजीदगी से", solemnly

Rules:
  1. Tag goes IMMEDIATELY BEFORE the emotional phrase, not at sentence
     start by default. Place it at the exact point where the emotion
     begins.
  2. Pure description / narration / atmospheric prose (weather, scene
     setting, time of day) gets NO tag. Tags are for VOCAL emotion only.
  3. ONE tag per emotional moment — don't stack [sad][crying] together.
  4. If two emotional moments appear in the same sentence, you may use
     two different tags at their respective positions.
  5. When you see a trigger from the table above, ADD the tag — don't
     skip it. The user wants emotional audio.
  6. Everything else (don't change words, don't dedupe, don't summarize)
     still applies. Tags are the ONLY new content allowed.

Examples:

Input:  "तो मुझसे तो उसका तड़पना और हाथ-पाँव पटकना नहीं देखा जाता।"
Output: "तो मुझसे [crying] तो उसका तड़पना और हाथ-पाँव पटकना नहीं देखा जाता।"

Input:  "रह-रहकर उसके मुँह से ऐसी दिल हिला देने वाली आवाज़ निकलती थी।"
Output: "रह-रहकर उसके मुँह से [crying] ऐसी दिल हिला देने वाली आवाज़ निकलती थी।"

Input:  "घीसू ने कहा—'मालूम होता है, बचेगी नहीं।'"
Output: "घीसू ने कहा—[sad] 'मालूम होता है, बचेगी नहीं।'"

Input:  "जाड़ों की रात थी, सारा गाँव अंधकार में लय हो गया था।"
Output: "जाड़ों की रात थी, सारा गाँव अंधकार में लय हो गया था।"
(NO tag — pure scene description, no vocal emotion.)

Input:  "वह हँसते हुए बोला—'अरे यार, क्या बात है!'"
Output: "वह हँसते हुए बोला—[laughs] 'अरे यार, क्या बात है!'"
"""


QWEN_SYSTEM_PROMPT = """You are a TEXT FORMATTER for an Indian TTS (Text-to-Speech) narrator.

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
  - If it only makes the text 'cleaner' or 'more correct' on the page
    but doesn't help the audio → do not make the change.
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
