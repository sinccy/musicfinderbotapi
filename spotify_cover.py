"""
Точный поиск релиза Spotify по обложке.

Spotify API не умеет reverse-image по всему каталогу.
Пайплайн:
  1) из поиска по картинке достаём ТОЛЬКО ссылки open.spotify.com/(album|track)/…
  2) тянем официальную обложку релиза через Spotify Web API
  3) сравниваем pHash/dHash с фото пользователя
  4) возвращаем результат ТОЛЬКО при почти идентичном совпадении

Без «объектов на картинке», без списка похожих кандидатов.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import aiohttp

from cache import get_session
from config import SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET
from cover_match import compute_hashes, distance
from reverse_image import find_spotify_links_from_image

logger = logging.getLogger(__name__)

# почти идентичные обложки (скрин/ресайз/сжатие JPEG)
EXACT_DISTANCE_MAX = 4


@dataclass(frozen=True)
class SpotifyCoverMatch:
    album_id: str
    name: str
    artists: str
    spotify_url: str
    artwork_url: str
    release_date: str
    distance: int
    album_type: str = "album"


class SpotifyCoverError(Exception):
    """Нет ключей / API недоступен."""


def spotify_configured() -> bool:
    return bool(SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET)


async def find_exact_spotify_cover(
    image_bytes: bytes,
    *,
    max_candidates: int = 15,
) -> Optional[SpotifyCoverMatch]:
    """
    Найти релиз Spotify с официальной обложкой == фото.
    None = точного совпадения нет.
    """
    if not image_bytes:
        return None
    if not spotify_configured():
        raise SpotifyCoverError(
            "Для поиска по обложке нужны SPOTIFY_CLIENT_ID и "
            "SPOTIFY_CLIENT_SECRET в .env\n"
            "https://developer.spotify.com/dashboard"
        )

    try:
        user_hashes = compute_hashes(image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.warning("user hash failed: %s", exc)
        return None

    links = await find_spotify_links_from_image(image_bytes)
    album_ids = await _resolve_album_ids(links, limit=max_candidates)

    # Если прямых spotify.com/album ссылок мало — расширяем кандидатов
    # через Spotify Search (только как индекс). В выдачу попадёт
    # ТОЛЬКО релиз, чья официальная обложка совпала по hash.
    if len(album_ids) < 3:
        try:
            from reverse_image import reverse_cover_queries

            tags = await reverse_cover_queries(image_bytes, limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cover tags for spotify search: %s", exc)
            tags = []
        for tag in tags:
            for aid in await _spotify_search_album_ids(tag, limit=3):
                if aid not in album_ids:
                    album_ids.append(aid)
                if len(album_ids) >= max_candidates:
                    break
            if len(album_ids) >= max_candidates:
                break

    if not album_ids:
        logger.info("spotify_cover: нет кандидатов-релизов Spotify")
        return None

    logger.info("spotify_cover candidates: %s", album_ids)
    best: Optional[SpotifyCoverMatch] = None

    for album_id in album_ids:
        album = await _spotify_get_album(album_id)
        if not album:
            continue
        art_url = _best_image_url(album.get("images") or [])
        if not art_url:
            continue
        art_bytes = await _fetch_bytes(art_url)
        if not art_bytes:
            continue
        try:
            cover_hashes = compute_hashes(art_bytes)
            dist = distance(user_hashes, cover_hashes)
        except Exception as exc:  # noqa: BLE001
            logger.debug("hash album %s: %s", album_id, exc)
            continue

        logger.info(
            "spotify_cover dist=%s %s – %s",
            dist,
            _artists_str(album),
            album.get("name"),
        )
        if dist > EXACT_DISTANCE_MAX:
            continue
        match = SpotifyCoverMatch(
            album_id=album_id,
            name=(album.get("name") or "").strip(),
            artists=_artists_str(album),
            spotify_url=(
                (album.get("external_urls") or {}).get("spotify")
                or f"https://open.spotify.com/album/{album_id}"
            ),
            artwork_url=art_url,
            release_date=(album.get("release_date") or "")[:10],
            distance=dist,
            album_type=(album.get("album_type") or "album"),
        )
        if best is None or match.distance < best.distance:
            best = match
            if best.distance == 0:
                break

    return best


async def _resolve_album_ids(
    links: list[tuple[str, str]],
    *,
    limit: int,
) -> list[str]:
    """(entity, id) → уникальные album_id."""
    out: list[str] = []
    seen: set[str] = set()
    for entity, eid in links:
        album_id = eid
        if entity == "track":
            track = await _spotify_get_track(eid)
            if not track:
                continue
            album_id = ((track.get("album") or {}).get("id") or "").strip()
        if not album_id or album_id in seen:
            continue
        seen.add(album_id)
        out.append(album_id)
        if len(out) >= limit:
            break
    return out


def _artists_str(album: dict) -> str:
    names = [
        (a.get("name") or "").strip()
        for a in (album.get("artists") or [])
        if a.get("name")
    ]
    return ", ".join(names)


def _best_image_url(images: list) -> str:
    if not images:
        return ""
    # Spotify: widest first обычно
    best = max(images, key=lambda im: int(im.get("width") or 0))
    return (best.get("url") or "").strip()


async def _fetch_bytes(url: str) -> Optional[bytes]:
    session = await get_session()
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=15, connect=6)
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            return data if len(data) > 200 else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch art: %s", exc)
        return None


async def _spotify_token() -> Optional[str]:
    from links import _spotify_access_token

    session = await get_session()
    return await _spotify_access_token(session)


async def _spotify_get_album(album_id: str) -> Optional[dict]:
    token = await _spotify_token()
    if not token:
        return None
    session = await get_session()
    try:
        async with session.get(
            f"https://api.spotify.com/v1/albums/{album_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"market": "RU"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("spotify album %s: %s", album_id, exc)
        return None


async def _spotify_get_track(track_id: str) -> Optional[dict]:
    token = await _spotify_token()
    if not token:
        return None
    session = await get_session()
    try:
        async with session.get(
            f"https://api.spotify.com/v1/tracks/{track_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"market": "RU"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("spotify track %s: %s", track_id, exc)
        return None


async def _spotify_search_album_ids(query: str, *, limit: int = 3) -> list[str]:
    query = (query or "").strip()
    if len(query) < 3:
        return []
    token = await _spotify_token()
    if not token:
        return []
    session = await get_session()
    try:
        async with session.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "q": query,
                "type": "album",
                "limit": str(min(limit, 10)),
                "market": "RU",
            },
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("spotify search %r: %s", query, exc)
        return []
    items = ((payload.get("albums") or {}).get("items")) or []
    out: list[str] = []
    for item in items:
        aid = (item.get("id") or "").strip()
        if aid:
            out.append(aid)
    return out[:limit]
