#!/usr/bin/env bash
# Запуск бота на macOS
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v "$c")"
      break
    fi
  done
fi

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "Нужен Python 3.10–3.12. Установите: brew install python@3.12"
  exit 1
fi

VER="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
case "$VER" in
  3.10|3.11|3.12) ;;
  *)
    echo "Рекомендуется Python 3.10–3.12 (сейчас $VER)."
    ;;
esac

if [[ ! -x .venv/bin/python ]]; then
  echo "Создаю venv…"
  rm -rf .venv
  "$PYTHON_BIN" -m venv .venv
  .venv/bin/pip install -U pip
  .venv/bin/pip install -r requirements.txt
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Создан .env — заполните BOT_TOKEN и OCR_SPACE_API_KEY"
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "⚠️  ffmpeg не найден. Для MP3: brew install ffmpeg"
fi

# Убиваем старые инстансы этого бота (Conflict: getUpdates)
pkill -f "[.]venv/bin/python bot.py" 2>/dev/null || true
sleep 1

echo "Старт бота…"
exec .venv/bin/python bot.py
