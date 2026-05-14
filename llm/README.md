# llm/ — isolated LLM module

Everything that talks to a language model lives here. Nothing else
in the codebase imports or instantiates an LLM client directly.

## Isolation contract

Code inside `llm/` MUST follow these rules:

1. **No project imports.** Only Python stdlib and `requests` are allowed.
   `import auth`, `import app`, `from config import ...` etc. are
   forbidden. The LLM has no business knowing about users, DB,
   filesystem layout, or any other concern.

2. **Env vars are namespaced.** Only `LLM_*`, `GEMINI_*`, and `OLLAMA_*`
   env vars are read, and only from `llm/config.py`. Never read auth
   keys, DB credentials, or anything else.

3. **Files are local-only.** The only files this module reads are
   `llm/prompts/*.md`. No `open()` outside this folder.

4. **Pure interface.** Every public function takes plain inputs
   (strings, lists, dicts) and returns plain outputs. No callbacks
   into the rest of the app. No side effects except outbound HTTP.

5. **Prompts are data, not code.** All prompts live in `llm/prompts/`
   as plain markdown. To change LLM behavior, edit the prompt — not
   the Python.

## Public API

```python
from llm import refine_for_tts, classify_emotions, generate_scene_prompts

# Pass 1: normalize Hindi/Hinglish for an Indian TTS narrator
refined = refine_for_tts("Hindi/Hinglish text here")

# Pass 2: classify emotion per sentence; returns list[str | None] of
# the same length as input
tags = classify_emotions(["sentence 1", "sentence 2", ...])

# Scene-prompt generator for image generation
scenes = generate_scene_prompts("longer story text")
```

## Switching providers

`LLM_PROVIDER=gemini` (default) or `LLM_PROVIDER=ollama`. The choice
lives in `llm/config.py`. Callers never know which provider answered.

## What's deliberately NOT here

- No auth, no rate limiting, no quota — those are app concerns
  (see `auth.py`).
- No Devanagari sanity checking — that lives in `normalizer.py`
  because it's an app-side guard against bad LLM output.
- No retries with backoff yet — add when needed.
