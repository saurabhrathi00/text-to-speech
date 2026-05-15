# Architecture

## Big picture

```
┌────────────┐         ┌────────────────────────┐         ┌──────────────┐
│  Browser   │  HTTPS  │  Flask backend         │  HTTPS  │  Gemini API  │
│  (PWA)     │ ──────▶ │  - auth (Supabase JWT) │ ──────▶ │  ElevenLabs  │
│            │         │  - quota / plans       │         │              │
│            │ ◀────── │  - LLM + TTS dispatch  │ ◀────── │              │
└────────────┘         └────────┬───────────────┘         └──────────────┘
                                │
                                ▼
                       ┌─────────────────────┐
                       │  Supabase           │
                       │  - Postgres         │
                       │  - Auth             │
                       │  - Storage (audio)  │
                       └─────────────────────┘
```

## Two deployments, same code

| | Local (admin GPU box) | Cloud (paying users) |
|---|---|---|
| TTS | parler / bark | elevenlabs |
| LLM | ollama (Qwen) | gemini |
| Heavy ML deps | yes (`requirements-local.txt`) | no |
| Audio storage | Supabase Storage (per-user signed URLs) | Supabase Storage |
| Sign-in | Google OAuth via Supabase | Google OAuth via Supabase |

Same Supabase DB. Switching is purely env (`TTS_PROVIDER`, `LLM_PROVIDER`).

## Request pipeline (`/generate`)

```
JWT verify ─► quota check ─► LLM refine ─► (emotion tags) ─► TTS synth ─► upload to Supabase Storage ─► log usage ─► prune retention
```

1. `auth.require_user` → JWT → set `g.user`
2. `_resolve_tts_provider_for_user` + `_resolve_llm_provider_for_user` → enforce plan whitelist
3. `auth.check_limits` → effective caps (base + bonus pool + topup credit)
4. `normalizer.normalize_text` → calls `llm.refine_for_tts` + optional `llm.classify_emotions`
5. `_tts_synthesize` → dispatches to `eleven_tts` / `tts_engine` / `bark_tts`
6. `audio_storage.upload` → Supabase Storage `<user_id>/<filename>.wav` → signed URL (1h TTL)
7. `auth.log_usage` → row in `usage_events`
8. `auth.consume_bonus_if_used` → decrement `bonus_uses` if base daily exhausted
9. `audio_storage.prune_user_audio` → keep newest 5, delete older than 24h

## Data model

### Profiles
- 1:1 with `auth.users` (Supabase managed)
- Carries `plan`, `role`, `plan_expires_at`, `banned`, `bonus_uses`, `bonus_max_chars_per_request`
- Auto-created by `handle_new_user()` trigger

### plan_limits (config table, admin-editable)
- Per-plan caps: `daily_uses`, `monthly_chars`, `max_chars_per_request`
- `kind` = `subscription` (replaces plan, sets expiry) or `topup` (additive credit)
- `validity_hours` for subscriptions
- `llm_providers[]` / `tts_providers[]` = role-based whitelist

### usage_events (append-only ledger)
- Every TTS gen logged here (positive `chars`)
- Top-ups inserted as negative `chars` events (`kind='credit.topup'`)
- View `usage_summary` aggregates: `chars_24h`, `gen_chars_30d`, `topup_credit_30d`, `uses_24h`, etc.

### upgrade_requests
- User-initiated plan-change asks
- Admin approves → `resolve_upgrade_request`:
  - subscription → stamp `plan` + `plan_expires_at`
  - topup → insert refund event + bump `bonus_uses` + maybe raise `bonus_max_chars_per_request`

## Effective plan + limits (the trick)

Plan expiry is **computed lazily on every request**, not via cron:

```
get_effective_plan(profile):
  role=admin       → 'admin'
  plan_expires_at past now → 'free'
  else             → profile.plan

get_effective_limits(profile, usage):
  monthly_chars  = base + topup_credit_30d
  daily_uses     = base + bonus_uses
  max_chars_per_request = max(base, bonus_max) while bonus_uses > 0
```

Both `check_limits` (enforcement) and `/api/me` (UI) read from the same `get_effective_limits` — single source of truth.

## Modules

| Module | Responsibility | Notes |
|---|---|---|
| `app.py` | Flask routes + dispatch helpers | 1000+ lines, planned split into `routes/` |
| `auth.py` | JWT + profile + plan + quota + upgrades | 700+ lines, planned split into `auth/` package |
| `security.py` | Rate limits, body cap, CORS, Content-Type, suspicious flags | Per-process state |
| `audio_storage.py` | Supabase Storage upload + retention (5 files / 24h) | Falls back to local disk on failure |
| `normalizer.py` | App-side LLM wrapper: Devanagari guard, sentence splitting, refusal detector | Talks to `llm/` |
| `llm/` | ISOLATED module — Gemini + Ollama, prompts as `.md` data | Only stdlib + `requests`. No project imports |
| `eleven_tts.py` | ElevenLabs REST client | Cloud-safe (no torch) |
| `tts_engine.py` / `bark_tts.py` / `aligner.py` / `image_gen.py` | Local-only (torch/transformers/parler-tts) | Lazy-imported in `app.py` |
| `config/providers.json` | Single source of truth for provider id / display / icon / kind | Frontend + backend both read this |

## Auth + permission flow

- **Sign-in:** Google OAuth → Supabase issues JWT → frontend stores in localStorage → `fetch` wrapper attaches `Authorization: Bearer <jwt>`
- **JWT verify:** local HS256 (fast) → Supabase SDK fallback (asymmetric-key projects)
- **Admin gate:** `require_admin` checks **email ∈ ADMIN_EMAILS env**, not DB role. A tampered profiles row cannot grant admin.
- **Provider whitelist:** per-plan list in DB. Server enforces (403 on forbidden); UI hides unavailable buttons.

## Anti-abuse layers

1. Google OAuth (no tempmail farming)
2. Free = 1 gen/day rolling (not lifetime)
3. Daily cap on every paid plan (rate-limits worst-day spend)
4. Provider whitelist server-side
5. `@security.rate_limit("user"/"ip", N, window)` on `/generate`, `/tts`, `/normalize`, `/api/upgrade-request`
6. `MAX_CONTENT_LENGTH = 1MB` → 413 with flag
7. `@security.require_json` → 415 on form-post probes
8. CORS allowlist (`CORS_ALLOWED_ORIGINS` env, empty = same-origin only)
9. `banned` flag on profiles → `require_user` returns 403
10. `flag_suspicious()` ring buffer + `/api/admin/security/recent`

## Frontend state

- All in `templates/index.html` (currently 1300+ lines, planned split into `static/js/`)
- Boot sequence:
  ```
  loadProviderRegistry() + loadPlans()   ← top-level await
  if (!token) → /login
  fetch /api/me → continueBoot(me)
    renderUserBar(me)
    applyAllowedProviders(me)
    waitForLocalModels() if needed
    revealApp()
  ```
- TDZ rule: any `let`/`const` referenced inside the boot chain MUST be declared before the first top-level `await`.

## Things NOT in scope

- Payments — manual approve flow (Razorpay = Phase 4)
- React frontend — Phase 4
- Mobile app — Phase 5
- Redis / multi-host — single Render worker is enough for first 100 users
- SQLAlchemy / migrations folder — single idempotent `schema.sql` is fine

See [README.md](../README.md) for the roadmap, [DEPLOY.md](DEPLOY.md) for cloud setup.
