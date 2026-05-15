<div align="center">

# 🎙️ SastaSpeech

### Studio-quality Hindi voiceovers, starting at ₹49.

Sasta + Mast Text-to-Speech for Indian creators. Type Hindi / Hinglish / English →
get natural narrator audio in seconds.

[![Built with](https://img.shields.io/badge/Flask-3-000?logo=flask)]()
[![Supabase](https://img.shields.io/badge/Supabase-Auth%20%2B%20Postgres-3ECF8E?logo=supabase)]()
[![Gemini](https://img.shields.io/badge/Gemini-2.0%20Flash-4285F4?logo=google)]()
[![ElevenLabs](https://img.shields.io/badge/ElevenLabs-v3-000)]()

</div>

---

## ✨ Why SastaSpeech

| | |
|---|---|
| 🇮🇳 **Best Hindi voices**         | ElevenLabs v3 ka natural prosody — sounds human, not a robot. |
| ⚡ **Seconds mein generate**       | Gemini cleans up Hinglish → ElevenLabs speaks it. End-to-end < 10 sec. |
| 🎭 **Emotion-aware**              | 60+ inline performance tags ([sobbing], [giggles], [whispers], [breathless]...) chosen per-sentence by Gemini — and it can invent new ones when nothing fits. |
| 💸 **Sabse Sasta pricing**         | Day pass ₹49 · Monthly se ₹299. Pay-as-you-go ya subscribe — koi commitment nahi. |
| 🎯 **No surprises**                | Char-by-char usage tracking. Daily + monthly caps. Plan kabhi bhi switch. |
| 🔒 **Private**                     | Your scripts never train any model. Audio lives in per-user Supabase buckets behind 1-hour signed URLs; auto-deleted after 24h. |

---

## 💰 Pricing

Built around a **₹0.0132 / character** cost basis (ElevenLabs v3) + sensible margins.

| Plan          | ₹/mo  | Validity | Reqs       | Max/req | Total chars | Audio output |
|---------------|-------|----------|------------|---------|-------------|--------------|
| **Free**      | 0     | forever  | 1/day      | 100     | 100         | ~12 sec      |
| **Sabse Sasta** ⚡ | 49 one-time | top-up | additive | — | +1,500    | ~3 min        |
| **Starter**   | 299   | 30 days  | 5/day      | 1,000   | 20k         | ~40 min       |
| **Pro**       | 799   | 30 days  | 10/day     | 3,000   | 50k         | ~1 hr 40 min  |
| **Pro Plus**  | 1,999 | 30 days  | 20/day     | 5,000   | 150k        | ~5 hours      |

**Sabse Sasta** is unique — a top-up that *adds* 1,500 chars to whatever plan you're on. Pro user out of monthly credits? Drop ₹49 for an emergency refill. Plan stays, expiry stays, you just get more chars.

Daily caps are rate-limiters (~3–5× daily average usage) — a single user can't burn the entire month in one day.

All plans live in `plan_limits` (DB), editable by admin without a code change.

---

## 🏗️ Architecture

```
┌─────────────────────────┐         ┌──────────────────────────┐
│  Browser (PWA)          │  HTTPS  │  Flask backend           │
│  - Login (Google OAuth) │ ──────▶ │  - JWT verify (Supabase) │
│  - Plan / quota UI      │         │  - Plan-aware routing    │
│  - Audio playback       │ ◀────── │  - Provider whitelist    │
└─────────────────────────┘         └─────────┬────────────────┘
                                              │
                          ┌───────────────────┼───────────────────┐
                          ▼                   ▼                   ▼
                  ┌───────────────┐  ┌────────────────┐  ┌──────────────┐
                  │   Gemini 2.0  │  │   ElevenLabs   │  │  Supabase    │
                  │   Flash (LLM) │  │   v3 (TTS)     │  │  Postgres    │
                  │   - refine    │  │   - synthesize │  │  - profiles  │
                  │   - emotions  │  │     audio      │  │  - plans     │
                  └───────────────┘  └────────────────┘  │  - usage     │
                                                         │  - upgrades  │
                                                         └──────────────┘
```

**Pipeline per `/generate`:**

1. Browser sends script + chosen TTS provider + chosen LLM
2. Flask validates JWT, looks up user's plan & allowed providers
3. Quota check (per-req, daily, monthly caps)
4. LLM refines text → emotion tags inserted (optional)
5. TTS synthesizes audio → `audio/<uuid>.wav` served to browser
6. Usage event logged → `usage_events` (drives the rolling counters)

---

## 🧱 Two-deployment model

```
┌─────────────────────────────┐         ┌─────────────────────────────┐
│  LOCAL (admin's GPU box)    │         │  CLOUD (production)         │
│                             │         │                             │
│  TTS_PROVIDER=parler        │         │  TTS_PROVIDER=elevenlabs    │
│  LLM_PROVIDER=ollama        │         │  LLM_PROVIDER=gemini        │
│                             │         │                             │
│  Loads: Parler + Whisper    │         │  No models — HTTP only      │
│         + Qwen via Ollama   │         │  Cheap CPU container ($7/mo)│
│                             │         │                             │
│  Serves: admin (role=admin) │         │  Serves: paying customers   │
└─────────────────────────────┘         └─────────────────────────────┘
              │                                       │
              └────────── same Supabase DB ──────────┘
```

Same code, same Supabase, different env. Cloud server stays cheap and reliable; admin keeps Parler/Qwen privileges for personal use. See [docs/DEPLOY.md](docs/DEPLOY.md) for full deploy steps.

---

## 🚀 Quickstart (local)

### Prerequisites
- Python 3.10+
- Supabase project (free tier OK) with schema applied
- Either a local GPU + Ollama + Parler, **or** Gemini + ElevenLabs API keys

### 1. Clone + install

```bash
git clone https://github.com/saurabhrathi00/text-to-speech.git
cd text-to-speech

# Cloud-only deps
pip install -r requirements.txt

# Add heavy ML deps if you'll run Parler / Qwen locally
pip install -r requirements-local.txt
```

### 2. Configure `.env`

Copy `.env.example → .env` and fill in:

```env
# LLM
LLM_PROVIDER=gemini              # or ollama
GEMINI_API_KEY=...               # from aistudio.google.com
GEMINI_MODEL=gemini-2.0-flash

# TTS
TTS_PROVIDER=elevenlabs          # or parler / bark
ELEVENLABS_API_KEY=...

# Supabase
SUPABASE_URL=https://<ref>.supabase.co
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_KEY=...         # secret — server only
SUPABASE_JWT_SECRET=...

# Admin (env-only — no UI path to grant admin)
ADMIN_EMAILS=you@example.com
```

### 3. Apply DB schema

In Supabase SQL Editor, paste `db/schema.sql` and run. Idempotent — re-run any time after pulling new changes.

### 4. Enable Google OAuth in Supabase

Authentication → Providers → Google → toggle on → paste Google Cloud OAuth credentials. Authentication → URL Configuration → add `http://localhost:5000` to redirect URLs.

### 5. Run

```bash
python app.py
# → Flask on http://localhost:5000
```

Open the URL → "Continue with Google" → generate your first audio.

---

## 📁 Project structure

```
text-to-speech/
├── app.py                    # Flask routes (auth, quota, /generate, /tts, admin, plans)
├── auth.py                   # Supabase JWT + plan/quota + bonus pool + upgrade requests
├── security.py               # Rate limiting + CORS + Content-Type guard + 413 handler
├── audio_storage.py          # Per-user Supabase Storage with 5-file / 24h retention
├── normalizer.py             # App-side wrapper (Devanagari guard + sentence splitting)
├── llm/                      # Isolated LLM module (see llm/README.md)
│   ├── __init__.py           #   refine_for_tts, classify_emotions, generate_scene_prompts
│   ├── client.py             #   Gemini + Ollama HTTP clients
│   ├── config.py             #   LLM env vars (only place that reads them)
│   └── prompts/              #   *.md prompt files — data not code (60+ emotion tags)
├── tts_engine.py             # Parler-TTS wrapper (local-only)
├── eleven_tts.py             # ElevenLabs REST client (cloud-safe)
├── bark_tts.py               # Bark wrapper (local-only)
├── aligner.py                # faster-whisper word alignment (local-only)
├── config/
│   └── providers.json        # Single source of truth — providers' id/display/icon/kind
├── db/
│   └── schema.sql            # Idempotent Postgres schema (run in Supabase)
├── static/                   # CSS, service worker, manifest, icons
├── templates/                # index.html + login.html (server-rendered)
├── requirements.txt          # Cloud-safe slim deps
├── requirements-local.txt    # Heavy ML deps (additive on top of base)
├── Procfile                  # gunicorn entrypoint for Render/Railway
├── docs/
│   ├── architecture.md       # Module map + request pipeline
│   ├── DEPLOY.md             # Full cloud + local deploy guide
│   └── INSTALL.md            # Quick install (Windows/Mac/Linux)
├── scripts/
│   ├── run.sh / run.bat      # Start server (auto cd to repo root)
│   └── setup.sh / setup.bat  # One-time venv + deps
└── README.md                 # ← you are here
```

---

## 🔑 Key design choices

**Provider registry as data.** Every provider's metadata (id, display name, icon, kind) lives in `config/providers.json`. Adding a new provider = one line + a `plan_limits` row in DB. Zero code changes elsewhere.

**LLM as an isolated package.** Everything in `llm/` is a black box — only `requests` + stdlib imports, no project coupling. Read [llm/README.md](llm/README.md) for the contract.

**Role-based provider whitelist.** Each plan in `plan_limits` carries `llm_providers[]` and `tts_providers[]`. Admin gets everything; free users get cloud-only. Server enforces, frontend hides unavailable UI.

**Effective plan resolves expiry.** Paid plans carry `validity_hours`; `profiles.plan_expires_at` is stamped on approve. Past expiry, the user auto-reverts to free — no cron job, computed lazily on each request.

**Top-ups stack on three axes.** Sabse Sasta adds:
- **chars** — negative `usage_events` row offsets the rolling 30-day sum
- **gens** — `profiles.bonus_uses` counter, drained only after the base daily cap is exhausted
- **per-req size** — `profiles.bonus_max_chars_per_request` raises the per-request ceiling while bonus gens remain

`get_effective_limits` folds all three into a single `daily_cap / monthly_cap / max_chars_per_request` view the UI and `check_limits` read from. Base caps stay alongside so the user-bar can show *"100 base + 1500 bonus = 1600"*.

**Audio in per-user Supabase Storage.** Files land at `audio/<user_id>/<filename>.wav` in a private bucket. The backend hands the browser a 1-hour signed URL; nothing on the local filesystem leaks across users. `audio_storage.prune_user_audio` runs after every upload — newest 5 files only, anything past `AUDIO_RETENTION_HOURS` (default 24) deleted regardless of count.

**Admin promotion is env-only.** `ADMIN_EMAILS` in `.env` is the *only* path to grant admin. No API route mutates `role`. A compromised DB row can't escalate — `require_admin` checks env, not DB.

**Two-deployment for reliability.** Local for admin (Parler/Qwen), cloud for customers (ElevenLabs/Gemini). Same code, same DB, different env. No tunnels, no home-machine dependency for paying users.

---

## 🛡️ Anti-abuse layers

| Layer | Effect |
|---|---|
| **Google OAuth required for signup** | No email farming with tempmail / +aliases — every account needs a phone-verified Gmail |
| **Free = 1 gen/day (not lifetime)** | Multi-account farming gives ~₹1.32/day per fake account — capped, not exponential |
| **Daily caps on paid plans** | Rate-limit so no single user can burn the monthly quota in 24h |
| **Provider whitelist server-side** | Free user can't bypass by crafting `{provider: 'parler'}` in the request |
| **Per-user + per-IP rate limits** | 15 generations/min/user, 30/min/IP. Excess returns 429 with `Retry-After`. Counters logged + surfaced via `/api/admin/security/recent` |
| **`MAX_CONTENT_LENGTH = 1MB`** | Payload bombing → 413. Body-size limit applied at the Flask level |
| **Strict `Content-Type: application/json`** | Form-post probes / accidental CSRF surface → 415 |
| **CORS allowlist (`CORS_ALLOWED_ORIGINS`)** | Empty by default = same-origin only. Cross-origin POSTs from unknown sites blocked |
| **Image generation admin-only** | All `/api/image*` + `/api/scenes` routes return 403 to non-admins |
| **`banned` flag on profiles** | Soft-block without losing history; admin-toggleable via `PATCH /api/admin/users/<id>` |
| **Suspicious-activity ring buffer** | Last 200 rate-limit hits + oversized bodies retained in memory for the admin to review |

---

## 🔭 Roadmap

- [x] **Phase 1** — Gemini LLM swap-in
- [x] **Phase 2** — Supabase auth + per-user quota + Google OAuth + upgrade flow (with top-ups)
- [x] **Phase 2.5** — Per-user Supabase Storage for audio (5-file / 24h retention)
- [x] **Phase 2.6** — Production anti-abuse layer (rate limiting, body-size cap, CORS, Content-Type guard)
- [ ] **Phase 3** — Cloud deploy to Render + `sastaspeech.com` SSL
- [ ] **Phase 4** — Razorpay payment integration (replaces manual approve)
- [ ] **Phase 5** — React Native mobile app (Android + iOS) hitting the same API

---

## 🧪 Try it

Live demo coming soon at **[sastaspeech.com](https://sastaspeech.com)** — Phase 3 work.

---

## 📜 License

[MIT](LICENSE) — fork it, ship your own, no warranties.

---

<div align="center">

Built with chai ☕ aur compute by [@saurabhrathi00](https://github.com/saurabhrathi00).

</div>
