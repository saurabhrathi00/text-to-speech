# CLAUDE.md — context for AI assistants editing this repo

## What this is

SastaSpeech — Flask-based Hindi TTS SaaS. Two-deployment model:
- **Local box** (admin): Parler + Whisper + Ollama/Qwen
- **Cloud** (paying users): ElevenLabs + Gemini

Same code, same Supabase DB, different `.env`. See [docs/DEPLOY.md](docs/DEPLOY.md).

## Hard rules

- **Never hardcode** provider names, plan limits, or display strings in code. Source of truth:
  - Providers → `config/providers.json` (+ DB `plan_limits.{llm,tts}_providers`)
  - Plans / pricing → `plan_limits` table
  - LLM prompts → `llm/prompts/*.md`
- **Admin promotion is env-only.** `ADMIN_EMAILS` in `.env`. No API path mutates `role`.
- **Service-role key never reaches frontend.** Only `SUPABASE_ANON_KEY` goes in templates.
- **Don't commit `.env`.** Use `.env.example` placeholders.

## Project conventions

- **Hindi/Hinglish UI strings.** Code identifiers + comments in English.
- **Provider names** never appear in user-visible UI when user has only 1 option (avoid "powered by ElevenLabs" leaks). Toggle row hides when ≤1 allowed.
- **Heavy ML modules** (`tts_engine`, `bark_tts`, `aligner`, `image_gen`) are **lazy-imported** in `app.py`. Cloud build skips them. Always guard with `if X is None: return 503`.
- **No emojis in commits** unless user asks.
- **No new files unless needed.** Prefer editing existing ones.

## Where things live

```
app.py            → Flask routes + helpers (will be split into routes/ later)
auth.py           → JWT + profiles + plans + quota + upgrades (will be split into auth/ later)
security.py       → rate limits, CORS, body-size, suspicious flags
audio_storage.py  → Supabase Storage, per-user folders, signed URLs
normalizer.py     → Devanagari guard + sentence splitting (app-side LLM wrapper)
llm/              → ISOLATED. Only stdlib + requests. Read llm/README.md
config.py         → Python config (PROVIDERS, PARLER_SPEAKERS, ElevenLabs defaults)
config/providers.json → declarative provider metadata (frontend + backend)
db/schema.sql     → idempotent (uses ALTER…IF NOT EXISTS)
scripts/          → run.sh / run.bat / setup.sh / setup.bat (callable from anywhere; auto cd to repo root)
docs/             → architecture.md, INSTALL.md, DEPLOY.md
static/ templates/ → Flask convention paths — do NOT move
```

## Patterns to follow

- **New endpoint**: decorate with `@auth.require_user` (or `require_admin`) + `@security.require_json` + `@security.rate_limit(...)`.
- **New provider**: add row in `config/providers.json` + `plan_limits.{llm,tts}_providers`. No code change.
- **New plan tier**: insert row in `plan_limits`. UI picker auto-updates via `/api/plans`.
- **DB schema change**: add as `ALTER TABLE … ADD COLUMN IF NOT EXISTS …` block. Don't rewrite the table.
- **Effective plan / limits**: always go through `auth.get_effective_plan()` and `auth.get_effective_limits()`. Never read `profile.plan` directly for enforcement.

## Anti-patterns (don't do)

- New service / repository / DTO layers
- ORM / SQLAlchemy
- FastAPI migration
- Hardcoded provider names in `if provider == 'gemini'` style branches outside `llm/client.py`
- New top-level `let`/`const` in `templates/index.html` after the `await loadProviderRegistry()` line (TDZ)
- Adding `role` to writable PATCH fields

## When in doubt

- Check the user has the relevant plan capability via `check_limits` + `get_allowed_providers` before doing any LLM/TTS work.
- Errors that mention a provider must name the *actual* provider that failed (use `_llm_error_message()`), not a hardcoded one.
- After a successful generation: `auth.log_usage()` + `auth.consume_bonus_if_used()` + `audio_storage.prune_user_audio()`.
