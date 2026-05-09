# Installation Guide

## Prerequisites (Both platforms)

1. **Python 3.10+** installed
2. **HuggingFace account** + token from https://huggingface.co/settings/tokens
3. **Accept model license** at https://huggingface.co/ai4bharat/indic-parler-tts (one-click)
4. **Ollama running** with `qwen2.5:7b` pulled (`ollama pull qwen2.5:7b`)

---

## 🪟 Windows (recommended — uses RTX 3060 GPU)

```cmd
git clone <repo>  OR  unzip the project folder
cd text-to-speech

REM One-time setup
setup.bat

REM Edit .env file: set HF_TOKEN
notepad .env

REM Start server
run.bat
```

Server: `http://localhost:5000`
Phone (same WiFi): `http://<your-PC-IP>:5000` — find your PC's IP with `ipconfig`.

---

## 🍎 Mac / 🐧 Linux

```bash
cd text-to-speech

# One-time setup
./setup.sh

# Edit .env: set HF_TOKEN
nano .env

# Start server
./run.sh
```

Server: `http://localhost:5000`
Phone IP shown in startup logs.

---

## .env config

| Variable | What it does | Example |
|---|---|---|
| `HF_TOKEN` | HuggingFace token to download TTS model | `hf_xxx...` |
| `OLLAMA_URL` | Where Qwen is running | `http://127.0.0.1:11434/api/chat` (local) or `http://192.168.1.10:11434/api/chat` (remote PC) |
| `OLLAMA_MODEL` | Model name | `qwen2.5:7b` |
| `HOST` | Bind address | `0.0.0.0` (all interfaces, allows phone access) |
| `PORT` | Flask port | `5000` |

---

## Troubleshooting

**"Qwen server se connect nahi ho paya"**
- Check Ollama is running: `ollama list`
- Verify URL in `.env` matches the machine where Qwen runs
- Same WiFi check: ping that IP

**"Awaaz generate nahi ho payi"**
- First run downloads ~2GB model — wait 5-10 min
- HF_TOKEN missing or invalid → check .env
- License not accepted → visit model page

**Phone can't reach server**
- Same WiFi ✓
- Windows Firewall: allow Python through firewall (one-time popup on first run)
- Use `HOST=0.0.0.0` (default)

**Slow on Mac CPU**
- Expected — use Windows + RTX 3060 for ~10x speedup
