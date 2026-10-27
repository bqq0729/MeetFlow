@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined WEB_PORT set WEB_PORT=8788
if not exist ".venv\Scripts\python.exe" (
  echo 처음 실행이에요. 가상환경을 만들게요...
  python -m venv .venv
)
".venv\Scripts\python" -c "import faster_whisper, sherpa_onnx, numpy, webview, docx, keyring" >nul 2>&1
if errorlevel 1 (
  echo 필요한 패키지가 없거나 덜 깔려 있어서 설치할게요 (몇 분 걸려요)...
  ".venv\Scripts\python" -m pip install --upgrade pip -q
  ".venv\Scripts\pip" install -r requirements.txt
  where nvidia-smi >nul 2>&1 && ".venv\Scripts\pip" install -r requirements-gpu.txt
)
".venv\Scripts\python" server.py
pause
