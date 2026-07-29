"""
Ссылки на платформы прослушивания:
  • Apple Music — из iTunes (collectionViewUrl / trackViewUrl)
  • Spotify — Client Credentials + Search API (free tier)
  • YouTube — yt-dlp ytsearch (бесплатно) или search URL
  • Яндекс.Музыка — yandex-music (токен опционален) или search URL

Spotify rate limit ≈ 180 req/min — кэшируем access token.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import aiohttp

from cache import get_session
from utils import (
    escape_html,
    soundcloud_albums_search_url,
    soundcloud_search_url,
    spotify_search_web_url,
    truncate,
    yandex_search_url,
    youtube_search_url,
)

logger = logging.getLogger(__name__)

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_SEARCH_URL = "https://api.spotify.com/v1/search"
SPOTIFY_ALBUM_URL = "https://api.spotify.com/v1/albums/{id}"
SPOTIFY_TRACK_URL = "https://api.spotify.com/v1/tracks/{id}"


@dataclass
class PlatformLinks:
    """Набор ссылок на платформы + превью."""

    apple_music: str = ""
    spotify: str = ""
    youtube: str = ""
    yandex: str = ""
    soundcloud: str = ""
    preview_url: str = ""
    query_used: str = ""


class _SpotifyTokenCache:
    access_token: str = ""
    expires_at: float = 0.0


_spotify_cache = _SpotifyTokenCache()


def _spotify_credentials() -> tuple[str, str]:
    client_id = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    return client_id, client_secret


async def _spotify_access_token(
    session: aiohttp.ClientSession,
) -> Optional[str]:
    """OAuth Client Credentials. Возвращает None, если ключи не заданы."""
    client_id, client_secret = _spotify_credentials()
    if not client_id or not client_secret:
        return None

    now = time.time()
    if _spotify_cache.access_token and now < _spotify_cache.expires_at - 30:
        return _spotify_cache.access_token

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}

    try:
        async with session.post(
            SPOTIFY_TOKEN_URL,
            headers=headers,
            data=data,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.error("Spotify token HTTP %s: %s", resp.status, body[:200])
                return None
            payload = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        logger.exception("Spotify token network: %s", exc)
        return None

    token = payload.get("access_token") or ""
    expires_in = int(payload.get("expires_in") or 3600)
    if not token:
        return None
    _spotify_cache.access_token = token
    _spotify_cache.expires_at = time.time() + expires_in
    logger.info("Spotify access token обновлён (ttl=%ss)", expires_in)
    return token


async def spotify_search_link(
    query: str,
    *,
    entity: str = "album",
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """
    Ищет альбом/трек в Spotify и возвращает внешний URL.
    При ошибке/отсутствии ключей — веб-поиск Spotify.
    """
    query = (query or "").strip()
    if not query:
        return ""

    fallback = spotify_search_web_url(query)
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()

    try:
        assert session is not None
        token = await _spotify_access_token(session)
        if not token:
            return fallback

        search_type = "track" if entity == "track" else "album"
        headers = {"Authorization": f"Bearer {token}"}
        items: list[Any] = []
        for market in ("RU", "US"):
            params = {
                "q": query,
                "type": search_type,
                "limit": "1",
                "market": market,
            }
            async with session.get(
                SPOTIFY_SEARCH_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    logger.warning("Spotify rate limit (429) — используем web search")
                    return fallback
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("Spotify search HTTP %s: %s", resp.status, body[:200])
                    continue
                payload = await resp.json(content_type=None)
            key = "tracks" if search_type == "track" else "albums"
            items = ((payload.get(key) or {}).get("items")) or []
            if items:
                break
        if not items:
            return fallback
        external = (items[0].get("external_urls") or {}).get("spotify") or ""
        return external or fallback
    except aiohttp.ClientError as exc:
        logger.exception("Spotify search network: %s", exc)
        return fallback
    finally:
        if owns and session is not None:
            await session.close()


async def spotify_lookup(
    entity: str,
    entity_id: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
) -> Optional[dict[str, Any]]:
    """Lookup альбома/трека Spotify по ID. Нужны SPOTIFY_CLIENT_*."""
    if not entity_id or entity not in {"album", "track"}:
        return None

    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        assert session is not None
        token = await _spotify_access_token(session)
        if not token:
            return None
        url = (
            SPOTIFY_TRACK_URL.format(id=entity_id)
            if entity == "track"
            else SPOTIFY_ALBUM_URL.format(id=entity_id)
        )
        headers = {"Authorization": f"Bearer {token}"}
        for market in ("RU", "US"):
            async with session.get(
                url,
                headers=headers,
                params={"market": market},
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                body = await resp.text()
                logger.error(
                    "Spotify lookup HTTP %s market=%s: %s",
                    resp.status,
                    market,
                    body[:200],
                )
        return None
    except aiohttp.ClientError as exc:
        logger.exception("Spotify lookup: %s", exc)
        return None
    finally:
        if owns and session is not None:
            await session.close()


def _youtube_search_sync(query: str) -> str:
    """Синхронный поиск первого ролика через yt-dlp."""
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        logger.warning("yt-dlp не установлен — fallback на YouTube search URL")
        return youtube_search_url(query)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "skip_download": True,
        "default_search": "ytsearch1",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=False)
        entries = (info or {}).get("entries") or []
        if not entries:
            return youtube_search_url(query)
        entry = entries[0] or {}
        video_id = entry.get("id") or ""
        url = entry.get("url") or entry.get("webpage_url") or ""
        if video_id and not url.startswith("http"):
            return f"https://www.youtube.com/watch?v={video_id}"
        if url.startswith("http"):
            return url
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("yt-dlp search failed: %s", exc)
    return youtube_search_url(query)


async def youtube_link(query: str) -> str:
    """Асинхронная обёртка над yt-dlp (с коротким таймаутом)."""
    query = (query or "").strip()
    if not query:
        return ""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_youtube_search_sync, query),
            timeout=4,
        )
    except Exception:  # noqa: BLE001
        return youtube_search_url(query)


def _yandex_search_sync(query: str, token: str) -> str:
    """Поиск альбома в Яндекс.Музыке (официальная неофициальная библиотека)."""
    try:
        from yandex_music import Client  # type: ignore
    except ImportError:
        logger.warning("yandex-music не установлен")
        return yandex_search_url(query)

    try:
        client = Client(token).init() if token else Client().init()
        result = client.search(query)
        if result and result.best and result.best.type == "album" and result.best.result:
            album = result.best.result
            album_id = getattr(album, "id", None)
            if album_id:
                return f"https://music.yandex.ru/album/{album_id}"
        if result and result.albums and result.albums.results:
            album = result.albums.results[0]
            album_id = getattr(album, "id", None)
            if album_id:
                return f"https://music.yandex.ru/album/{album_id}"
        if result and result.tracks and result.tracks.results:
            track = result.tracks.results[0]
            track_id = getattr(track, "id", None)
            albums = getattr(track, "albums", None) or []
            album_id = albums[0].id if albums else None
            if track_id and album_id:
                return f"https://music.yandex.ru/album/{album_id}/track/{track_id}"
            if track_id:
                return f"https://music.yandex.ru/track/{track_id}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Yandex Music search failed: %s", exc)
    return yandex_search_url(query)


async def yandex_link(query: str) -> str:
    """Ссылка на Яндекс.Музыку (токен опционален)."""
    query = (query or "").strip()
    if not query:
        return ""
    token = os.getenv("YANDEX_MUSIC_TOKEN", "").strip()
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_yandex_search_sync, query, token),
            timeout=4,
        )
    except Exception:  # noqa: BLE001
        return yandex_search_url(query)


async def soundcloud_link(
    query: str,
    *,
    artist: str = "",
    title: str = "",
    entity: str = "track",
) -> str:
    """
    Ссылка SoundCloud на поиск треков/альбомов — НЕ на профиль пользователя.
    Фанатские аккаунты с именем альбома (как «Скучаю но ещё работаю») отсекаются:
    для альбомов сразу ведём в search/albums.
    """
    query = (query or "").strip()
    artist = (artist or "").strip()
    title = (title or "").strip()
    if artist and title:
        query = f"{artist} {title}".strip()
    elif not query and (artist or title):
        query = f"{artist} {title}".strip()
    if not query:
        return ""
    is_album = entity in {"album", "playlist", "set"}
    # Надёжный UX: фильтрованный поиск. scsearch часто отдаёт фан-профили
    # с названием альбома вместо релиза.
    if is_album:
        return soundcloud_albums_search_url(query)
    return soundcloud_search_url(query)


async def soundcloud_search_tracks(
    query: str, *, limit: int = 5, artist: str = ""
) -> list[dict[str, str]]:
    """Поиск треков SoundCloud через yt-dlp (для артистов вне Apple)."""
    query = (query or "").strip()
    if not query:
        return []

    def _sc_sync() -> list[dict[str, str]]:
        try:
            import yt_dlp  # type: ignore
        except ImportError:
            return []
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 10,
        }
        out: list[dict[str, str]] = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"scsearch{max(limit * 2, 8)}:{query}", download=False
                )
            art_l = (artist or "").lower()
            ranked: list[tuple[float, dict[str, str]]] = []
            for entry in (info or {}).get("entries") or []:
                if not entry:
                    continue
                title = (entry.get("title") or "").strip()
                uploader = (
                    entry.get("uploader")
                    or entry.get("creator")
                    or entry.get("channel")
                    or ""
                ).strip()
                url = (
                    entry.get("webpage_url") or entry.get("url") or ""
                ).strip()
                if not title:
                    continue
                if url.startswith("https://api.soundcloud.com"):
                    sid = str(entry.get("id") or "")
                    # не профиль
                    url = ""
                if not url.startswith("http"):
                    sid = str(entry.get("id") or "")
                    if "/" in sid:
                        url = f"https://soundcloud.com/{sid}"
                    else:
                        continue
                # отсечь профили
                if re.match(
                    r"^https?://(?:www\.)?soundcloud\.com/[^/?#]+/?$",
                    url,
                    flags=re.I,
                ):
                    continue
                score = 0.0
                if art_l and uploader.lower() == art_l:
                    score += 20
                elif art_l and art_l in uploader.lower():
                    score += 10
                if art_l and art_l in title.lower():
                    score += 8
                # фанатский акк с именем из query
                if uploader and uploader.lower() in query.lower() and len(uploader) > 8:
                    score -= 15
                ranked.append(
                    (
                        score,
                        {
                            "title": title,
                            "artist": uploader,
                            "url": url,
                            "query": f"{uploader} {title}".strip() or title,
                        },
                    )
                )
            ranked.sort(key=lambda x: -x[0])
            out = [row for _, row in ranked[:limit]]
        except Exception as exc:  # noqa: BLE001
            logger.warning("soundcloud_search_tracks: %s", exc)
        return out

    try:
        return await asyncio.wait_for(asyncio.to_thread(_sc_sync), timeout=10)
    except Exception:  # noqa: BLE001
        return []


async def itunes_preview_url(
    artist: str,
    title: str,
    *,
    session: Optional[aiohttp.ClientSession] = None,
    country: str = "ru",
) -> str:
    """30-сек превью из iTunes Search (https audio URL)."""
    query = f"{artist} {title}".strip()
    if not query:
        return ""
    owns = session is None
    if owns:
        session = aiohttp.ClientSession()
    try:
        assert session is not None
        for cc in (country, "us", "ru"):
            if not cc:
                continue
            params = {
                "term": query,
                "media": "music",
                "entity": "song",
                "limit": "5",
                "country": cc.lower(),
            }
            try:
                async with session.get(
                    "https://itunes.apple.com/search",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("itunes preview search: %s", exc)
                continue
            title_l = (title or "").lower().strip()
            artist_l = (artist or "").lower().strip()
            best = ""
            for item in payload.get("results") or []:
                prev = (item.get("previewUrl") or "").strip()
                if not prev.startswith("http"):
                    continue
                tn = (item.get("trackName") or "").lower()
                an = (item.get("artistName") or "").lower()
                if title_l and title_l in tn and (
                    not artist_l or artist_l in an or an in artist_l
                ):
                    return prev
                if not best:
                    best = prev
            if best:
                return best
        return ""
    finally:
        if owns and session is not None:
            await session.close()


async def build_platform_links(
    *,
    artist: str,
    title: str,
    apple_url: str = "",
    preview_url: str = "",
    entity: str = "album",
    session: Optional[aiohttp.ClientSession] = None,
) -> PlatformLinks:
    """Собирает ссылки Apple / Spotify / YouTube / Yandex / SoundCloud + превью."""
    query = f"{artist} {title}".strip()
    if session is None:
        session = await get_session()

    spotify_url, yt_url, ya_url, sc_url = await asyncio.gather(
        spotify_search_link(query, entity=entity, session=session),
        youtube_link(query),
        yandex_link(query),
        soundcloud_link(
            query, artist=artist, title=title, entity=entity
        ),
    )

    preview = (preview_url or "").strip()
    if not preview.startswith("http"):
        try:
            preview = await asyncio.wait_for(
                itunes_preview_url(
                    artist, title, session=session, country="ru"
                ),
                timeout=6,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("preview fetch: %s", exc)
            preview = ""

    return PlatformLinks(
        apple_music=apple_url or "",
        spotify=spotify_url or spotify_search_web_url(query),
        youtube=yt_url or youtube_search_url(query),
        yandex=ya_url or yandex_search_url(query),
        soundcloud=sc_url or soundcloud_search_url(query),
        preview_url=preview or "",
        query_used=query,
    )


@dataclass
class UrlMeta:
    """Метаданные, вытащенные из внешней ссылки."""

    artist: str = ""
    title: str = ""
    album: str = ""
    cover: str = ""
    query: str = ""
    final_url: str = ""
    prefer: str = "track"  # track | album


async def follow_redirects(url: str, *, timeout: float = 8) -> str:
    """Раскрывает short-link (spotify.link, on.soundcloud.com, …)."""
    url = (url or "").strip()
    if not url:
        return ""
    host = (urlparse(url).netloc or "").lower()
    # YouTube / прямые music URL — не качаем HTML (1MB+) и не зависаем
    if any(
        h in host
        for h in (
            "youtu.be",
            "youtube.com",
            "music.youtube.com",
            "open.spotify.com",
            "music.apple.com",
            "itunes.apple.com",
            "music.yandex.",
            "soundcloud.com",
            "genius.com",
        )
    ):
        return url
    session = await get_session()
    try:
        async with session.get(
            url,
            allow_redirects=True,
            timeout=aiohttp.ClientTimeout(total=timeout),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.5 Safari/605.1.15"
                ),
                "Accept": "text/html,application/json,*/*;q=0.8",
            },
        ) as resp:
            return str(resp.url) or url
    except Exception as exc:  # noqa: BLE001
        logger.warning("follow_redirects: %s", exc)
        return url

def _parse_oembed_title(title: str, author: str = "") -> tuple[str, str]:
    """'Flying Bird by Boris Brejcha' / 'Artist - Title' → (artist, title)."""
    t = (title or "").strip()
    a = (author or "").strip()
    if not t:
        return a, ""
    m = re.search(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", t, flags=re.I)
    if m:
        return (m.group("artist").strip(), m.group("title").strip())
    for sep in (" – ", " — ", " - ", " | "):
        if sep in t:
            left, right = t.split(sep, 1)
            left, right = left.strip(), right.strip()
            if a and a.lower() in left.lower():
                return a or left, right
            if a and a.lower() in right.lower():
                return a or right, left
            # чаще Artist - Title
            if len(left) <= len(right) + 8:
                return left, right
            return right, left
    return a, t


async def _oembed_json(oembed_url: str) -> Optional[dict[str, Any]]:
    session = await get_session()
    try:
        async with session.get(
            oembed_url,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("oembed failed: %s", exc)
        return None


async def soundcloud_url_meta(url: str) -> Optional[UrlMeta]:
    """Мета SoundCloud через oEmbed (+ варианты URL / slug)."""
    url = (url or "").strip()
    if not url:
        return None
    final = await follow_redirects(url)
    candidates: list[str] = []
    for u in (final, url):
        if u and u not in candidates:
            candidates.append(u)
            if u.startswith("http://"):
                candidates.append("https://" + u[len("http://") :])

    # часто slug без дефисов 404 → пробуем user с дефисами (camel/сдвоенные слова)
    extra: list[str] = []
    for u in candidates:
        m = re.search(
            r"(https?://(?:www\.)?soundcloud\.com/)([^/?#]+)/([^/?#]+)",
            u,
            flags=re.I,
        )
        if not m:
            continue
        base, user, track = m.group(1), m.group(2), m.group(3)
        if "-" not in user and len(user) > 6:
            # borisbrejcha → boris-brejcha (эвристика по заглавным нет — делим по частым стыкам)
            spaced = re.sub(
                r"([a-z])([A-Z])", r"\1-\2", user
            )  # CamelCase
            if spaced == user:
                # вставляем дефис перед известными хвостами / каждые «словоподобные» куски
                spaced = re.sub(
                    r"(brejcha|breicha|music|official|beats|records)$",
                    r"-\1",
                    user,
                    flags=re.I,
                )
            if spaced != user:
                extra.append(f"{base}{spaced}/{track}")
            # ещё вариант: первые N букв + остаток (boris + brejcha)
            for cut in (5, 6, 7, 8):
                if 3 < cut < len(user) - 2:
                    extra.append(f"{base}{user[:cut]}-{user[cut:]}/{track}")
    for u in extra:
        if u not in candidates:
            candidates.append(u)

    data = None
    used = final or url
    for u in candidates:
        oembed = (
            "https://soundcloud.com/oembed?format=json&url=" + quote_plus(u)
        )
        data = await _oembed_json(oembed)
        if data and (data.get("title") or data.get("author_name")):
            used = u
            break

    if data:
        author = (data.get("author_name") or "").strip()
        raw_title = (data.get("title") or "").strip()
        art, tit = _parse_oembed_title(raw_title, author)
        cover = (data.get("thumbnail_url") or "").strip()
        query = f"{art} {tit}".strip() or raw_title
        return UrlMeta(
            artist=art,
            title=tit or raw_title,
            cover=cover,
            query=query,
            final_url=used,
            prefer="track",
        )

    # fallback: slug user/track-name
    m = re.search(
        r"soundcloud\.com/([^/?#]+)/([^/?#]+)", used or url, flags=re.I
    )
    if not m:
        return None
    user = m.group(1).replace("-", " ").strip()
    track = m.group(2).replace("-", " ").strip()
    if user.lower() in {"sets", "you", "discover", "search", "tags"}:
        return None
    return UrlMeta(
        artist=user,
        title=track,
        query=f"{user} {track}".strip(),
        final_url=used or url,
        prefer="track",
    )


async def spotify_url_meta(url: str) -> Optional[UrlMeta]:
    """Мета Spotify через oEmbed (без Client ID)."""
    url = (url or "").strip()
    if not url:
        return None
    final = await follow_redirects(url)
    oembed = "https://open.spotify.com/oembed?url=" + quote_plus(final)
    data = await _oembed_json(oembed)
    if not data:
        return None
    raw_title = (data.get("title") or "").strip()
    author = (data.get("author_name") or "").strip()
    art, tit = _parse_oembed_title(raw_title, author)
    # Spotify oEmbed часто только название трека
    if not art and "·" in raw_title:
        parts = [p.strip() for p in raw_title.split("·")]
        if len(parts) >= 2:
            tit, art = parts[0], parts[-1]
    prefer = "album" if "/album/" in final else "track"
    return UrlMeta(
        artist=art,
        title=tit or raw_title,
        cover=(data.get("thumbnail_url") or "").strip(),
        query=f"{art} {tit or raw_title}".strip(),
        final_url=final,
        prefer=prefer,
    )


async def resolve_any_url_meta(url: str) -> Optional[UrlMeta]:
    """Универсальный разбор: redirect → oEmbed SoundCloud/Spotify → slug."""
    from utils import parse_music_url

    final = await follow_redirects(url)
    parsed = parse_music_url(final) or parse_music_url(url)
    if not parsed:
        return None
    if parsed.platform == "soundcloud":
        return await soundcloud_url_meta(final or url)
    if parsed.platform == "spotify":
        meta = await spotify_url_meta(final or url)
        if meta:
            return meta
    return None


def format_platform_links(links: PlatformLinks) -> str:
    """HTML-блок со ссылками на платформы."""
    rows: list[str] = ["<b>Слушать:</b>"]
    if links.apple_music:
        rows.append(
            f'🍎 <a href="{escape_html(links.apple_music)}">Apple Music</a>'
        )
    if links.spotify:
        rows.append(f'💚 <a href="{escape_html(links.spotify)}">Spotify</a>')
    if links.youtube:
        rows.append(f'▶️ <a href="{escape_html(links.youtube)}">YouTube</a>')
    if links.yandex:
        rows.append(f'🟡 <a href="{escape_html(links.yandex)}">Яндекс.Музыка</a>')
    if links.soundcloud:
        rows.append(f'☁ <a href="{escape_html(links.soundcloud)}">SoundCloud</a>')
    if links.preview_url:
        rows.append(
            f'🎧 <a href="{escape_html(links.preview_url)}">Превью 30 сек</a>'
        )
    if len(rows) == 1:
        rows.append("<i>Ссылки не найдены.</i>")
    return truncate("\n".join(rows), limit=1000)
