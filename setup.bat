@echo off
REM ====================================================
REM YouTube Narrator — One-time setup for Windows
REM ====================================================

echo [1/4] Creating virtual environment...
python -m venv .venv
if errorlevel 1 (
  echo Python not found. Install Python 3.10+ from python.org first.
  exit /b 1
)

echo [2/4] Activating venv and upgrading pip...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip

echo [3/4] Installing PyTorch with CUDA 12.1 support (for RTX 3060)...
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121

echo [4/4] Installing remaining dependencies...
pip install flask transformers parler-tts soundfile requests

if not exist .env (
  echo Creating .env from template...
  copy .env.example .env
  echo.
  echo  IMPORTANT: Edit .env and set your HF_TOKEN
  echo  Get token from: https://huggingface.co/settings/tokens
  echo  Also accept license at: https://huggingface.co/ai4bharat/indic-parler-tts
)

echo.
echo Setup complete! Run "run.bat" to start the server.
pause
