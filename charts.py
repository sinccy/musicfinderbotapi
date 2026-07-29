"""
Топ-чарты Genius (scraping / публичный chart API) + fallback Apple RSS.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp
from bs4 import BeautifulSoup

from cache import charts_cache_get, charts_cache_set, get_session
from utils import escape_html, truncate

logger = logging.getLogger(__name__)

APPLE_RSS_BASE = "https://rss.marketingtools.apple.com/api/v2"
APPLE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


class ChartsError(Exception):
    pass


@dataclass(frozen=True)
class ChartSong:
    position: int
    title: str
    artist: str
    url: str = ""
    cover_url: str = ""
    index: int = 0  # 0-based в полном списке


async def get_genius_charts(
    country: str = "global",
    *,
    limit: int = 20,
) -> tuple[list[ChartSong], str]:
    """Публичный alias: региональные / global топы Genius (+ Apple fallback)."""
    return await fetch_genius_chart(country, limit=limit)


async def fetch_genius_chart(
    country: str = "global",
    *,
    limit: int = 20,
) -> tuple[list[ChartSong], str]:
    """
    Возвращает (songs, source_label).
    country: global | ru | us | gb | de | fr ...
    """
    country = (country or "global").lower()
    cache_key = f"genius:v2:{country}:{limit}"
    hit = charts_cache_get(cache_key)
    if hit is not None:
        return hit

    errors: list[str] = []

    # 1) Genius public chart API (регион через country_code)
    try:
        songs = await _genius_api_chart(country, limit=limit)
        if songs:
            label = (
                "Genius Charts API (Global)"
                if country == "global"
                else f"Genius Charts API ({country.upper()})"
            )
            result = (songs, label)
            charts_cache_set(cache_key, result)
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Genius API chart failed (%s): %s", country, exc)
        errors.append(f"api: {exc}")

    # 2) HTML scrape (#top-songs?country=)
    try:
        songs = await _genius_scrape_chart(country, limit=limit)
        if songs:
            result = (songs, f"Genius.com HTML ({country})")
            charts_cache_set(cache_key, result)
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Genius scrape failed (%s): %s", country, exc)
        errors.append(f"scrape: {exc}")

    # 3) Fallback Apple most-played (региональный)
    try:
        cc = "us" if country == "global" else country
        songs = await _apple_top_songs(cc, limit=limit)
        if songs:
            result = (
                songs,
                f"Apple Music most-played ({cc.upper()}) — "
                "Genius недоступен, использую Apple Music",
            )
            charts_cache_set(cache_key, result)
            return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("Apple fallback failed (%s): %s", country, exc)
        errors.append(f"apple: {exc}")

    raise ChartsError(
        "❌ Не удалось загрузить чарты Genius и Apple Music.\n"
        f"Детали: {'; '.join(errors)[:250]}"
    )


async def _genius_api_chart(country: str, *, limit: int) -> list[ChartSong]:
    session = await get_session()
    # Неофициальный endpoint сайта Genius
    params: dict[str, str] = {
        "time_period": "day",
        "chart_genre": "all",
        "per_page": str(min(limit, 50)),
        "page": "1",
    }
    # Региональные чарты: country_code=RU / US / GB / DE / FR
    if country and country != "global":
        params["country_code"] = country.upper()

    url = "https://genius.com/api/songs/chart"
    async with session.get(
        url,
        params=params,
        headers={
            **APPLE_HEADERS,
            "Accept": "application/json",
            "Referer": "https://genius.com/",
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as resp:
        if resp.status != 200:
            body = await resp.text()
            raise ChartsError(f"Genius API HTTP {resp.status}: {body[:100]}")
        payload = await resp.json(content_type=None)

    # структура: response.chart_items[].item
    response = payload.get("response") or payload
    items = response.get("chart_items") or response.get("songs") or []
    songs: list[ChartSong] = []
    for idx, row in enumerate(items[:limit]):
        item = row.get("item") if isinstance(row, dict) and "item" in row else row
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or item.get("title_with_featured") or "").strip()
        artist = ""
        primary = item.get("primary_artist") or {}
        if isinstance(primary, dict):
            artist = (primary.get("name") or "").strip()
        if not artist:
            artist = (item.get("artist_names") or "").strip()
        if not title:
            continue
        cover = ""
        if item.get("song_art_image_thumbnail_url"):
            cover = item["song_art_image_thumbnail_url"]
        elif item.get("song_art_image_url"):
            cover = item["song_art_image_url"]
        songs.append(
            ChartSong(
                position=idx + 1,
                title=title,
                artist=artist or "Unknown",
                url=item.get("url") or "",
                cover_url=cover,
                index=idx,
            )
        )
    return songs


async def _genius_scrape_chart(country: str, *, limit: int) -> list[ChartSong]:
    session = await get_session()
    # #top-songs — hash на сервер не уходит; региональность через ?country=
    if country == "global":
        urls = ["https://genius.com/"]
    else:
        code = quote_plus(country.lower())
        urls = [
            f"https://genius.com/?country={code}",
            f"https://genius.com/songs?for_country={code.upper()}",
        ]

    html = ""
    for url in urls:
        try:
            async with session.get(
                url,
                headers=APPLE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    logger.warning("Genius HTML %s → HTTP %s", url, resp.status)
                    continue
                html = await resp.text()
                if html:
                    break
        except Exception as exc:  # noqa: BLE001
            logger.warning("Genius HTML fetch %s: %s", url, exc)

    if not html:
        raise ChartsError("Genius HTML пустой ответ")

    soup = BeautifulSoup(html, "lxml")
    songs: list[ChartSong] = []

    # Вариант 1: блоки чарта
    selectors = [
        ".chart_item",
        "div[class*='ChartSong']",
        "a[class*='ChartSongdesktop']",
        "div.ChartItemdesktop__Metadata-sc",
        "div[class*='chart_row']",
        "div[class*='ChartItem']",
    ]
    nodes = []
    for sel in selectors:
        nodes = soup.select(sel)
        if nodes:
            break

    if not nodes:
        # Вариант 2: ссылки /songs/ или lyrics pages в топе
        for a in soup.select("a[href*='lyrics'], a[href*='-lyrics']")[: limit * 2]:
            text = a.get_text(" ", strip=True)
            href = a.get("href") or ""
            if not text or len(text) < 3:
                continue
            # "Artist – Title" или "Title by Artist"
            artist, title = _split_artist_title(text)
            if not title:
                continue
            if href.startswith("/"):
                href = "https://genius.com" + href
            songs.append(
                ChartSong(
                    position=len(songs) + 1,
                    title=title,
                    artist=artist or "Unknown",
                    url=href,
                    index=len(songs),
                )
            )
            if len(songs) >= limit:
                break
        return songs

    for node in nodes[:limit]:
        text = node.get_text(" ", strip=True)
        link = node.get("href") if node.name == "a" else None
        if not link:
            a = node.find("a")
            link = a.get("href") if a else ""
        if link and link.startswith("/"):
            link = "https://genius.com" + link
        artist, title = _split_artist_title(text)
        # иногда внутри отдельные spans
        if not title:
            title_el = node.select_one("[class*='Title'], h3, h4")
            artist_el = node.select_one("[class*='Artist'], h4, span")
            title = title_el.get_text(strip=True) if title_el else text
            artist = artist_el.get_text(strip=True) if artist_el else ""
        if not title:
            continue
        songs.append(
            ChartSong(
                position=len(songs) + 1,
                title=title,
                artist=artist or "Unknown",
                url=link or "",
                index=len(songs),
            )
        )
    return songs[:limit]


def _split_artist_title(text: str) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    for sep in (" – ", " — ", " - ", " | "):
        if sep in text:
            left, right = text.split(sep, 1)
            # Genius часто "Title Artist" в одном блоке; эвристика:
            if len(left) < len(right):
                return left.strip(), right.strip()
            return right.strip(), left.strip()
    if " by " in text.lower():
        parts = re.split(r"\s+by\s+", text, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            return parts[1].strip(), parts[0].strip()
    return "", text


async def _apple_top_songs(country: str, *, limit: int) -> list[ChartSong]:
    url = f"{APPLE_RSS_BASE}/{country}/music/most-played/{limit}/songs.json"
    session = await get_session()
    async with session.get(
        url,
        headers=APPLE_HEADERS,
        timeout=aiohttp.ClientTimeout(total=8),
    ) as resp:
        if resp.status != 200:
            # classic iTunes RSS
            itunes = (
                f"https://itunes.apple.com/{country}/rss/topsongs/limit={limit}/json"
            )
            async with session.get(
                itunes, headers=APPLE_HEADERS, timeout=aiohttp.ClientTimeout(total=8)
            ) as r2:
                if r2.status != 200:
                    raise ChartsError(f"Apple songs HTTP {resp.status}/{r2.status}")
                payload = await r2.json(content_type=None)
            return _parse_itunes_songs(payload, limit=limit)
        payload = await resp.json(content_type=None)

    results = (payload.get("feed") or {}).get("results") or []
    songs: list[ChartSong] = []
    for idx, item in enumerate(results[:limit]):
        name = (item.get("name") or "").strip()
        artist = (item.get("artistName") or "").strip()
        if not name:
            continue
        art = item.get("artwork") or {}
        cover = ""
        if isinstance(art, dict):
            cover = (art.get("url") or "").replace("{w}", "200").replace("{h}", "200")
        songs.append(
            ChartSong(
                position=idx + 1,
                title=name,
                artist=artist or "Unknown",
                url=item.get("url") or "",
                cover_url=cover,
                index=idx,
            )
        )
    return songs


def _parse_itunes_songs(payload: dict[str, Any], *, limit: int) -> list[ChartSong]:
    entries = (payload.get("feed") or {}).get("entry") or []
    if isinstance(entries, dict):
        entries = [entries]
    songs: list[ChartSong] = []
    for idx, entry in enumerate(entries[:limit]):
        name = ((entry.get("im:name") or {}).get("label")) or ""
        artist = ((entry.get("im:artist") or {}).get("label")) or ""
        if not name:
            continue
        images = entry.get("im:image") or []
        if isinstance(images, dict):
            images = [images]
        cover = (images[-1].get("label") if images else "") or ""
        link = ""
        links = entry.get("link")
        if isinstance(links, dict):
            link = (links.get("attributes") or {}).get("href") or ""
        songs.append(
            ChartSong(
                position=idx + 1,
                title=name,
                artist=artist or "Unknown",
                url=link,
                cover_url=cover,
                index=idx,
            )
        )
    return songs


def format_chart_text(
    songs: list[ChartSong],
    *,
    title: str,
    country: str,
    source: str = "",
    page: int = 1,
    total: int = 0,
) -> str:
    lines = [f"<b>{escape_html(title)}</b>"]
    cc = (country or "").lower()
    if cc and cc != "global":
        lines.append(f"Регион: <code>{escape_html(country.upper())}</code>")
    if source:
        lines.append(f"Источник: <i>{escape_html(source)}</i>")
    if total:
        lines.append(f"Страница {page} · всего {total}")
    lines.append("")
    for s in songs:
        row = (
            f"<b>{s.position}.</b> {escape_html(s.title)} — "
            f"<i>{escape_html(s.artist)}</i>"
        )
        if s.url:
            row = (
                f"<b>{s.position}.</b> "
                f'<a href="{escape_html(s.url)}">{escape_html(s.title)}</a> — '
                f"<i>{escape_html(s.artist)}</i>"
            )
        lines.append(row)
    return truncate("\n".join(lines), limit=3500)


# --- совместимость со старым bot (newreleases) ---


@dataclass(frozen=True)
class ChartItem:
    position: int
    name: str
    artist: str
    artwork_url: str = ""
    url: str = ""
    release_date: str = ""
    source_id: str = ""


def _parse_release_day(raw: str) -> Optional[str]:
    """Дата релиза в календарном дне Europe/Moscow (YYYY-MM-DD)."""
    from datetime import datetime, timedelta, timezone

    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        if len(raw) >= 19 and ("T" in raw or " " in raw[10:11]):
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                # Яндекс часто отдаёт локальное MSK без tz
                dt = dt.replace(tzinfo=timezone(timedelta(hours=3)))
            msk = timezone(timedelta(hours=3))
            return dt.astimezone(msk).date().isoformat()
        return datetime.strptime(raw[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _release_within(raw: str, days: int = 7, *, upcoming_days: int = 14) -> bool:
    from datetime import date, timedelta

    day = _parse_release_day(raw)
    if not day:
        return False
    try:
        from datetime import datetime as _dt

        d = _dt.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        return False
    today = date.today()
    return today - timedelta(days=days) <= d <= today + timedelta(days=upcoming_days)


def _dedupe_releases(chunks: list[list[ChartItem]]) -> list[ChartItem]:
    """Склеивает списки релизов: первый источник имеет приоритет при дублях."""
    seen: set[str] = set()
    out: list[ChartItem] = []
    for chunk in chunks:
        for x in chunk:
            key = x.source_id or f"{x.name.casefold()}|{x.artist.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            out.append(x)
    out.sort(key=lambda x: x.release_date, reverse=True)
    return [
        ChartItem(
            position=i + 1,
            name=x.name,
            artist=x.artist,
            artwork_url=x.artwork_url,
            url=x.url,
            release_date=x.release_date,
            source_id=x.source_id,
        )
        for i, x in enumerate(out)
    ]


_YANDEX_CLIENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "X-Yandex-Music-Client": "YandexMusicDesktopAppWindows/5.23.2",
}

# Официальные подборки music-blog «Громкие новинки» (шире, чем лендинг)
_YANDEX_LOUD_PLAYLISTS: tuple[tuple[int, int], ...] = (
    (103372440, 1175),  # месяца
    (103372440, 2441),  # рэп
    (103372440, 2440),  # поп
    (103372440, 2466),  # электроника
    (103372440, 2464),  # инди
    (103372440, 1176),  # потяжелее
    (103372440, 2650),  # танцевальная
)


def _album_to_chart_item(alb: Any, *, artists_fallback: str = "") -> Optional[ChartItem]:
    if not alb:
        return None
    rd = _parse_release_day(getattr(alb, "release_date", None) or "")
    if not rd:
        return None
    name = (getattr(alb, "title", None) or "").strip()
    if not name:
        return None
    artists = ", ".join(
        a.name for a in (getattr(alb, "artists", None) or []) if getattr(a, "name", None)
    )
    artists = artists or artists_fallback
    cover = ""
    try:
        cover = alb.get_cover_url("300x300") or ""
    except Exception:  # noqa: BLE001
        cover = ""
    aid = getattr(alb, "id", None)
    return ChartItem(
        position=0,
        name=name,
        artist=artists,
        artwork_url=cover,
        url=f"https://music.yandex.ru/album/{aid}" if aid else "",
        release_date=rd,
        source_id=str(aid or ""),
    )


async def _yandex_landing_new_releases(*, days: int = 7) -> list[ChartItem]:
    """Официальный блок new-releases API Яндекс.Музыки."""
    session = await get_session()
    try:
        async with session.get(
            "https://api.music.yandex.net/landing-blocks/new-releases",
            headers=_YANDEX_CLIENT_HEADERS,
            params={"language": "ru", "count": 100},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("yandex landing new-releases HTTP %s", resp.status)
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex landing new-releases: %s", exc)
        return []

    rows = payload.get("newReleases") or []
    album_ids: list[str] = []
    meta: dict[str, tuple[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        alb = row.get("album") or {}
        aid = alb.get("id")
        if not aid:
            continue
        sid = str(aid)
        if sid not in album_ids:
            album_ids.append(sid)
        arts = ", ".join(
            a.get("name", "") for a in (row.get("artists") or []) if a.get("name")
        )
        rd = _parse_release_day(row.get("releaseDate") or "") or ""
        meta[sid] = (arts, rd)

    if not album_ids:
        return []

    import asyncio

    def _fetch(ids: list[str]) -> list[Any]:
        try:
            from yandex_music import Client
        except ImportError:
            return []
        client = Client().init()
        out: list[Any] = []
        for i in range(0, len(ids), 50):
            try:
                out.extend(client.albums(ids[i : i + 50]) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning("yandex albums: %s", exc)
        return out

    albums = await asyncio.to_thread(_fetch, album_ids)
    items: list[ChartItem] = []
    for alb in albums:
        item = _album_to_chart_item(alb)
        if not item:
            # fallback на дату из лендинга (UTC→MSK уже учтён)
            sid = str(getattr(alb, "id", "") or "")
            arts, rd = meta.get(sid, ("", ""))
            if not rd or not _release_within(rd, days):
                continue
            name = (getattr(alb, "title", None) or "").strip()
            if not name:
                continue
            item = ChartItem(
                position=0,
                name=name,
                artist=arts,
                artwork_url="",
                url=f"https://music.yandex.ru/album/{sid}" if sid else "",
                release_date=rd,
                source_id=sid,
            )
        if not _release_within(item.release_date, days):
            continue
        items.append(item)
    return items


async def _yandex_playlist_new_releases(*, days: int = 7) -> list[ChartItem]:
    """«Громкие новинки» по жанрам — больше RU + зарубежные, когда они в подборках."""
    import asyncio

    def _load() -> list[ChartItem]:
        try:
            from yandex_music import Client
        except ImportError:
            logger.warning("yandex-music не установлен")
            return []
        client = Client().init()
        by_id: dict[str, ChartItem] = {}
        for owner_uid, kind in _YANDEX_LOUD_PLAYLISTS:
            try:
                playlist = client.users_playlists(kind, owner_uid)
                tracks = playlist.fetch_tracks() if playlist else []
            except Exception as exc:  # noqa: BLE001
                logger.warning("yandex playlist %s:%s: %s", owner_uid, kind, exc)
                continue
            for short in tracks or []:
                track = getattr(short, "track", None) or short
                if not track:
                    continue
                albums = getattr(track, "albums", None) or []
                alb = albums[0] if albums else None
                arts = ", ".join(
                    a.name
                    for a in (getattr(track, "artists", None) or [])
                    if getattr(a, "name", None)
                )
                item = _album_to_chart_item(alb, artists_fallback=arts)
                if not item or not _release_within(item.release_date, days):
                    continue
                if item.source_id and item.source_id not in by_id:
                    by_id[item.source_id] = item
        return list(by_id.values())

    return await asyncio.to_thread(_load)


async def _yandex_new_releases(*, days: int = 7, limit: int = 50) -> list[ChartItem]:
    """Недельные релизы из Яндекс.Музыки (лендинг + громкие новинки)."""
    landing = await _yandex_landing_new_releases(days=days)
    playlists = await _yandex_playlist_new_releases(days=days)
    # плейлисты первыми: там чаще свежий рэп/поп и крупные зарубежные
    merged = _dedupe_releases([playlists, landing])
    return merged[:limit]


# Крупные артисты, чьи альбомы часто не попадают в RU-лендинг Яндекса
_ITUNES_WATCH_ARTIST_IDS: tuple[int, ...] = (
    128050210,  # Future
    271256,  # Drake
    271364,  # Kanye West / Ye
    183313439,  # The Weeknd
    278873738,  # Travis Scott
    894820141,  # Playboi Carti
    2822050,  # Eminem
    394291128,  # Post Malone
    265981158,  # Tyler, The Creator
    358714210,  # Billie Eilish
    282076859,  # Ariana Grande
    159260351,  # Taylor Swift
    183598771,  # Rihanna
    2715720,  # Beyoncé
    976291,  # Coldplay
    368183298,  # Kendrick Lamar
    331663014,  # Rosalía
    1065981054,  # Carti / alt ids ignored if empty
)


async def _itunes_watched_new_releases(
    country: str = "us",
    *,
    days: int = 21,
    limit: int = 50,
) -> list[ChartItem]:
    """Свежие альбомы крупных артистов через iTunes Lookup (Future и т.п.)."""
    import asyncio

    from cache import get_session as _gs

    cc = (country or "us").lower()
    if cc in {"global", ""}:
        cc = "us"
    session = await _gs()
    out: list[ChartItem] = []

    async def _one(aid: int) -> list[ChartItem]:
        url = "https://itunes.apple.com/lookup"
        params = {
            "id": str(aid),
            "entity": "album",
            "limit": "50",
            "country": cc,
        }
        try:
            async with session.get(
                url,
                params=params,
                headers=APPLE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("itunes watch %s: %s", aid, exc)
            return []
        raw = [
            item
            for item in (payload.get("results") or [])
            if item.get("wrapperType") != "artist"
        ]
        raw.sort(key=lambda x: (x.get("releaseDate") or ""), reverse=True)
        items: list[ChartItem] = []
        for item in raw:
            name = (item.get("collectionName") or "").strip()
            artist = (item.get("artistName") or "").strip()
            if not name or not artist:
                continue
            release = (item.get("releaseDate") or "")[:10]
            if not release or not _release_within(
                release, days, upcoming_days=14
            ):
                continue
            art_url = (item.get("artworkUrl100") or "").replace(
                "100x100bb", "200x200bb"
            )
            cid = item.get("collectionId")
            items.append(
                ChartItem(
                    position=0,
                    name=name,
                    artist=artist,
                    artwork_url=art_url,
                    url=item.get("collectionViewUrl") or "",
                    release_date=release,
                    source_id=str(cid) if cid else "",
                )
            )
        return items

    ids = [i for i in _ITUNES_WATCH_ARTIST_IDS if i < 10**10]
    results = await asyncio.gather(*[_one(aid) for aid in ids[:24]])
    for chunk in results:
        out.extend(chunk)
    out.sort(key=lambda x: x.release_date, reverse=True)
    return out[:limit]


async def _itunes_rss_recent_albums(
    countries: tuple[str, ...] = ("us", "ru", "gb", "de", "fr"),
    *,
    days: int = 21,
    limit: int = 80,
) -> list[ChartItem]:
    """
    Свежие альбомы из iTunes Top Albums RSS по нескольким странам.
    Берём только те, у которых releaseDate в окне days — так попадают
    новинки разных артистов, не только watchlist.
    """
    import asyncio

    session = await get_session()

    async def _country(cc: str) -> list[ChartItem]:
        url = f"https://itunes.apple.com/{cc}/rss/topalbums/limit=100/json"
        try:
            async with session.get(
                url,
                headers=APPLE_HEADERS,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    return []
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("itunes rss %s: %s", cc, exc)
            return []
        entries = ((payload.get("feed") or {}).get("entry")) or []
        if isinstance(entries, dict):
            entries = [entries]
        out: list[ChartItem] = []
        for entry in entries:
            name = ((entry.get("im:name") or {}).get("label") or "").strip()
            artist = ((entry.get("im:artist") or {}).get("label") or "").strip()
            if not name or not artist:
                continue
            release = (
                (entry.get("im:releaseDate") or {}).get("label") or ""
            )[:10]
            if not release or not _release_within(
                release, days, upcoming_days=14
            ):
                continue
            images = entry.get("im:image") or []
            if isinstance(images, dict):
                images = [images]
            cover = (images[-1].get("label") if images else "") or ""
            link = ""
            links = entry.get("link")
            if isinstance(links, dict):
                link = (links.get("attributes") or {}).get("href") or ""
            elif isinstance(links, list) and links:
                link = (links[0].get("attributes") or {}).get("href") or ""
            # id из id.label / attributes
            sid = ""
            id_node = entry.get("id") or {}
            if isinstance(id_node, dict):
                sid = (id_node.get("attributes") or {}).get("im:id") or ""
                if not sid:
                    label = id_node.get("label") or ""
                    m = re.search(r"/id(\d+)", label)
                    if m:
                        sid = m.group(1)
            out.append(
                ChartItem(
                    position=0,
                    name=name,
                    artist=artist,
                    artwork_url=cover,
                    url=link,
                    release_date=release,
                    source_id=str(sid) if sid else "",
                )
            )
        return out

    chunks = await asyncio.gather(*[_country(cc) for cc in countries])
    return _dedupe_releases(list(chunks))[:limit]


async def fetch_new_releases(country: str = "ru", **_kwargs: Any):
    """Свежие релизы: Яндекс + iTunes RSS (много стран) + watchlist артистов."""
    del _kwargs
    cc = (country or "ru").lower()
    window = 21
    errors: list[str] = []

    ya: list[ChartItem] = []
    rss: list[ChartItem] = []
    watched: list[ChartItem] = []

    try:
        ya = await _yandex_new_releases(days=21, limit=60)
    except Exception as exc:  # noqa: BLE001
        logger.exception("yandex new releases failed: %s", exc)
        errors.append(f"yandex: {exc}")

    try:
        # своя страна + крупные сторы — шире покрытие артистов
        countries = []
        if cc and cc not in {"global"}:
            countries.append(cc)
        for extra in ("us", "ru", "gb", "de", "fr"):
            if extra not in countries:
                countries.append(extra)
        rss = await _itunes_rss_recent_albums(
            tuple(countries[:5]), days=21, limit=100
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("itunes rss releases: %s", exc)
        errors.append(f"rss: {exc}")

    try:
        watched = await _itunes_watched_new_releases("us", days=21, limit=40)
    except Exception as exc:  # noqa: BLE001
        logger.warning("itunes watched releases: %s", exc)
        errors.append(f"itunes: {exc}")

    # RSS + Яндекс по дате; watchlist докидывает пропуски (Future и т.п.)
    merged = _dedupe_releases([rss, ya, watched])
    pin: list[ChartItem] = []
    if watched:
        seen_in_top = {
            (x.source_id or f"{x.name.casefold()}|{x.artist.casefold()}")
            for x in merged[:40]
        }
        pin = [
            w
            for w in watched
            if (
                w.source_id
                or f"{w.name.casefold()}|{w.artist.casefold()}"
            )
            not in seen_in_top
        ][:10]

    tail_sorted = sorted(
        merged, key=lambda x: x.release_date or "", reverse=True
    )
    final = pin + tail_sorted
    seen_keys: set[str] = set()
    items: list[ChartItem] = []
    for x in final:
        key = x.source_id or f"{x.name.casefold()}|{x.artist.casefold()}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append(
            ChartItem(
                position=len(items) + 1,
                name=x.name,
                artist=x.artist,
                artwork_url=x.artwork_url,
                url=x.url,
                release_date=x.release_date,
                source_id=x.source_id,
            )
        )
        if len(items) >= 50:
            break

    if not items:
        raise ChartsError(
            "Свежих альбомов не найдено. "
            "Попробуйте /top или поиск по названию."
            + (f" ({'; '.join(errors[:2])})" if errors else "")
        )
    sources = []
    if ya:
        sources.append("Яндекс")
    if rss:
        sources.append("iTunes")
    source = f"{' + '.join(sources) or 'каталоги'} · новые релизы (~{window} дн.)"
    return items, source


async def fetch_top_chart(country: str = "ru", chart_type: str = "songs", **_k):
    songs, _src = await fetch_genius_chart(
        "global" if country == "global" else country, limit=20
    )
    return [
        ChartItem(
            position=s.position,
            name=s.title,
            artist=s.artist,
            artwork_url=s.cover_url,
            url=s.url,
        )
        for s in songs
    ]


def paginate(items: list, offset: int, page_size: int = 10) -> list:
    chunk = items[offset : offset + page_size]
    out = []
    for i, x in enumerate(chunk, start=1):
        if isinstance(x, ChartItem):
            out.append(
                ChartItem(
                    position=offset + i,
                    name=x.name,
                    artist=x.artist,
                    artwork_url=x.artwork_url,
                    url=x.url,
                    release_date=x.release_date,
                    source_id=x.source_id,
                )
            )
        else:
            out.append(x)
    return out


def format_chart_list(items, *, title, country, source="", page=1, total=0):
    songs = [
        ChartSong(
            position=getattr(i, "position", 0),
            title=getattr(i, "name", getattr(i, "title", "")),
            artist=getattr(i, "artist", ""),
            url=getattr(i, "url", ""),
            cover_url=getattr(i, "artwork_url", ""),
            index=getattr(i, "position", 1) - 1,
        )
        for i in items
    ]
    return format_chart_text(
        songs,
        title=title,
        country=country,
        source=source,
        page=page,
        total=total,
    )
