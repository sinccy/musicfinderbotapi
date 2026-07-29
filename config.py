"""Загрузка конфигурации из окружения / .env."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


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
# путь к cookies.txt (netscape) или браузер: chrome / safari / firefox
YTDLP_COOKIES_FILE = _get("YTDLP_COOKIES_FILE") or _get("YOUTUBE_COOKIES_FILE")
YTDLP_COOKIES_FROM_BROWSER = (
    _get("YTDLP_COOKIES_FROM_BROWSER") or _get("YOUTUBE_COOKIES_FROM_BROWSER")
)

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
