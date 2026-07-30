#!/usr/bin/env bash
# Компактный cookies.txt только для YouTube (чтобы влезло в Bothost env).
set -euo pipefail
cd "$(dirname "$0")"

OUT="${1:-$HOME/Downloads/yt_cookies_compact.txt}"
TMP="$(mktemp)"

if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
else
  PY=python3
fi

"$PY" -m yt_dlp \
  --cookies-from-browser chrome \
  --cookies "$TMP" \
  --skip-download \
  --no-playlist \
  --ignore-no-formats-error \
  "https://www.youtube.com/watch?v=jNQXAC9IVRw" >/dev/null 2>&1 || true

# Оставляем только youtube/google auth cookies
"$PY" - <<PY
from pathlib import Path
src = Path("$TMP")
dst = Path("$OUT")
if not src.is_file() or src.stat().st_size < 50:
    raise SystemExit("cookies not written — открой Chrome, зайди на youtube.com и повтори")

keep_names = {
    "SID", "HSID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID",
    "__Secure-1PSIDTS", "__Secure-3PSIDTS",
    "__Secure-1PAPISID", "__Secure-3PAPISID",
    "LOGIN_INFO", "PREF", "CONSENT",
    "VISITOR_INFO1_LIVE", "YSC", "SIDCC", "__Secure-1PSIDCC", "__Secure-3PSIDCC",
}
keep_domains = ("youtube.com", "google.com", "youtu.be")
lines_out = ["# Netscape HTTP Cookie File", "# compact youtube export", ""]
kept = 0
for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
    if not line or line.startswith("#"):
        continue
    parts = line.split("\t")
    if len(parts) < 7:
        continue
    domain, name = parts[0], parts[5]
    if not any(d in domain for d in keep_domains):
        continue
    if name not in keep_names and not name.startswith("__Secure-"):
        continue
    lines_out.append(line)
    kept += 1
if kept < 3:
    raise SystemExit(f"too few cookies kept ({kept}) — залогинься в YouTube в Chrome")
dst.write_text("\n".join(lines_out) + "\n", encoding="utf-8")
print(f"wrote {dst} ({dst.stat().st_size} bytes, {kept} cookies)")
PY

rm -f "$TMP"
base64 -i "$OUT" | tr -d '\n' | pbcopy
B64_LEN=$(base64 -i "$OUT" | tr -d '\n' | wc -c | tr -d ' ')
echo "OK: base64 в буфере (${B64_LEN} символов). Вставь в YTDLP_COOKIES_B64"
if [[ "$B64_LEN" -gt 12000 ]]; then
  echo "WARNING: всё ещё длинно для Bothost env — лучше загрузи файл в /app/data/cookies.txt"
fi
