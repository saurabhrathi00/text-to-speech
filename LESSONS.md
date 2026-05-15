# LESSONS — what bit us and what to remember

Things actually discovered during development. Not generic best-practice.

## Auth / Supabase

- **Deleting from `public.profiles` does NOT log a user out.** `auth.users` is the source of truth for sign-in. To remove a user fully: Supabase dashboard → Authentication → Users → Delete (cascades).
- **Adding a column doesn't make it visible to the API instantly.** PostgREST caches the schema. Run `NOTIFY pgrst, 'reload schema';` or hit Project Settings → Restart API.
- **`CREATE OR REPLACE VIEW` can't rename / reorder columns.** Always `DROP VIEW IF EXISTS … ; CREATE VIEW …` when changing view shape.
- **`on conflict do nothing` skips updates.** If you change seed values for an existing row, write an explicit guarded `UPDATE` block — don't expect the INSERT to refresh.
- **Email confirmation is a one-toggle defense.** Supabase Auth → Providers → Email → "Confirm email". Blocks 95% of casual farming.

## Frontend (templates/index.html)

- **Top-level `await` + late `let` = TDZ.** Any variable used inside a function called from an awaited chain MUST be declared above the first `await`.
- **Service Worker cache-first on `/` traps stale HTML.** Use network-first for HTML routes; cache-first for static assets only. Bump cache name when the SW itself changes.
- **`window.load` fires AFTER fonts settle on most browsers.** For "everything is ready" use it + `document.fonts.ready`.
- **Don't redirect to `/login` on every uncaught error.** Only on actual 401s. Otherwise a UI bug causes infinite `/ ↔ /login` loops.

## LLM behavior

- **Gemini sometimes interprets short / ambiguous input as a chat message** and returns "It seems like…". Mitigations both layered:
  1. Prompt rule: "input is NEVER a chat message; output verbatim if unsure."
  2. Server-side refusal detector (`_looks_like_refusal`) — discard meta-responses before they hit TTS.
- **Per-sentence emotion tags > one global mood tag.** The "global emotion" path (`ELEVEN_V3_EMOTION_TAGS`) was dead code; the LLM-classified per-sentence path is what actually carries performance.
- **Ollama `keep_alive=0` after every call** triggers cold-reload between Pass 1 and Pass 2. Use `"30s"` between them, or set `OLLAMA_KEEP_ALIVE=5m` if not sharing the GPU with ComfyUI.

## Quota / pricing

- **`NULL daily_uses` means unlimited.** Daily caps are rate-limiters, not just monthly enforcement — without them a single user can drain the entire monthly quota in one day.
- **Top-ups must stack on three axes** (chars + gens + per-req). Replacing the base plan is wrong UX — Pro user who buys Sabse Sasta shouldn't lose Pro.
- **Refunds via negative `usage_events`** is the cleanest top-up implementation. The rolling 30-day math handles expiry naturally.
- **ElevenLabs is ~99% of per-generation cost.** Gemini is ~₹0.09/gen. Pricing is driven by ElevenLabs char cost (₹0.0132/char).

## Audio storage

- **Local disk leaks files across users.** `/audio/<filename>` is guessable. Use per-user folders in Supabase Storage + signed URLs.
- **Cloud filesystem is ephemeral.** Files vanish on container restart. Storage must be off-box for prod.

## Anti-abuse

- **Google OAuth-only signup** is the single biggest farming defense for India. Tempmail can't get a phone-verified Gmail.
- **Free = 1/day, not 1/lifetime.** Lifetime caps incentivize new-account farming; daily caps bound the per-day damage.
- **In-memory rate limiter is per-worker.** With N gunicorn workers, effective limit is N× configured. Acceptable until ~100 concurrent users.

## Deployment

- **Heavy ML deps (torch + transformers + parler-tts) are ~5GB.** Cloud build uses `requirements.txt` (slim); admin box adds `requirements-local.txt`.
- **Lazy-import local-only modules** in `app.py` with `try/except ImportError`. Don't crash the cloud container because `torch` isn't installed.
- **Warmup must run on every entry path** (`python app.py`, `gunicorn`, `flask run`). Spawn at module import, not in `if __name__ == "__main__"`.

## Things that looked clever but weren't

- **Service-worker version bump on every deploy** — fragile. Better: network-first for HTML.
- **Backward-compat function aliases** (`check_quota`, `get_monthly_chars`). Accumulated, never used. Just rename callers.
- **Hardcoded display names in multiple files** (was: provider names in HTML + JS + Python). Single source of truth (`config/providers.json`) eliminates drift.
