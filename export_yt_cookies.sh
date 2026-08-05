#!/usr/bin/env bash
# Cookies для Bothost: youtube.com + accounts.google.com.
#
# ВАЖНО для UMG/гео (Ken Carson и т.п.):
# Cookies с домашнего Wi‑Fi НЕ работают через чужой YTMUSIC_PROXY
# (YouTube: Sign in to confirm you're not a bot).
# Экспортируй cookies ТОЛЬКО после входа в YouTube через ТОТ ЖЕ прокси:
#   1) В браузере включи системный/extension proxy = YTMUSIC_PROXY
#   2) Инкогнито → youtube.com → логин → robots.txt → Get cookies.txt LOCALLY
#   3) ./export_yt_cookies.sh ~/Downloads/cookies.txt
#
# Использование:
#   ./export_yt_cookies.sh
#   ./export_yt_cookies.sh ~/Downloads/yt_cookies_full.txt
set -euo pipefail
cd "$(dirname "$0")"

OUT="$HOME/Downloads/yt_cookies_server.txt"
SRC="${1:-}"
TMP="$(mktemp)"

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
else
  PY=python3
fi

if [[ -n "$SRC" && -f "$SRC" ]]; then
  cp "$SRC" "$TMP"
elif [[ -f "$HOME/Downloads/yt_cookies_full.txt" ]]; then
  echo "Using existing ~/Downloads/yt_cookies_full.txt"
  cp "$HOME/Downloads/yt_cookies_full.txt" "$TMP"
else
  echo "Extracting cookies from Chrome…"
  "$PY" -m yt_dlp \
    --cookies-from-browser chrome \
    --cookies "$TMP" \
    --skip-download \
    --no-playlist \
    --ignore-no-formats-error \
    "https://www.youtube.com/watch?v=jNQXAC9IVRw" || true
fi

"$PY" - <<PY
from pathlib import Path
src = Path("$TMP")
dst = Path("$OUT")
if not src.is_file() or src.stat().st_size < 50:
    raise SystemExit("нет исходных cookies — сначала сделай yt_cookies_full.txt")

def keep(domain: str, name: str) -> bool:
    d = domain.lstrip(".").lower()
    if "youtube.com" in d or "youtu.be" in d:
        return True
    if d in {"google.com", "accounts.google.com"} or d.endswith(".google.com") and "accounts" in d:
        return True
    # корневой .google.com нужен для SID/SAPISID
    if domain in {".google.com", "google.com"}:
        return True
    return False

lines_out = ["# Netscape HTTP Cookie File", "# youtube + google accounts", ""]
kept = 0
names = set()
for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
    if not line or line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) < 7:
        continue
    domain, name = parts[0], parts[5]
    if not keep(domain, name):
        continue
    lines_out.append(line)
    kept += 1
    names.add(name)

if kept < 5 or "LOGIN_INFO" not in names:
    raise SystemExit(
        f"мало cookies ({kept}, LOGIN_INFO={'yes' if 'LOGIN_INFO' in names else 'no'}). "
        "Залогинься в YouTube в Chrome и пересоздай full export."
    )
dst.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
print(f"wrote {dst} ({dst.stat().st_size} bytes, {kept} cookies, LOGIN_INFO=yes)")
PY

rm -f "$TMP"
base64 -i "$OUT" | tr -d '\n' | pbcopy
B64_LEN=$(base64 -i "$OUT" | tr -d '\n' | wc -c | tr -d ' ')
echo "OK: base64 в буфере (${B64_LEN} символов)"
echo "Bothost: удали старый YTDLP_COOKIES_B64 → вставь новый → Update from Git → рестарт"
if [[ "$B64_LEN" -gt 18000 ]]; then
  echo "WARNING: длинновато — лучше файл /app/data/cookies.txt без B64"
fi
