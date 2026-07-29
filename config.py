"""Загрузка конфигурации из окружения / .env."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_COOKIE_PATHS = (
    "/app/data/cookies.txt",
    "/app/data/youtube_cookies.txt",
    "/app/cookies.txt",
)

_last_cookies_error = ""


def _get(name: str, default: str = "") -> str:
    val = (os.getenv(name) or default).strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
        val = val[1:-1].strip()
    return val


def _fix_b64_padding(b64: str) -> str:
    pad = (-len(b64)) % 4
    return b64 + ("=" * pad)


def _decode_cookies_b64(b64: str) -> tuple[bytes | None, str]:
    if not b64:
        return None, ""
    cleaned = b64.replace("\n", "").replace("\r", "").replace(" ", "")
    cleaned = _fix_b64_padding(cleaned)
    try:
        raw = base64.b64decode(cleaned, validate=False)
    except Exception as exc:  # noqa: BLE001
        return None, f"base64 decode failed: {exc}"
    if len(raw) < 50:
        return None, f"decoded cookies too small ({len(raw)} bytes)"
    head = raw[:300].lower()
    if b"youtube.com" not in head and b"netscape" not in head:
        return None, "decoded data does not look like cookies.txt"
    return raw, ""


def _resolve_ytdlp_cookies_file() -> str:
    """
    Путь к cookies.txt для yt-dlp.
    На сервере (Bothost) нет Chrome — только файл или YTDLP_COOKIES_B64.
    """
    global _last_cookies_error

    explicit = _get("YTDLP_COOKIES_FILE") or _get("YOUTUBE_COOKIES_FILE")
    if explicit and Path(explicit).is_file():
        return explicit

    for candidate in _DEFAULT_COOKIE_PATHS:
        if Path(candidate).is_file():
            logger.info("yt-dlp cookies found at default path %s", candidate)
            return candidate

    b64 = _get("YTDLP_COOKIES_B64")
    if not b64:
        return explicit or ""

    data_dir = Path(_get("DATA_DIR") or "/app/data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("cannot create DATA_DIR %s: %s — using /tmp", data_dir, exc)
        data_dir = Path("/tmp")

    target = data_dir / "youtube_cookies.txt"
    raw, err = _decode_cookies_b64(b64)
    if raw is None:
        _last_cookies_error = err or "YTDLP_COOKIES_B64 decode failed"
        logger.warning("YTDLP_COOKIES_B64 invalid: %s", _last_cookies_error)
        return explicit or ""

    try:
        target.write_bytes(raw)
        logger.info("wrote yt-dlp cookies to %s (%d bytes)", target, len(raw))
        _last_cookies_error = ""
        return str(target)
    except OSError as exc:
        _last_cookies_error = f"cannot write cookies: {exc}"
        logger.warning("%s", _last_cookies_error)
        return explicit or ""


def _resolve_ytdlp_cookies_from_browser(cookies_file: str) -> str:
    """На Docker/сервере браузера нет — не пытаемся cookies-from-browser."""
    if cookies_file and Path(cookies_file).is_file():
        return ""
    val = _get("YTDLP_COOKIES_FROM_BROWSER") or _get("YOUTUBE_COOKIES_FROM_BROWSER")
    if not val:
        return ""
    if Path("/.dockerenv").exists() or _get("YTDLP_COOKIES_B64"):
        return ""
    return val


BOT_TOKEN = _get("BOT_TOKEN")
OCR_SPACE_API_KEY = _get("OCR_SPACE_API_KEY") or _get("OCR_API_KEY")
# eng / rus / auto (OCR Engine 2 лучше с auto)
OCR_LANGUAGE = _get("OCR_LANGUAGE", "auto") or "auto"
DEFAULT_COUNTRY = (_get("DEFAULT_COUNTRY", "ru") or "ru").lower()

SPOTIFY_CLIENT_ID = _get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = _get("SPOTIFY_CLIENT_SECRET")
YANDEX_MUSIC_TOKEN = _get("YANDEX_MUSIC_TOKEN")
# Genius API (опционально) — https://genius.com/api-clients
GENIUS_ACCESS_TOKEN = _get("GENIUS_ACCESS_TOKEN") or _get("GENIUS_TOKEN")
# SerpAPI Google Lens (опционально) — https://serpapi.com/
SERPAPI_API_KEY = _get("SERPAPI_API_KEY") or _get("SERP_API_KEY")

TELEGRAM_PROXY = (
    _get("TELEGRAM_PROXY") or _get("HTTPS_PROXY") or _get("HTTP_PROXY")
)

# YouTube / yt-dlp (если снова «Sign in to confirm you’re not a bot»):
# Mac: YTDLP_COOKIES_FROM_BROWSER=chrome
# Сервер: YTDLP_COOKIES_FILE=/app/cookies.txt или YTDLP_COOKIES_B64=<base64 cookies.txt>
YTDLP_COOKIES_FILE = _resolve_ytdlp_cookies_file()
YTDLP_COOKIES_FROM_BROWSER = _resolve_ytdlp_cookies_from_browser(
    YTDLP_COOKIES_FILE
)


def refresh_ytdlp_cookies() -> str:
    """Перечитать cookies (после mkdir /app/data на сервере)."""
    global YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER

    YTDLP_COOKIES_FILE = _resolve_ytdlp_cookies_file()
    YTDLP_COOKIES_FROM_BROWSER = _resolve_ytdlp_cookies_from_browser(
        YTDLP_COOKIES_FILE
    )
    return YTDLP_COOKIES_FILE


def ytdlp_cookies_status() -> dict[str, object]:
    path = YTDLP_COOKIES_FILE or ""
    p = Path(path) if path else None
    ok = bool(p and p.is_file())
    source = "none"
    if ok and path:
        env_path = _get("YTDLP_COOKIES_FILE") or _get("YOUTUBE_COOKIES_FILE")
        if env_path and str(p.resolve()) == str(Path(env_path).resolve()):
            source = "env file"
        elif path in _DEFAULT_COOKIE_PATHS:
            source = "uploaded/default path"
        elif path.endswith("youtube_cookies.txt"):
            source = "YTDLP_COOKIES_B64"
        else:
            source = "file"
    return {
        "path": path if ok else "",
        "source": source,
        "size": p.stat().st_size if ok and p else 0,
        "b64_env_set": bool(_get("YTDLP_COOKIES_B64")),
        "error": _last_cookies_error,
    }

# YouTube Music регион каталога (ISO: US, GB, DE…).
# Важно: YouTube часто всё равно смотрит на IP — для реального US-каталога
# нужен прокси/VPN с выходом в этот регион (YTMUSIC_PROXY).
YTMUSIC_LOCATION = (_get("YTMUSIC_LOCATION", "US") or "US").upper()
YTMUSIC_LANGUAGE = _get("YTMUSIC_LANGUAGE", "en") or "en"
YTMUSIC_PROXY = _get("YTMUSIC_PROXY") or _get("YTM_PROXY")

# Пагинация альбомов / синглов (6–7 на страницу)
ALBUMS_PER_PAGE = 7
CHART_PER_PAGE = 10

# Таймауты (сек)
REQUEST_TIMEOUT = 5.0
PHOTO_SEARCH_TIMEOUT = 55.0
DOWNLOAD_TIMEOUT = 120.0
DOWNLOAD_ALBUM_TIMEOUT = 180.0

# Telegram лимит аудио ~50 MB
MAX_AUDIO_BYTES = 48 * 1024 * 1024


def require_core_env() -> None:
    missing = []
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OCR_SPACE_API_KEY:
        missing.append("OCR_SPACE_API_KEY")
    if missing:
        raise SystemExit(
            "Не заданы переменные: "
            + ", ".join(missing)
            + "\nСкопируйте .env.example → .env и заполните значения."
        )
