"""
Поиск песен по фрагменту текста через Genius API / публичный search.
Также resolve Genius URL (альбом / артист / трек).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus, unquote

import aiohttp

from cache import get_session, search_cache_get, search_cache_set
from config import GENIUS_ACCESS_TOKEN
from utils import ParsedUrl, slug_to_title

logger = logging.getLogger(__name__)


class LyricsError(Exception):
    pass


@dataclass(frozen=True)
class LyricHit:
    title: str
    artist: str
    url: str = ""
    album: str = ""
    cover_url: str = ""
    index: int = 0

    @property
    def button_label(self) -> str:
        label = f"{self.title} – {self.artist}"
        return label if len(label) <= 64 else label[:61] + "…"


@dataclass(frozen=True)
class GeniusResolved:
    """Нормализованный результат разбора Genius-ссылки."""

    kind: str  # album | artist | track
    artist: str
    title: str = ""
    album: str = ""
    cover_url: str = ""
    url: str = ""
    query: str = ""  # готовая строка для поиска в iTunes и т.д.


async def search_by_lyrics(query: str, *, limit: int = 20) -> list[LyricHit]:
    """
    Ищет песни по фрагменту текста / названию на Genius.
    Сначала api.genius.com (если есть токен), иначе genius.com/api/search.
    """
    q = (query or "").strip()
    if not q:
        return []

    cache_key = f"lyrics:v1:{q.lower()}:{limit}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    session = await get_session()
    results: list[LyricHit] = []

    if GENIUS_ACCESS_TOKEN:
        try:
            results = await _search_official(session, q, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Genius official search failed: %s", exc)

    if not results:
        try:
            results = await _search_public(session, q, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Genius public search failed: %s", exc)
            raise LyricsError(
                "Не удалось найти песни по тексту. Попробуйте другое написание."
            ) from exc

    search_cache_set(cache_key, results)
    return results


async def _search_official(
    session: aiohttp.ClientSession, query: str, *, limit: int
) -> list[LyricHit]:
    headers = {
        "Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    async with session.get(
        "https://api.genius.com/search",
        params={"q": query},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise LyricsError(f"Genius API HTTP {resp.status}: {body[:120]}")
        payload = await resp.json(content_type=None)

    hits = ((payload.get("response") or {}).get("hits")) or []
    return _parse_hits(hits, limit=limit)


async def _search_public(
    session: aiohttp.ClientSession, query: str, *, limit: int
) -> list[LyricHit]:
    """Публичный endpoint сайта (без ключа)."""
    url = f"https://genius.com/api/search?q={quote_plus(query)}"
    async with session.get(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://genius.com/",
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise LyricsError(f"Genius public HTTP {resp.status}: {body[:120]}")
        payload = await resp.json(content_type=None)

    # response.sections[].hits  или response.hits
    response = payload.get("response") or payload
    hits: list = []
    if response.get("hits"):
        hits = response["hits"]
    else:
        for sec in response.get("sections") or []:
            if (sec.get("type") or "") in {"song", "top_hit", ""}:
                hits.extend(sec.get("hits") or [])
        if not hits:
            for sec in response.get("sections") or []:
                hits.extend(sec.get("hits") or [])

    return _parse_hits(hits, limit=limit)


def _parse_hits(hits: list, *, limit: int) -> list[LyricHit]:
    out: list[LyricHit] = []
    seen: set[str] = set()
    for row in hits:
        if not isinstance(row, dict):
            continue
        result = row.get("result") if "result" in row else row
        if not isinstance(result, dict):
            continue
        # только песни
        if row.get("type") and row.get("type") != "song":
            if result.get("ty") not in (None, "song") and "title" not in result:
                continue
        title = (result.get("title") or result.get("title_with_featured") or "").strip()
        if not title:
            continue
        artist = ""
        primary = result.get("primary_artist") or {}
        if isinstance(primary, dict):
            artist = (primary.get("name") or "").strip()
        if not artist:
            artist = (result.get("artist_names") or "").strip()
        key = f"{artist.lower()}|{title.lower()}"
        if key in seen:
            continue
        seen.add(key)
        cover = (
            result.get("song_art_image_thumbnail_url")
            or result.get("song_art_image_url")
            or ""
        )
        out.append(
            LyricHit(
                title=title,
                artist=artist or "Unknown",
                url=result.get("url") or "",
                album="",
                cover_url=cover,
                index=len(out),
            )
        )
        if len(out) >= limit:
            break
    logger.info("Genius lyrics search: %d hits", len(out))
    return out


async def resolve_genius_url(parsed: ParsedUrl) -> Optional[GeniusResolved]:
    """Разбирает genius.com ссылку в artist/title для единого поиска бота."""
    if parsed.platform != "genius":
        return None
    session = await get_session()
    entity = parsed.entity
    eid = parsed.entity_id or ""

    if entity == "album" and "/" in eid:
        artist_slug, album_slug = eid.split("/", 1)
        artist = slug_to_title(unquote(artist_slug))
        album = slug_to_title(unquote(album_slug))
        # уточнить через multi-search
        hit = await _genius_multi_best(
            session, f"{artist} {album}", prefer="album"
        )
        if hit:
            return hit
        return GeniusResolved(
            kind="album",
            artist=artist,
            title=album,
            album=album,
            url=parsed.original,
            query=f"{artist} {album}".strip(),
        )

    if entity == "artist" and eid:
        artist = slug_to_title(unquote(eid))
        hit = await _genius_multi_best(session, artist, prefer="artist")
        if hit:
            return hit
        return GeniusResolved(
            kind="artist",
            artist=artist,
            url=parsed.original,
            query=artist,
        )

    if entity == "song" and eid:
        if eid.isdigit() and GENIUS_ACCESS_TOKEN:
            try:
                song = await _genius_api_song(session, eid)
                if song:
                    return song
            except Exception as exc:  # noqa: BLE001
                logger.debug("genius song api: %s", exc)
        # slug или fallback search
        q = slug_to_title(unquote(eid)) if not eid.isdigit() else ""
        if not q:
            q = parsed.original
        hit = await _genius_multi_best(session, q, prefer="song")
        if hit:
            return hit

    # неизвестный тип — общий поиск по URL/хвосту
    hit = await _genius_multi_best(session, parsed.original, prefer="song")
    return hit


async def _genius_api_song(
    session: aiohttp.ClientSession, song_id: str
) -> Optional[GeniusResolved]:
    headers = {
        "Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}",
        "Accept": "application/json",
    }
    async with session.get(
        f"https://api.genius.com/songs/{song_id}",
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            return None
        payload = await resp.json(content_type=None)
    song = ((payload.get("response") or {}).get("song")) or {}
    title = (song.get("title") or "").strip()
    artist = ""
    primary = song.get("primary_artist") or {}
    if isinstance(primary, dict):
        artist = (primary.get("name") or "").strip()
    album_name = ""
    alb = song.get("album") or {}
    if isinstance(alb, dict):
        album_name = (alb.get("name") or "").strip()
    if not title:
        return None
    return GeniusResolved(
        kind="track",
        artist=artist,
        title=title,
        album=album_name,
        cover_url=song.get("song_art_image_url") or "",
        url=song.get("url") or "",
        query=f"{artist} {title}".strip(),
    )


async def _genius_multi_best(
    session: aiohttp.ClientSession,
    query: str,
    *,
    prefer: str = "song",
) -> Optional[GeniusResolved]:
    q = (query or "").strip()
    if not q:
        return None
    url = f"https://genius.com/api/search/multi?q={quote_plus(q)}"
    try:
        async with session.get(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                "Referer": "https://genius.com/",
            },
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("genius multi: %s", exc)
        return None

    sections = ((payload.get("response") or {}).get("sections")) or []
    order = {
        "album": ("album", "top_hit", "song", "artist"),
        "artist": ("artist", "top_hit", "album", "song"),
        "song": ("song", "top_hit", "album", "artist"),
    }.get(prefer, ("top_hit", "song", "album", "artist"))

    by_type: dict[str, list[dict[str, Any]]] = {}
    for sec in sections:
        typ = (sec.get("type") or "").strip()
        by_type.setdefault(typ, []).extend(sec.get("hits") or [])

    for typ in order:
        for row in by_type.get(typ, []):
            resolved = _hit_to_resolved(row, typ)
            if resolved:
                return resolved
    # любой hit
    for sec in sections:
        for row in sec.get("hits") or []:
            resolved = _hit_to_resolved(row, sec.get("type") or "")
            if resolved:
                return resolved
    return None


def _hit_to_resolved(row: dict[str, Any], typ: str) -> Optional[GeniusResolved]:
    if not isinstance(row, dict):
        return None
    result = row.get("result") if "result" in row else row
    if not isinstance(result, dict):
        return None
    hit_type = (row.get("type") or typ or "").lower()

    if hit_type in {"album", "top_hit"} and (
        result.get("name") or result.get("_type") == "album" or "/albums/" in (result.get("url") or "")
    ):
        name = (result.get("name") or result.get("full_title") or "").strip()
        artist = ""
        primary = result.get("artist") or result.get("primary_artist") or {}
        if isinstance(primary, dict):
            artist = (primary.get("name") or "").strip()
        if not artist and " by " in name.lower():
            # "Album by Artist"
            parts = name.rsplit(" by ", 1)
            if len(parts) == 2:
                name, artist = parts[0].strip(), parts[1].strip()
        if name:
            return GeniusResolved(
                kind="album",
                artist=artist,
                title=name,
                album=name,
                cover_url=result.get("cover_art_url")
                or result.get("song_art_image_url")
                or "",
                url=result.get("url") or "",
                query=f"{artist} {name}".strip(),
            )

    if hit_type == "artist" or result.get("_type") == "artist":
        artist = (result.get("name") or "").strip()
        if artist:
            return GeniusResolved(
                kind="artist",
                artist=artist,
                url=result.get("url") or "",
                query=artist,
            )

    title = (result.get("title") or result.get("title_with_featured") or "").strip()
    if not title:
        return None
    artist = ""
    primary = result.get("primary_artist") or {}
    if isinstance(primary, dict):
        artist = (primary.get("name") or "").strip()
    if not artist:
        artist = (result.get("artist_names") or "").strip()
    return GeniusResolved(
        kind="track",
        artist=artist,
        title=title,
        cover_url=result.get("song_art_image_thumbnail_url")
        or result.get("song_art_image_url")
        or "",
        url=result.get("url") or "",
        query=f"{artist} {title}".strip(),
    )
