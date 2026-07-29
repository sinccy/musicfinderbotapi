"""Вспомогательные функции: HTML, ошибки, URL-детекция, форматирование."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import quote_plus, urlparse

# --- Ссылки на платформы / ID ---

URL_RE = re.compile(
    r"https?://[^\s<>\"']+",
    re.IGNORECASE,
)

ITUNES_ID_RE = re.compile(
    r"(?:id|/album/[^/]+/|/song/[^/]+/)(\d{6,})",
    re.IGNORECASE,
)
ITUNES_LOOKUP_ID_RE = re.compile(r"[?&]id=(\d+)", re.IGNORECASE)
ITUNES_SONG_I_RE = re.compile(r"[?&]i=(\d{6,})", re.IGNORECASE)

SPOTIFY_ID_RE = re.compile(
    r"(?:open\.spotify\.com|spotify\.link)/(?:intl-[a-z]{2}/)?"
    r"(album|track|artist)/([a-zA-Z0-9]{22})",
    re.IGNORECASE,
)
SPOTIFY_URI_RE = re.compile(
    r"spotify:(album|track|artist):([a-zA-Z0-9]{22})",
    re.IGNORECASE,
)

YANDEX_ID_RE = re.compile(
    r"music\.yandex\.(?:ru|com)/(?:album|track)/(\d+)",
    re.IGNORECASE,
)
YANDEX_ALBUM_TRACK_RE = re.compile(
    r"music\.yandex\.(?:ru|com)/album/(\d+)/track/(\d+)",
    re.IGNORECASE,
)
SOUNDCLOUD_RE = re.compile(
    r"(?:soundcloud\.com|on\.soundcloud\.com)/([^\s?#]+)",
    re.IGNORECASE,
)

YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)([\w-]{6,})",
    re.IGNORECASE,
)
YOUTUBE_ID_ONLY_RE = re.compile(r"^[\w-]{11}$")

GENIUS_ALBUM_RE = re.compile(
    r"genius\.com/albums/([^/?#]+)/([^/?#]+)",
    re.IGNORECASE,
)
GENIUS_ARTIST_RE = re.compile(
    r"genius\.com/artists/([^/?#]+)",
    re.IGNORECASE,
)
GENIUS_SONG_ID_RE = re.compile(
    r"genius\.com/songs/(\d+)",
    re.IGNORECASE,
)
GENIUS_SONG_SLUG_RE = re.compile(
    r"genius\.com/([A-Za-z0-9-]+)-lyrics(?:\?|$|#)",
    re.IGNORECASE,
)

YANDEX_ARTIST_RE = re.compile(
    r"music\.yandex\.(?:ru|com)/artist/(\d+)",
    re.IGNORECASE,
)

Platform = Literal[
    "itunes", "spotify", "yandex", "youtube", "genius", "soundcloud", "unknown"
]
QueryKind = Literal["artist", "album", "both", "ambiguous", "combo"]

# ISO → emoji для подписей (условные иконки у артистов)
_FLAG_BY_CC = {
    "ru": "🇷🇺",
    "us": "🇺🇸",
    "gb": "🇬🇧",
    "uk": "🇬🇧",
    "de": "🇩🇪",
    "fr": "🇫🇷",
    "jp": "🇯🇵",
    "br": "🇧🇷",
    "ua": "🇺🇦",
    "kz": "🇰🇿",
    "by": "🇧🇾",
    "pl": "🇵🇱",
}

# стоп-слова, типичные для названий альбомов
_ALBUM_STOPS = {
    "the",
    "a",
    "an",
    "of",
    "and",
    "&",
    "vol",
    "volume",
    "ep",
    "lp",
    "ost",
    "deluxe",
    "remastered",
    "edition",
    "soundtrack",
    "live",
}


def classify_query(query: str) -> QueryKind:
    """
    Эвристика: artist / album / combo / both / ambiguous.
    - 1 слово → album
    - 2+ слов «как имя» → artist (или ambiguous)
    - 3+ слов без стоп-слов → combo (артист + альбом)
    """
    q = (query or "").strip()
    if not q:
        return "both"
    words = q.split()
    lower = [w.lower().strip(".,!?:;\"'") for w in words]

    if len(words) == 1:
        return "album"

    has_stop = any(w in _ALBUM_STOPS for w in lower)
    if has_stop and len(words) <= 4:
        return "album"

    # 3+ слов: вероятно «Artist Album Title»
    if len(words) >= 3 and not has_stop:
        return "combo"

    # 2 слова: имя артиста или короткое название альбома
    if len(words) == 2:
        # оба слова буквенные → неоднозначно
        alphaish = all(re.sub(r"[-']", "", w).isalpha() for w in words)
        if alphaish:
            return "ambiguous"
        return "both"

    return "both"


def split_artist_album_query(query: str) -> list[tuple[str, str]]:
    """
    Варианты разбиения «Artist … Album».
    Сначала короткие префиксы артиста (1–2 слова) — обычно точнее.
    """
    words = (query or "").strip().split()
    if len(words) < 2:
        return []
    pairs: list[tuple[str, str]] = []
    # 1 слово артиста → 2 → … → n-1
    for i in range(1, len(words)):
        artist = " ".join(words[:i]).strip()
        album = " ".join(words[i:]).strip()
        if artist and album:
            pairs.append((artist, album))
    return pairs


def slug_to_title(slug: str) -> str:
    """Villian / Beautiful-evil → читаемые слова."""
    s = (slug or "").replace("-", " ").replace("_", " ").strip()
    return re.sub(r"\s+", " ", s)


def flag_emoji(country_or_hint: str = "") -> str:
    """Флаг по ISO-коду или эвристике по тексту (кириллица → 🇷🇺)."""
    raw = (country_or_hint or "").strip()
    if len(raw) == 2 and raw.isalpha():
        return _FLAG_BY_CC.get(raw.lower(), "🌍")
    if re.search(r"[А-Яа-яЁёІіЇїЄє]", raw):
        return "🇷🇺"
    return "🌍"


@dataclass(frozen=True)
class ParsedUrl:
    """Разобранная музыкальная ссылка."""

    platform: Platform
    entity: str  # album | track | artist | video | unknown
    entity_id: str
    original: str


def escape_html(text: str) -> str:
    """Экранирует HTML для Telegram parse_mode=HTML."""
    return html.escape(text or "", quote=False)


def format_error(message: str) -> str:
    """Единый стиль сообщений об ошибках (на русском)."""
    return f"⚠️ {message}"


def truncate(text: str, limit: int = 3500) -> str:
    """Обрезает длинный текст под лимит Telegram (~4096)."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def youtube_search_url(query: str) -> str:
    """Бесплатная ссылка на поиск трека на YouTube (без API-ключа)."""
    return f"https://www.youtube.com/results?search_query={quote_plus(query)}"


def extract_youtube_video_id(url_or_id: str) -> str:
    """Достаёт video id из любой youtube/youtu.be ссылки (игнорирует list=)."""
    raw = (url_or_id or "").strip()
    if not raw:
        return ""
    m = YOUTUBE_RE.search(raw)
    if m:
        return m.group(1)
    # чистый id
    if YOUTUBE_ID_ONLY_RE.match(raw):
        return raw
    return ""


def normalize_youtube_watch_url(url_or_id: str) -> str:
    """
    Чистый watch?v=ID без playlist/radio/start_radio.
    Иначе yt-dlp/бот может уехать в другой ролик из list=.
    """
    vid = extract_youtube_video_id(url_or_id)
    if not vid:
        return (url_or_id or "").strip()
    return f"https://www.youtube.com/watch?v={vid}"


def yandex_search_url(query: str) -> str:
    """Ссылка на поиск в Яндекс.Музыке."""
    return f"https://music.yandex.ru/search?text={quote_plus(query)}"


def spotify_search_web_url(query: str) -> str:
    """Ссылка на веб-поиск Spotify (fallback без API)."""
    return f"https://open.spotify.com/search/{quote_plus(query)}"


def soundcloud_search_url(query: str) -> str:
    """Ссылка на поиск в SoundCloud (треки)."""
    return f"https://soundcloud.com/search/sounds?q={quote_plus(query)}"


def soundcloud_albums_search_url(query: str) -> str:
    """Ссылка на поиск альбомов/плейлистов в SoundCloud."""
    return f"https://soundcloud.com/search/albums?q={quote_plus(query)}"


def extract_first_url(text: str) -> Optional[str]:
    """Возвращает первый URL из текста или None."""
    if not text:
        return None
    match = URL_RE.search(text.strip())
    return match.group(0).rstrip(").,;]") if match else None


def parse_music_url(url: str) -> Optional[ParsedUrl]:
    """
    Определяет платформу и ID сущности из URL.
    Apple / Spotify / Яндекс / YouTube / Genius.
    """
    if not url:
        return None

    raw = url.strip()
    host = (urlparse(raw).netloc or "").lower()

    # Genius
    if "genius.com" in host:
        m = GENIUS_ALBUM_RE.search(raw)
        if m:
            return ParsedUrl(
                platform="genius",
                entity="album",
                entity_id=f"{m.group(1)}/{m.group(2)}",
                original=raw,
            )
        m = GENIUS_ARTIST_RE.search(raw)
        if m:
            return ParsedUrl(
                platform="genius",
                entity="artist",
                entity_id=m.group(1),
                original=raw,
            )
        m = GENIUS_SONG_ID_RE.search(raw)
        if m:
            return ParsedUrl(
                platform="genius",
                entity="song",
                entity_id=m.group(1),
                original=raw,
            )
        m = GENIUS_SONG_SLUG_RE.search(raw)
        if m:
            return ParsedUrl(
                platform="genius",
                entity="song",
                entity_id=m.group(1),
                original=raw,
            )
        return ParsedUrl(
            platform="genius", entity="unknown", entity_id="", original=raw
        )

    # Spotify (в т.ч. spotify.link / URI — после follow redirects)
    sp = SPOTIFY_ID_RE.search(raw) or SPOTIFY_URI_RE.search(raw)
    if sp or "spotify.com" in host or "spotify.link" in host:
        if sp:
            return ParsedUrl(
                platform="spotify",
                entity=sp.group(1).lower(),
                entity_id=sp.group(2),
                original=raw,
            )
        return ParsedUrl(platform="spotify", entity="unknown", entity_id="", original=raw)

    # YouTube
    yt = YOUTUBE_RE.search(raw)
    if yt or "youtube.com" in host or "youtu.be" in host or "music.youtube.com" in host:
        vid = yt.group(1) if yt else ""
        return ParsedUrl(
            platform="youtube",
            entity="video",
            entity_id=vid,
            original=raw,
        )

    # SoundCloud
    sc = SOUNDCLOUD_RE.search(raw)
    if sc or "soundcloud.com" in host:
        slug = (sc.group(1) if sc else "").strip("/")
        entity = "track"
        if slug and "/" not in slug:
            entity = "artist"
        return ParsedUrl(
            platform="soundcloud",
            entity=entity,
            entity_id=slug,
            original=raw,
        )

    # Yandex Music
    ya_art = YANDEX_ARTIST_RE.search(raw)
    if ya_art:
        return ParsedUrl(
            platform="yandex",
            entity="artist",
            entity_id=ya_art.group(1),
            original=raw,
        )
    ya_at = YANDEX_ALBUM_TRACK_RE.search(raw)
    if ya_at:
        return ParsedUrl(
            platform="yandex",
            entity="track",
            entity_id=ya_at.group(2),
            original=raw,
        )
    ya = YANDEX_ID_RE.search(raw)
    if ya or "music.yandex" in host:
        entity = "album"
        if "/track/" in raw:
            entity = "track"
        elif "/artist/" in raw:
            entity = "artist"
        return ParsedUrl(
            platform="yandex",
            entity=entity,
            entity_id=ya.group(1) if ya else "",
            original=raw,
        )

    # Apple Music / iTunes
    if "apple.com" in host or "itunes.apple.com" in host or "music.apple.com" in host:
        mid = None
        song_i = ITUNES_SONG_I_RE.search(raw)
        # ?i=SONG_ID — конкретный трек на странице альбома
        if song_i:
            mid = song_i.group(1)
            entity = "track"
        else:
            m = ITUNES_LOOKUP_ID_RE.search(raw)
            if m:
                mid = m.group(1)
            else:
                m2 = ITUNES_ID_RE.search(raw)
                if m2:
                    mid = m2.group(1)
            entity = "album"
            if "/song/" in raw:
                entity = "track"
            elif "/artist/" in raw:
                entity = "artist"
        return ParsedUrl(
            platform="itunes",
            entity=entity,
            entity_id=mid or "",
            original=raw,
        )

    return ParsedUrl(platform="unknown", entity="unknown", entity_id="", original=raw)


def country_code(raw: Optional[str], default: str = "ru") -> str:
    """Нормализует код страны (ISO 3166-1 alpha-2)."""
    if not raw:
        return default
    code = raw.strip().lower()
    if len(code) == 2 and code.isalpha():
        return code
    return default
