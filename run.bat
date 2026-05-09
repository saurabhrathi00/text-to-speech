@echo off
REM ====================================================
REM YouTube Narrator — Start server (Windows)
REM ====================================================

if not exist .venv (
  echo Virtual environment not found. Run setup.bat first.
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
