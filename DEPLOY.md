# Deployment guide — Two-deployment model

SastaSpeech runs the same code in two places:

| Box | Purpose | TTS | LLM | Heavy ML deps |
|---|---|---|---|---|
| **Local (admin)** | papa / saurabh use Parler + Qwen | parler / bark | ollama | yes |
| **Cloud (production)** | paying customers — Free / Starter / Pro / Pro Plus | elevenlabs | gemini | **no** |

Both connect to the **same Supabase database** — single source of truth for users, plans, usage. Admin can log in from either box; cloud customers only ever hit the cloud box.

---

## Cloud deployment (Render / Railway / Fly)

### Pre-reqs

1. Supabase project with schema applied (`db/schema.sql`).
2. ElevenLabs API key (paid plan).
3. Google Gemini API key (free tier OK to start).
4. Google OAuth client configured in Supabase (Authentication → Providers → Google).

### Steps

1. **Push code** to GitHub (already done).
2. **Render**: New → Web Service → connect repo.
   - Environment: **Python 3**
   - Build command: `pip install -r requirements.txt` (cloud reqs only — no torch)
   - Start command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 300`
   - Region: pick closest to your users (Singapore for India)
   - Plan: Starter ($7/mo) is enough — no GPU needed.
3. **Environment variables** (Render dashboard → Environment):
   ```
   TTS_PROVIDER=elevenlabs
   LLM_PROVIDER=gemini

   SUPABASE_URL=https://<ref>.supabase.co
   SUPABASE_ANON_KEY=<public anon key>
   SUPABASE_SERVICE_KEY=<service role key, secret>
   SUPABASE_JWT_SECRET=<from supabase settings>

   ELEVENLABS_API_KEY=<from elevenlabs dashboard>
   GEMINI_API_KEY=<from aistudio.google.com>
   GEMINI_MODEL=gemini-2.0-flash

   ADMIN_EMAILS=saurabh45rathi@gmail.com,rathisubodh0@gmail.com
   ```
4. **Deploy.** First build takes ~2 min (no heavy ML deps).
5. **Custom domain**: Render → Settings → Custom domains → add `sastaspeech.com`. Configure DNS A/CNAME as Render shows.
6. **Update Supabase**: Authentication → URL Configuration → Site URL `https://sastaspeech.com`, redirect URLs include `https://sastaspeech.com/` and `https://sastaspeech.com/**`.
7. **Update Google OAuth**: Google Cloud Console → Credentials → your client → add `https://sastaspeech.com` to Authorised JavaScript origins, plus the existing Supabase callback in redirect URIs.

### What runs

Cloud server boots in ~3 seconds (no model load). On every `/generate` request:
1. JWT verified via Supabase (HS256 then SDK fallback).
2. User's plan + allowed providers fetched from DB.
3. Text refined via Gemini API (~1–3 sec).
4. Audio synthesized via ElevenLabs API (~2–8 sec).
5. Audio file written to local disk, served via `/audio/<file>`.

### Audio storage caveat

Cloud filesystem is **ephemeral** — files vanish when the container restarts. For now `_prune_old_audio` keeps only the latest 50 files. For real persistence, the next step is Cloudflare R2 or S3:

- `audio` route uploads to R2 instead of disk
- `audio_url` returned to the client is a signed R2 URL (1-hour TTL)
- Costs ~$0 for first 10 GB

That's a Phase 3 follow-up — not blocking the initial launch.

---

## Local deployment (admin box, optional)

Install heavy deps too:
```bash
pip install -r requirements.txt -r requirements-local.txt
```

`.env`:
```
TTS_PROVIDER=parler        # or bark
LLM_PROVIDER=ollama
OLLAMA_URL=http://127.0.0.1:11434/api/chat
OLLAMA_MODEL=qwen3:14b
OLLAMA_KEEP_ALIVE=5m

# Same Supabase + admin keys as cloud — both boxes share state
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_KEY=...
SUPABASE_JWT_SECRET=...
ADMIN_EMAILS=saurabh45rathi@gmail.com,rathisubodh0@gmail.com

# These can also live here if you sometimes use cloud providers locally
ELEVENLABS_API_KEY=...
GEMINI_API_KEY=...
```

Start: `python app.py` or `flask run`.

---

## Why two deployments instead of bridging

| Concern | Two-deployment | Bridging via Cloudflare Tunnel |
|---|---|---|
| Home internet uptime | doesn't matter | catastrophic if down |
| Home machine 24/7 | not needed | required |
| Cloud server cost | $7/mo CPU | $7/mo CPU + ongoing tunnel risk |
| Latency for cloud users | low (cloud → ElevenLabs) | high (cloud → home → ElevenLabs) |
| Throughput | scales with ElevenLabs | bottlenecked by home upload speed |
| User-facing reliability | high | depends on weakest link |

Local Parler / Qwen stay an admin-only privilege. Cloud customers always get ElevenLabs + Gemini.
