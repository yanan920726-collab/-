#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${NOVEL_DOWNLOADER_PORT:-8765}"
URL="http://127.0.0.1:${PORT}"

echo "正在启动网页小说下载器..."
echo "第一次启动会自动安装依赖，请稍等。"

mkdir -p "$SCRIPT_DIR/.tmp" "$SCRIPT_DIR/.cache"
export TMPDIR="$SCRIPT_DIR/.tmp"
export PIP_CACHE_DIR="$SCRIPT_DIR/.cache/pip"

for CHECK_PORT in $(seq "$PORT" "$((PORT + 19))"); do
  CHECK_URL="http://127.0.0.1:${CHECK_PORT}"
  if curl -fsS "$CHECK_URL/api/health" >/dev/null 2>&1; then
    echo "检测到下载器已经在运行，正在打开页面：$CHECK_URL"
    open "$CHECK_URL"
    exit 0
  fi
done

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

source ".venv/bin/activate"
PYTHON="$SCRIPT_DIR/.venv/bin/python"

if ! "$PYTHON" - <<'PY'
import importlib.util
import sys

missing = [
    name
    for name in ("requests", "bs4", "playwright")
    if importlib.util.find_spec(name) is None
]
try:
    import urllib3
    if int(urllib3.__version__.split(".", 1)[0]) >= 2:
        missing.append("urllib3<2")
except Exception:
    missing.append("urllib3<2")
sys.exit(1 if missing else 0)
PY
then
  echo "正在安装 Python 依赖..."
  "$PYTHON" -m pip install --upgrade pip >/dev/null
  "$PYTHON" -m pip install -r requirements.txt >/dev/null
else
  echo "Python 依赖已就绪，跳过安装。"
fi

if ! compgen -G "$HOME/Library/Caches/ms-playwright/chromium_headless_shell*" >/dev/null && ! compgen -G "$HOME/Library/Caches/ms-playwright/chromium-*" >/dev/null; then
  echo "正在安装浏览器内核..."
  "$PYTHON" -m playwright install chromium >/dev/null
else
  echo "浏览器内核已就绪，跳过安装。"
fi

echo "启动完成后会自动打开浏览器页面。"
echo "如果默认端口被占用，会自动切换到后续端口。请以下方实际启动地址为准。"

NOVEL_DOWNLOADER_PORT="$PORT" "$PYTHON" novel_web_app.py
