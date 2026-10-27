#!/bin/bash
# 개인용(personal) 실행 스크립트. 원본(whisper-stt-web)과 같은 PC에서 같이 띄워도 포트가
# 겹치지 않게 기본 WEB_PORT를 8788로 둔다 (원본은 8787). 필요하면 실행 전 환경변수로 덮어쓸 수
# 있다: WEB_HOST=0.0.0.0 WEB_PORT=8788 ./run.sh
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"  # claude·codex CLI (비대화형 SSH 세션은 ~/.bashrc를 안 읽어서 직접 추가)

if [ ! -x ".venv/bin/python" ]; then
  echo "처음 실행이에요. 가상환경을 만들게요..."
  python3 -m venv .venv
fi

if ! ".venv/bin/python" -c "import faster_whisper, sherpa_onnx, numpy, webview, docx, keyring" >/dev/null 2>&1; then
  echo "필요한 패키지가 없거나 덜 깔려 있어서 설치할게요 (몇 분 걸려요)..."
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt
  if command -v nvidia-smi >/dev/null 2>&1; then .venv/bin/pip install -r requirements-gpu.txt; fi
fi

# pip로 깐 cuBLAS/cuDNN(.so)을 faster-whisper(ctranslate2)가 찾을 수 있게 한다 (Windows의 add_cuda_dll_paths에 해당)
for d in "$PWD"/.venv/lib/python3*/site-packages/nvidia/*/lib; do
  [ -d "$d" ] && LD_LIBRARY_PATH="$d${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
done
export LD_LIBRARY_PATH

export WEB_HOST="${WEB_HOST:-127.0.0.1}"
export WEB_PORT="${WEB_PORT:-8788}"
exec .venv/bin/python server.py
