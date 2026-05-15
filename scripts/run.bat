@echo off
REM ====================================================
REM SastaSpeech — Start server (Windows)
REM Run from anywhere — script auto-cd's to repo root.
REM ====================================================
cd /d "%~dp0\.."

if not exist .venv (
  echo Virtual environment not found. Run scripts\setup.bat first.
  exit /b 1
)

if not exist .env (
  echo .env file not found. Copy .env.example to .env and add your HF_TOKEN.
  exit /b 1
)

call .venv\Scripts\activate.bat

echo Starting YouTube Narrator...
echo Open http://localhost:5000 in your browser
echo Phone (same WiFi): http://YOUR-PC-IP:5000
echo.
python app.py
