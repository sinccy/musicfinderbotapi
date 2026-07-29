"""
Поиск альбомов: iTunes + MusicBrainz (async, параллельно, с кэшем).

Всегда передаём country= в iTunes.
Максимум 3 кандидатных запроса. Таймаут запроса ~5с.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import aiohttp

from cache import get_session, search_cache_get, search_cache_set
from utils import escape_html, flag_emoji, truncate, youtube_search_url

logger = logging.getLogger(__name__)

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"
MUSICBRAINZ_RELEASE_URL = "https://musicbrainz.org/ws/2/release/"
MUSICBRAINZ_RELEASE_LOOKUP = "https://musicbrainz.org/ws/2/release/{mbid}"
COVER_ART_FRONT = "https://coverartarchive.org/release/{mbid}/front-500"

MB_HEADERS = {
    "User-Agent": "AlbumCoverBot/2.1 (telegram; contact@example.com)",
    "Accept": "application/xml",
}

_ITUNES_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"
    ),
    "Accept": "application/json,text/javascript,*/*;q=0.8",
}

Source = Literal["itunes", "musicbrainz", "spotify"]
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)


class MusicAPIError(Exception):
    pass


_itunes_next_ok_at = 0.0
_itunes_fail_streak = 0


@dataclass(frozen=True)
class AlbumCandidate:
    source: Source
    source_id: str
    artist_name: str
    collection_name: str
    artwork_url: str = ""
    track_count: int = 0
    release_year: str = ""
    release_date: str = ""  # YYYY-MM-DD для сортировки
    collection_view_url: str = ""

    @property
    def button_label(self) -> str:
        label = f"{self.collection_name} – {self.artist_name}"
        year = self.release_year or (self.release_date[:4] if self.release_date else "")
        if year:
            candidate = f"{label} ({year})"
            if len(candidate) <= 64:
                return candidate
        return label if len(label) <= 64 else label[:61] + "…"

    @property
    def dedupe_key(self) -> str:
        return (
            f"{self.artist_name.lower().strip()}|"
            f"{self.collection_name.lower().strip()}"
        )

    @property
    def sort_date_key(self) -> str:
        """Для сортировки newest → oldest."""
        if self.release_date and len(self.release_date) >= 4:
            return self.release_date
        if self.release_year:
            return f"{self.release_year}-01-01"
        return "0000-01-01"


@dataclass(frozen=True)
class TrackInfo:
    track_name: str
    track_number: int
    preview_url: Optional[str] = None
    track_view_url: Optional[str] = None
    artist_name: str = ""
    duration_ms: int = 0


@dataclass(frozen=True)
class TrackCandidate:
    """Сингл / трек из iTunes search entity=song."""

    source: Source
    track_id: str
    track_name: str
    artist_name: str
    collection_name: str = ""
    artwork_url: str = ""
    release_date: str = ""
    preview_url: str = ""
    track_view_url: str = ""
    collection_id: str = ""
    duration_ms: int = 0

    @property
    def button_label(self) -> str:
        year = self.release_date[:4] if self.release_date else ""
        label = f"{self.track_name} – {self.artist_name}"
        if year:
            cand = f"{label} ({year})"
            if len(cand) <= 64:
                return cand
        return label if len(label) <= 64 else label[:61] + "…"

    @property
    def sort_date_key(self) -> str:
        return self.release_date if self.release_date else "0000-01-01"


@dataclass(frozen=True)
class SearchSplitResult:
    albums: list[AlbumCandidate]
    singles: list[TrackCandidate]
    exact: bool = True
    note: str = ""


@dataclass(frozen=True)
class AlbumDetails:
    source: Source
    source_id: str
    artist_name: str
    collection_name: str
    artwork_url: str
    collection_view_url: str
    release_year: str
    tracks: list[TrackInfo] = field(default_factory=list)
    preview_url: str = ""


def _artwork_hires(url: str) -> str:
    if not url:
        return ""
    for size in ("100x100bb", "60x60bb", "30x30bb"):
        if size in url:
            return url.replace(size, "600x600bb")
    return url


def _year_from_date(date_str: str) -> str:
    return date_str[:4] if date_str and len(date_str) >= 4 else ""


def _tokenize(text: str) -> set[str]:
    return {
        t
        for t in "".join(ch.lower() if ch.isalnum() else " " for ch in text).split()
        if len(t) > 1
    }


def _relevance_score(album: AlbumCandidate, query: str) -> tuple:
    q = _tokenize(query)
    artist_t = _tokenize(album.artist_name)
    album_t = _tokenize(album.collection_name)
    combined = artist_t | album_t
    overlap = len(q & combined)
    artist_exact = int(album.artist_name.lower().strip() in query.lower())
    name_l = album.collection_name.lower()
    penalty = 0
    if "single" in name_l:
        penalty += 8
    if any(x in name_l for x in ("tribute", "karaoke", "lullaby")):
        penalty += 4
    if album.track_count == 1:
        penalty += 3
    album_bonus = 3 if album.track_count >= 7 else 0
    return (
        overlap + artist_exact * 3 + album_bonus - penalty,
        album.track_count,
    )


def _album_from_itunes(item: dict[str, Any]) -> Optional[AlbumCandidate]:
    collection_id = item.get("collectionId")
    if not collection_id:
        return None
    rd = (item.get("releaseDate") or "")[:10]
    return AlbumCandidate(
        source="itunes",
        source_id=str(int(collection_id)),
        artist_name=(item.get("artistName") or "Неизвестный исполнитель").strip(),
        collection_name=(item.get("collectionName") or "Без названия").strip(),
        artwork_url=_artwork_hires(
            item.get("artworkUrl100") or item.get("artworkUrl60") or ""
        ),
        track_count=int(item.get("trackCount") or 0),
        release_year=_year_from_date(item.get("releaseDate") or ""),
        release_date=rd,
        collection_view_url=item.get("collectionViewUrl") or "",
    )


async def _itunes_get(params: dict[str, str], url: str = ITUNES_SEARCH_URL) -> dict[str, Any]:
    """iTunes Search/Lookup с коротким backoff и circuit breaker на 403/429."""
    global _itunes_next_ok_at, _itunes_fail_streak
    session = await get_session()
    last_err: Exception | None = None
    loop = asyncio.get_running_loop()
    if _itunes_fail_streak >= 3 and loop.time() < _itunes_next_ok_at:
        raise MusicAPIError("iTunes временно недоступен.")
    for attempt in range(2):
        delay = max(0.0, _itunes_next_ok_at - loop.time())
        if delay > 0:
            await asyncio.sleep(min(delay, 1.2))
        try:
            async with session.get(
                url,
                params=params,
                headers=_ITUNES_HEADERS,
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                if resp.status in {403, 429}:
                    body = await resp.text()
                    logger.warning(
                        "iTunes HTTP %s (try %s): %s",
                        resp.status,
                        attempt + 1,
                        body[:120],
                    )
                    _itunes_fail_streak += 1
                    _itunes_next_ok_at = loop.time() + min(
                        10.0, 1.5 * max(1, _itunes_fail_streak)
                    )
                    last_err = MusicAPIError("iTunes временно недоступен.")
                    if attempt == 0:
                        await asyncio.sleep(0.5)
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    logger.error("iTunes HTTP %s: %s", resp.status, body[:160])
                    last_err = MusicAPIError("iTunes временно недоступен.")
                    await asyncio.sleep(0.3)
                    continue
                _itunes_fail_streak = 0
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_err = exc
            logger.warning("iTunes network (try %s): %s", attempt + 1, exc)
            await asyncio.sleep(0.35)
    if isinstance(last_err, MusicAPIError):
        raise last_err
    raise MusicAPIError("iTunes временно недоступен.") from last_err


def sort_albums_newest(albums: list[AlbumCandidate]) -> list[AlbumCandidate]:
    """Новые сверху; без даты (0000) — в конец."""
    return sorted(
        albums,
        key=lambda a: (a.sort_date_key, a.collection_name.lower()),
        reverse=True,
    )


def filter_exact_artist(
    albums: list[AlbumCandidate],
    query: str,
) -> list[AlbumCandidate]:
    """Артист совпадает с запросом (в т.ч. og buda ↔ ог буда)."""
    import re

    from parser import artist_names_match

    q = (query or "").strip()
    if not q:
        return albums
    q_low = q.lower()
    pattern = re.compile(rf"(?i)\b{re.escape(q)}\b")
    exact = [
        a
        for a in albums
        if artist_names_match(q, a.artist_name)
        or a.artist_name.lower().strip() == q_low
        or pattern.search(a.artist_name)
    ]
    return exact


def _whole_word_match(haystack: str, needle: str) -> bool:
    import re

    needle = (needle or "").strip()
    if not needle:
        return False
    if haystack.lower().strip() == needle.lower():
        return True
    return bool(re.search(rf"(?i)\b{re.escape(needle)}\b", haystack or ""))


def _norm_title(text: str) -> str:
    import re

    t = (text or "").lower().strip()
    t = re.sub(r"[「」『』\[\]()（）]", " ", t)
    t = re.sub(r"\s+", " ", t)
    for junk in (
        " - single",
        " - ep",
        " (deluxe)",
        " (remastered)",
        " deluxe",
        " remastered",
    ):
        t = t.replace(junk, "")
    return t.strip()


def _fuzzy_ratio(a: str, b: str) -> float:
    import difflib

    return difflib.SequenceMatcher(
        None, _norm_title(a), _norm_title(b)
    ).ratio()


def filter_discography(
    albums: list[AlbumCandidate],
    query: str,
) -> list[AlbumCandidate]:
    """Фильтр дискографии по подстроке / fuzzy названию альбома."""
    q = (query or "").strip()
    if not q or not albums:
        return list(albums)
    qn = _norm_title(q)
    # 1 символ («b» из «huzzy b») даёт ложные hit'ы в любом названии с этой буквой
    if len(qn) < 2:
        return []
    scored: list[tuple[float, AlbumCandidate]] = []
    for a in albums:
        name = _norm_title(a.collection_name)
        if qn == name:
            scored.append((1.0, a))
        elif len(qn) >= 3 and (qn in name or name in qn):
            scored.append((0.92, a))
        elif _whole_word_match(a.collection_name, q):
            scored.append((0.88, a))
        else:
            ratio = _fuzzy_ratio(a.collection_name, q)
            # короткие фильтры — только почти точное совпадение
            min_ratio = 0.88 if len(qn) <= 2 else 0.72
            if ratio >= min_ratio:
                scored.append((ratio, a))
    scored.sort(key=lambda x: (-x[0], x[1].sort_date_key), reverse=False)
    scored.sort(key=lambda x: -x[0])
    return [a for _, a in scored]


def split_albums_singles(
    items: list[AlbumCandidate],
) -> tuple[list[AlbumCandidate], list[AlbumCandidate]]:
    """Делит релизы артиста на альбомы и синглы/EP."""
    albums: list[AlbumCandidate] = []
    singles: list[AlbumCandidate] = []
    for a in items:
        name = (a.collection_name or "").lower()
        is_single = (
            " - single" in name
            or name.endswith(" single")
            or a.track_count == 1
        )
        is_ep = " - ep" in name or name.endswith(" ep") or name.endswith("- ep")
        if is_single:
            singles.append(a)
        elif is_ep and a.track_count and a.track_count <= 6:
            singles.append(a)
        else:
            albums.append(a)
    if not albums and singles:
        return singles, []
    return albums, singles


def is_feature_credit_release(album: AlbumCandidate, artist_name: str) -> bool:
    """True, если релиз — чужой трек «(feat. ЭтотАртист)», попавший в дискографию."""
    name = (album.collection_name or "").strip()
    art = (artist_name or "").strip()
    if not name or not art:
        return False
    low = name.lower()
    a = art.lower()
    # «Something (feat. Ken Carson)» / «featuring Ken Carson»
    for pat in (f"(feat. {a}", f"(feat {a}", f"featuring {a}", f"(with {a}"):
        if pat in low:
            return True
    return False


def filter_artist_own_releases(
    items: list[AlbumCandidate],
    artist_name: str,
) -> list[AlbumCandidate]:
    """Убирает чужие feat-синглы из дискографии артиста."""
    return [a for a in items if not is_feature_credit_release(a, artist_name)]


async def enrich_artists(
    artists: list[dict[str, Any]],
    *,
    country: str = "ru",
) -> list[dict[str, Any]]:
    """Добавляет flag + hint + catalog_score для кнопок выбора артиста."""
    out: list[dict[str, Any]] = []
    for a in artists:
        row = dict(a)
        hint = ""
        blob = row.get("artist_name") or ""
        album_n = 0
        track_sum = 0
        try:
            # RU+US — у рэперов свежие альбомы часто только в US
            pools: list[AlbumCandidate] = []
            seen: set[str] = set()
            for cc in (country, "us"):
                if not cc:
                    continue
                try:
                    albs = await albums_by_artist_id(
                        str(row["artist_id"]), country=cc, limit=40
                    )
                except Exception:  # noqa: BLE001
                    albs = []
                for alb in albs:
                    key = str(alb.source_id or "") or alb.collection_name.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    pools.append(alb)
            pools = sort_albums_newest(pools)
            own = filter_artist_own_releases(pools, row.get("artist_name") or "")
            if own:
                pools = own
            album_n = len(pools)
            track_sum = sum(int(x.track_count or 0) for x in pools)
            if pools:
                # hint = свежий полноценный альбом, не сингл
                full_albums, _sng = split_albums_singles(pools)
                hint_src = full_albums[0] if full_albums else pools[0]
                hint = hint_src.collection_name
                blob += " " + " ".join(x.collection_name for x in pools[:4])
        except Exception as exc:  # noqa: BLE001
            logger.debug("enrich artist %s: %s", row.get("artist_id"), exc)
        # сила каталога: много LP > пара фит-синглов
        catalog_score = album_n * 12 + min(track_sum, 200)
        genre = (row.get("genre") or "").lower()
        if any(g in genre for g in ("хип-хоп", "hip-hop", "rap", "trap")):
            catalog_score += 8
        if "vocal" in genre or "вокал" in genre:
            catalog_score -= 15
        row["flag"] = flag_emoji(blob)
        row["hint"] = hint
        row["album_count"] = album_n
        row["catalog_score"] = catalog_score
        out.append(row)
    out.sort(key=lambda r: (-int(r.get("catalog_score") or 0), r.get("artist_name") or ""))
    return out


def pick_dominant_artist(
    artists: list[dict[str, Any]],
    *,
    query: str = "",
) -> dict[str, Any] | None:
    """
    Если один артист с тем же именем явно доминирует по каталогу — берём его.
    Иначе None (показать пикер).
    """
    if not artists:
        return None
    pool = list(artists)
    q = (query or "").strip()
    if q:
        from parser import artist_match_score, artist_names_match

        matched = [
            a
            for a in pool
            if artist_names_match(q, a.get("artist_name") or "")
            or artist_match_score(q, a.get("artist_name") or "") >= 70
            or _artist_name_tokens_close(q, a.get("artist_name") or "")
        ]
        if matched:
            pool = matched
    if len(pool) == 1:
        return pool[0]
    ranked = sorted(
        pool,
        key=lambda r: (-int(r.get("catalog_score") or 0), -int(r.get("album_count") or 0)),
    )
    top = ranked[0]
    if len(ranked) == 1:
        return top
    second = ranked[1]
    top_s = int(top.get("catalog_score") or 0)
    sec_s = int(second.get("catalog_score") or 0)
    top_n = int(top.get("album_count") or 0)
    sec_n = int(second.get("album_count") or 0)
    top_name = (top.get("artist_name") or "").strip().casefold()
    sec_name = (second.get("artist_name") or "").strip().casefold()
    # дубликаты одного имени (разные id iTunes) — берём с большим каталогом
    if top_name and (
        top_name == sec_name
        or _artist_name_tokens_close(top_name, sec_name)
    ) and top_s >= sec_s and top_s >= 80:
        if top_s >= sec_s + 80 or top_n >= sec_n + 2:
            return top
    # явный фаворит: сильно больше альбомов / очков
    if top_n >= 5 and top_n >= sec_n * 3:
        return top
    if top_s >= 60 and top_s >= max(sec_s * 3, sec_s + 40):
        return top
    if top_n >= 8 and sec_n <= 2:
        return top
    # сильный лидер при том же написании имени (Nettspend 619 vs 216)
    if (
        top_name == sec_name or _artist_name_tokens_close(top_name, sec_name)
    ) and top_s >= 200 and top_s >= sec_s * 2:
        return top
    return None


def _artist_name_tokens_close(a: str, b: str) -> bool:
    """Ken Carson ≈ Ken Car$on."""
    def _key(s: str) -> str:
        t = (s or "").casefold().replace("$", "s")
        return re.sub(r"[^a-z0-9а-яё]+", "", t)

    ka, kb = _key(a), _key(b)
    return bool(ka and kb and ka == kb)


def artist_button_label(artist: dict[str, Any]) -> str:
    flag = artist.get("flag") or "🎤"
    name = artist.get("artist_name") or "Artist"
    hint = artist.get("hint") or artist.get("genre") or ""
    label = f"{flag} {name}"
    if hint:
        label = f"{label} · {hint}"
    return label if len(label) <= 64 else label[:61] + "…"


async def find_album_via_artist_discography(
    query: str,
    *,
    country: str = "ru",
    limit: int = 15,
) -> SearchSplitResult | None:
    """
    Combo/album: найти артиста → дискография → фильтр по названию альбома.
    Ловит кейсы вроде «Villian красивое зло».
    """
    from utils import split_artist_album_query

    q = (query or "").strip()
    if not q:
        return None
    pairs = split_artist_album_query(q)
    if not pairs:
        # одно «слово» — как альбом: пробуем как фильтр бессмысленно без артиста
        return None

    # алиасы опечаток артиста (Brejha → Brejcha)
    try:
        from parser import expand_query_aliases

        alias_queries = expand_query_aliases(q)
    except Exception:  # noqa: BLE001
        alias_queries = [q]

    search_artist_terms = [art for art, _ in pairs[:4]]
    for aq in alias_queries:
        for art, _alb in split_artist_album_query(aq)[:3]:
            if art and art not in search_artist_terms:
                search_artist_terms.append(art)
        # полный исправленный query тоже как пары
        for art, alb in split_artist_album_query(aq)[:3]:
            if (art, alb) not in pairs and len(_norm_title(alb)) >= 2:
                pairs.append((art, alb))

    for art, alb in pairs[:6]:
        # слишком короткий «альбом» (1 буква) — не combo, а часть имени артиста
        if len(_norm_title(alb)) < 2:
            continue
        artist_terms = [art] + [t for t in search_artist_terms if t != art][:2]
        ranked: list[tuple[float, dict[str, Any]]] = []
        for term in artist_terms:
            try:
                artists = await search_artists(term, country=country, limit=8)
            except MusicAPIError:
                artists = []
            if not artists and country.lower() != "us":
                try:
                    artists = await search_artists(term, country="us", limit=8)
                except MusicAPIError:
                    artists = []
            for a in artists:
                # Spotify artist_id не годится для iTunes lookup — резолвим по имени
                if str(a.get("source") or "") == "spotify" or not str(
                    a.get("artist_id") or ""
                ).isdigit():
                    try:
                        mapped = await search_artists(
                            a["artist_name"],
                            country=country,
                            limit=3,
                            allow_spotify=False,
                        )
                    except MusicAPIError:
                        mapped = []
                    mapped = [
                        m
                        for m in mapped
                        if str(m.get("artist_id") or "").isdigit()
                    ]
                    if mapped:
                        a = mapped[0]
                    else:
                        continue
                name = a["artist_name"]
                if name.lower().strip() == term.lower().strip():
                    ranked.append((1.0, a))
                else:
                    r = _fuzzy_ratio(name, term)
                    if r >= 0.75:
                        ranked.append((r, a))
        # уникальные artist_id
        uniq: dict[str, tuple[float, dict[str, Any]]] = {}
        for score, a in ranked:
            aid = str(a.get("artist_id") or "")
            if not aid:
                continue
            prev = uniq.get(aid)
            if prev is None or score > prev[0]:
                uniq[aid] = (score, a)
        ranked = sorted(uniq.values(), key=lambda x: -x[0])
        if not ranked:
            continue

        for score, a in ranked[:5]:
            discog = await albums_by_artist_id(
                a["artist_id"], country=country, limit=50
            )
            if not discog:
                discog = await albums_by_artist_id(
                    a["artist_id"], country="us", limit=50
                )
            matched = filter_discography(discog, alb)
            if matched:
                logger.info(
                    "discography hit artist=%r album=%r via %r (%.2f)",
                    a["artist_name"],
                    matched[0].collection_name,
                    art,
                    score,
                )
                return SearchSplitResult(
                    albums=matched[:limit],
                    singles=[],
                    exact=score >= 0.9
                    and _fuzzy_ratio(matched[0].collection_name, alb) >= 0.85,
                    note="" if score >= 0.9 else "Возможное совпадение:",
                )

            # трек/сингл: «Flying Bird» — не альбом, а song в каталоге
            songs = await search_itunes_songs(
                f"{a['artist_name']} {alb}",
                limit=min(limit, 15),
                country=country,
            )
            if not songs:
                songs = await _spotify_search_tracks_as_candidates(
                    f"{a['artist_name']} {alb}", limit=min(limit, 10)
                )
            hit_tracks = filter_tracks_by_artist_title(
                songs, artist=a["artist_name"], title=alb
            )
            if hit_tracks:
                # не принимать hit, если «артист» — короткое слово из имени
                # (Boris ⊂ Boris Brejcha), а title раздут лишними словами
                title_ratio = _fuzzy_ratio(hit_tracks[0].track_name, alb)
                art_ratio = _fuzzy_ratio(a["artist_name"], art)
                strong = title_ratio >= 0.85 and (
                    art_ratio >= 0.82
                    or a["artist_name"].lower().strip() == art.lower().strip()
                )
                if not strong and title_ratio < 0.92:
                    continue
                logger.info(
                    "discography track hit artist=%r track=%r",
                    a["artist_name"],
                    hit_tracks[0].track_name,
                )
                # дедуп одинаковых треков
                uniq_tracks: list[TrackCandidate] = []
                seen_names: set[str] = set()
                for t in hit_tracks:
                    k = f"{t.artist_name.casefold()}|{t.track_name.casefold()}"
                    if k in seen_names:
                        continue
                    seen_names.add(k)
                    uniq_tracks.append(t)
                return SearchSplitResult(
                    albums=[],
                    singles=uniq_tracks[:limit],
                    exact=title_ratio >= 0.85,
                    note="",
                )

    # iTunes лежит / артист не сматчился — прямой Spotify по полному query
    # (и по лучшим парам artist+title)
    try_queries = [q]
    for art, alb in pairs[:3]:
        try_queries.append(f"{art} {alb}")
        try_queries.append(f"artist:{art} track:{alb}")
    seen_qq: set[str] = set()
    for qq in try_queries:
        k = qq.casefold().strip()
        if not k or k in seen_qq:
            continue
        seen_qq.add(k)
        try:
            sp_songs = await _spotify_search_tracks_as_candidates(
                qq, limit=min(limit, 10)
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("spotify discog fallback: %s", exc)
            sp_songs = []
        if not sp_songs:
            continue
        best = sp_songs
        if pairs:
            art0, alb0 = pairs[0]
            filtered = filter_tracks_by_artist_title(
                sp_songs, artist=art0, title=alb0
            )
            if filtered:
                best = filtered
            else:
                # мягкий фильтр: title overlap
                soft = [
                    t
                    for t in sp_songs
                    if _fuzzy_ratio(t.track_name, alb0) >= 0.75
                    or alb0.lower() in t.track_name.lower()
                ]
                if soft:
                    best = soft
        logger.info(
            "spotify fallback track hit %r / %r via %r",
            best[0].artist_name,
            best[0].track_name,
            qq,
        )
        return SearchSplitResult(
            albums=[],
            singles=best[:limit],
            exact=_fuzzy_ratio(
                best[0].track_name, pairs[0][1] if pairs else q
            )
            >= 0.85,
            note="",
        )
    return None


def filter_strict_albums(
    albums: list[AlbumCandidate],
    query: str,
    *,
    mode: str = "both",
    artist_hint: str = "",
    album_hint: str = "",
) -> tuple[list[AlbumCandidate], bool]:
    """
    Строгая фильтрация.
    mode: artist | album | combo | both
    Возвращает (filtered, exact_found).
    """
    import difflib

    q = (query or "").strip()
    if not albums:
        return [], False

    scored: list[tuple[int, AlbumCandidate]] = []

    for a in albums:
        art = a.artist_name.strip()
        alb = a.collection_name.strip()
        score = 0

        if mode == "artist":
            if art.lower() == q.lower():
                score = 100
            elif _whole_word_match(art, q):
                score = 80
        elif mode == "album":
            if alb.lower() == q.lower():
                score = 100
            elif _whole_word_match(alb, q):
                score = 80
            # бонус если артист тоже в запросе
            if artist_hint and art.lower() == artist_hint.lower():
                score += 15
        elif mode == "combo" and artist_hint and album_hint:
            art_ok = art.lower() == artist_hint.lower() or _whole_word_match(
                art, artist_hint
            )
            alb_ok = _whole_word_match(alb, album_hint) or alb.lower() == album_hint.lower()
            if art_ok and alb_ok:
                score = 120
            elif art_ok:
                score = 60
            elif alb_ok:
                score = 40
        else:  # both
            if art.lower() == q.lower():
                score = 90
            if _whole_word_match(alb, q) or alb.lower() == q.lower():
                score = max(score, 85)
            if art.lower() in q.lower() and _whole_word_match(alb, q.split()[-1]):
                score = max(score, 95)
            # «Artist Track Title» → артист в запросе + fuzzy по названию альбома/сингла
            if artist_hint and album_hint:
                art_ok = (
                    art.lower() == artist_hint.lower()
                    or _whole_word_match(art, artist_hint)
                    or _fuzzy_ratio(art, artist_hint) >= 0.85
                )
                alb_ok = (
                    _whole_word_match(alb, album_hint)
                    or alb.lower() == album_hint.lower()
                    or _fuzzy_ratio(alb, album_hint) >= 0.78
                    or _norm_title(album_hint) in _norm_title(alb)
                )
                if art_ok and alb_ok:
                    score = max(score, 110)
                elif art_ok and _fuzzy_ratio(alb, album_hint) >= 0.6:
                    score = max(score, 75)

        if score > 0:
            scored.append((score, a))

    if scored:
        scored.sort(key=lambda x: (-x[0], x[1].sort_date_key), reverse=False)
        scored.sort(key=lambda x: -x[0])
        exact_list = [a for s, a in scored if s >= 80]
        if exact_list:
            return sort_albums_newest(exact_list), True
        # частичные совпадения — не считаем exact
        return sort_albums_newest([a for _, a in scored])[:3], False

    # fuzzy fallback: top 3 close matches by album+artist string
    labels = [f"{a.artist_name} {a.collection_name}" for a in albums]
    close = difflib.get_close_matches(q, labels, n=3, cutoff=0.45)
    if close:
        fuzzy = [a for a, lab in zip(albums, labels) if lab in close]
        # сохранить порядок close
        order = {lab: i for i, lab in enumerate(close)}
        fuzzy.sort(
            key=lambda a: order.get(f"{a.artist_name} {a.collection_name}", 99)
        )
        return fuzzy[:3], False

    # ещё один fuzzy по имени альбома
    album_names = [a.collection_name for a in albums]
    close_alb = difflib.get_close_matches(q, album_names, n=3, cutoff=0.5)
    if close_alb:
        fuzzy = [a for a in albums if a.collection_name in close_alb][:3]
        return fuzzy, False

    return albums[:3], False


async def search_itunes(
    query: str,
    *,
    entity: str = "album",
    country: str = "ru",
    limit: int = 25,
    strict: bool = True,
    mode: str = "both",
    artist_hint: str = "",
    album_hint: str = "",
) -> tuple[list[AlbumCandidate], bool]:
    """
    Поиск iTunes с опциональной строгой фильтрацией.
    Возвращает (albums, exact_match_found).
    """
    q = (query or "").strip()
    if not q:
        return [], False

    if entity == "musicArtist" or mode == "artist":
        # дискография точного артиста
        artists = await search_artists(q, country=country, limit=8)
        exact_artists = [
            a
            for a in artists
            if a["artist_name"].lower().strip() == q.lower()
        ]
        if not exact_artists and strict:
            import difflib

            names = [a["artist_name"] for a in artists]
            close = difflib.get_close_matches(q, names, n=3, cutoff=0.6)
            pick = [a for a in artists if a["artist_name"] in close][:1]
            if pick:
                albums = await albums_by_artist_id(
                    pick[0]["artist_id"], country=country, limit=limit
                )
                return albums[:limit], False
            return [], False
        if exact_artists:
            albums = await albums_by_artist_id(
                exact_artists[0]["artist_id"], country=country, limit=limit
            )
            return sort_albums_newest(albums)[:limit], True

    albums = await search_itunes_albums(q, limit=max(limit, 25), country=country)
    if not strict:
        return sort_albums_newest(albums)[:limit], True

    filtered, exact = filter_strict_albums(
        albums,
        q,
        mode=mode,
        artist_hint=artist_hint,
        album_hint=album_hint,
    )
    return filtered[:limit], exact


async def search_itunes_albums(
    query: str,
    *,
    limit: int = 8,
    country: str = "ru",
) -> list[AlbumCandidate]:
    query = (query or "").strip()
    if not query:
        return []
    cache_key = f"it:alb:v2:{country}:{query}:{limit}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    async def _once(cc: str) -> list[AlbumCandidate]:
        try:
            payload = await _itunes_get(
                {
                    "term": query,
                    "media": "music",
                    "entity": "album",
                    "limit": str(min(max(limit, 25), 50)),
                    "country": cc.lower(),
                }
            )
        except MusicAPIError:
            return []
        albums: list[AlbumCandidate] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            album = _album_from_itunes(item)
            if not album or album.source_id in seen:
                continue
            seen.add(album.source_id)
            albums.append(album)
        return albums

    albums = await _once(country)
    # fallback: глобальный US, если в регионе пусто
    if not albums and country.lower() != "us":
        albums = await _once("us")

    albums.sort(key=lambda a: _relevance_score(a, query), reverse=True)
    albums = sort_albums_newest(albums[: max(limit, 25)])[:limit]
    search_cache_set(cache_key, albums)
    return albums


async def search_itunes_artist_albums(
    query: str,
    *,
    limit: int = 8,
    country: str = "ru",
) -> list[AlbumCandidate]:
    query = (query or "").strip()
    if not query:
        return []
    cache_key = f"it:art:{country}:{query}:{limit}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    artist_payload = await _itunes_get(
        {
            "term": query,
            "media": "music",
            "entity": "musicArtist",
            "limit": "3",
            "country": country.lower(),
        }
    )
    artists = artist_payload.get("results") or []
    if not artists:
        search_cache_set(cache_key, [])
        return []

    q_tokens = _tokenize(query)
    artists.sort(
        key=lambda a: len(q_tokens & _tokenize(a.get("artistName") or "")),
        reverse=True,
    )
    artist_id = artists[0].get("artistId")
    if not artist_id:
        search_cache_set(cache_key, [])
        return []

    lookup = await _itunes_get(
        {
            "id": str(artist_id),
            "entity": "album",
            "limit": str(min(limit + 5, 25)),
            "country": country.lower(),
        },
        url=ITUNES_LOOKUP_URL,
    )
    albums: list[AlbumCandidate] = []
    seen: set[str] = set()
    for item in lookup.get("results") or []:
        if item.get("wrapperType") == "artist":
            continue
        album = _album_from_itunes(item)
        if not album or album.source_id in seen:
            continue
        if "single" in album.collection_name.lower() or album.track_count <= 2:
            continue
        seen.add(album.source_id)
        albums.append(album)
    albums = albums[:limit]
    search_cache_set(cache_key, albums)
    return albums


async def search_musicbrainz_releases(
    query: str,
    *,
    limit: int = 5,
) -> list[AlbumCandidate]:
    query = (query or "").strip()
    if not query:
        return []
    cache_key = f"mb:{query}:{limit}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    session = await get_session()
    lucene = f'artist:"{query}" OR release:"{query}" OR "{query}"'
    try:
        async with session.get(
            MUSICBRAINZ_RELEASE_URL,
            params={"query": lucene, "limit": str(limit)},
            headers=MB_HEADERS,
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                search_cache_set(cache_key, [])
                return []
            xml_text = await resp.text()
    except aiohttp.ClientError as exc:
        logger.warning("MusicBrainz network: %s", exc)
        search_cache_set(cache_key, [])
        return []

    albums = _parse_mb_xml(xml_text)
    albums.sort(key=lambda a: _relevance_score(a, query), reverse=True)
    albums = albums[:limit]
    search_cache_set(cache_key, albums)
    return albums


def _parse_mb_xml(xml_text: str) -> list[AlbumCandidate]:
    # xmltodict предпочтительно
    try:
        import xmltodict  # type: ignore

        data = xmltodict.parse(xml_text)
        metadata = data.get("metadata") or data
        release_list = metadata.get("release-list") or {}
        releases = release_list.get("release") or []
        if isinstance(releases, dict):
            releases = [releases]
        out: list[AlbumCandidate] = []
        for release in releases:
            if not isinstance(release, dict):
                continue
            mbid = release.get("@id") or ""
            title = release.get("title")
            if isinstance(title, dict):
                title = title.get("#text")
            title = (title or "").strip()
            if not mbid or not title:
                continue
            artist_names: list[str] = []
            credit = release.get("artist-credit") or {}
            ncs = credit.get("name-credit") or []
            if isinstance(ncs, dict):
                ncs = [ncs]
            for nc in ncs:
                if not isinstance(nc, dict):
                    continue
                artist = nc.get("artist") or {}
                name = artist.get("name") if isinstance(artist, dict) else None
                if isinstance(name, dict):
                    name = name.get("#text")
                if name:
                    artist_names.append(str(name))
            date = release.get("date")
            if isinstance(date, dict):
                date = date.get("#text")
            out.append(
                AlbumCandidate(
                    source="musicbrainz",
                    source_id=mbid,
                    artist_name=", ".join(artist_names) or "Неизвестный исполнитель",
                    collection_name=title,
                    artwork_url=COVER_ART_FRONT.format(mbid=mbid),
                    release_year=_year_from_date(str(date or "")),
                    collection_view_url=f"https://musicbrainz.org/release/{mbid}",
                )
            )
        if out:
            return out
    except Exception as exc:  # noqa: BLE001
        logger.debug("xmltodict failed: %s", exc)

    ns = {"mb": "http://musicbrainz.org/ns/mmd-2.0"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    albums: list[AlbumCandidate] = []
    for release in root.findall(".//mb:release", ns):
        mbid = release.attrib.get("id")
        title_el = release.find("mb:title", ns)
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not mbid or not title:
            continue
        names: list[str] = []
        for credit in release.findall("mb:artist-credit/mb:name-credit", ns):
            name_el = credit.find("mb:artist/mb:name", ns) or credit.find("mb:name", ns)
            if name_el is not None and name_el.text:
                names.append(name_el.text.strip())
        date_el = release.find("mb:date", ns)
        albums.append(
            AlbumCandidate(
                source="musicbrainz",
                source_id=mbid,
                artist_name=", ".join(names) or "Неизвестный исполнитель",
                collection_name=title,
                artwork_url=COVER_ART_FRONT.format(mbid=mbid),
                release_year=_year_from_date(
                    (date_el.text or "") if date_el is not None else ""
                ),
                collection_view_url=f"https://musicbrainz.org/release/{mbid}",
            )
        )
    return albums


def merge_albums(*groups: list[AlbumCandidate], limit: int = 8) -> list[AlbumCandidate]:
    merged: list[AlbumCandidate] = []
    seen_keys: set[str] = set()
    seen_ids: set[tuple[str, str]] = set()
    for group in groups:
        for album in group:
            id_key = (album.source, album.source_id)
            if id_key in seen_ids or album.dedupe_key in seen_keys:
                continue
            seen_ids.add(id_key)
            seen_keys.add(album.dedupe_key)
            merged.append(album)
    return merged[:limit]


async def _search_one_query(
    query: str,
    *,
    country: str,
    limit: int,
) -> list[AlbumCandidate]:
    """Параллельно: album search + artist albums (+ MB если пусто)."""
    album_task = asyncio.create_task(
        search_itunes_albums(query, limit=limit, country=country)
    )
    artist_task = asyncio.create_task(
        search_itunes_artist_albums(query, limit=limit, country=country)
    )
    album_hits, artist_hits = await asyncio.gather(
        album_task, artist_task, return_exceptions=True
    )
    if isinstance(album_hits, Exception):
        logger.warning("album search err: %s", album_hits)
        album_hits = []
    if isinstance(artist_hits, Exception):
        logger.warning("artist search err: %s", artist_hits)
        artist_hits = []

    batch = merge_albums(artist_hits, album_hits, limit=limit)  # type: ignore[arg-type]
    if batch:
        batch.sort(key=lambda a: _relevance_score(a, query), reverse=True)
        return batch

    mb = await search_musicbrainz_releases(query, limit=limit)
    return mb


def _multi_query_score(album: AlbumCandidate, queries: list[str]) -> tuple:
    """
    Скоринг по ВСЕМ кандидатам. Сильный буст, если название альбома
    совпадает с одним из запросов (IGOR + Tyler → IGOR сверху).
    """
    best = max(_relevance_score(album, q) for q in queries)
    name_l = album.collection_name.lower().strip()
    artist_l = album.artist_name.lower().strip()
    boost = 0
    for q in queries:
        ql = q.lower().strip()
        if not ql:
            continue
        if name_l == ql or name_l in ql.split() or ql == name_l:
            boost += 25
        elif ql in name_l or name_l in ql:
            boost += 15
        # «IGOR Tyler, The Creator» / «Tyler IGOR»
        if name_l in ql and any(
            tok in artist_l for tok in ql.replace(",", " ").split() if len(tok) > 2
        ):
            boost += 10
        if ql in artist_l:
            boost += 3
    return (best[0] + boost, best[1], name_l == "igor")


async def search_albums_multi(
    queries: list[str],
    *,
    limit: int = 8,
    country: str = "ru",
    max_queries: int = 3,
) -> list[AlbumCandidate]:
    """
    До max_queries кандидатов параллельно, результаты СЛИВАЕМ и ранжируем.
    Так OCR «Tyler» + «IGOR» находит IGOR, а не случайный Wolf.
    """
    cleaned = [q.strip() for q in queries if q and q.strip()][:max_queries]
    if not cleaned:
        return []

    cache_key = f"multi:v2:{country}:{limit}:{('|'.join(cleaned)).lower()}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    tasks = [
        asyncio.create_task(_search_one_query(q, country=country, limit=max(limit, 10)))
        for q in cleaned
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    groups: list[list[AlbumCandidate]] = []
    for q, res in zip(cleaned, results):
        if isinstance(res, Exception):
            logger.warning("query %r failed: %s", q, res)
            continue
        if res:
            logger.info("Hit for %r → %d albums", q, len(res))
            groups.append(res)  # type: ignore[arg-type]

    merged = merge_albums(*groups, limit=max(limit * 3, 20))
    merged.sort(key=lambda a: _multi_query_score(a, cleaned), reverse=True)
    best = merged[:limit]
    logger.info(
        "Merged top: %s",
        [f"{a.collection_name} – {a.artist_name}" for a in best[:5]],
    )
    search_cache_set(cache_key, best)
    return best


async def search_by_lyrics(query: str, *, limit: int = 20):
    """Поиск песен по фрагменту текста (Genius) — обёртка для bot/music API."""
    from lyrics import search_by_lyrics as _search

    return await _search(query, limit=limit)


def _cover_clue_variants(clue: str) -> list[str]:
    """Варианты строки для поиска артиста: целиком, Artist–Album, префиксы."""
    import re

    c = re.sub(r"\s+", " ", (clue or "").strip())
    if len(c) < 2:
        return []
    out: list[str] = [c]
    low = c.lower()

    for sep in (" - ", " – ", " — ", " by ", " | "):
        if sep in low:
            idx = low.index(sep)
            left, right = c[:idx].strip(), c[idx + len(sep) :].strip()
            if left:
                out.append(left)
            if right:
                out.append(right)

    words = c.split()
    if len(words) >= 2:
        out.append(" ".join(words[:2]))
        out.append(" ".join(words[:3]) if len(words) >= 3 else "")
        out.append(" ".join(words[:-1]))  # без последнего слова (часто альбом)
        if len(words) >= 4:
            out.append(" ".join(words[-3:]))
            out.append(" ".join(words[:1]))

    # убрать пустые / дубли
    seen: set[str] = set()
    clean: list[str] = []
    for v in out:
        v = (v or "").strip()
        key = v.lower()
        if len(v) < 2 or key in seen:
            continue
        seen.add(key)
        clean.append(v)
    return clean[:8]


def _artist_name_in_clue(artist_name: str, clue: str) -> float:
    """Насколько имя артиста объясняется подсказкой с картинки (0…100)."""
    an = (artist_name or "").lower().strip()
    cl = (clue or "").lower().strip()
    if not an or not cl:
        return 0.0
    if an == cl:
        return 100.0
    if len(an) >= 3 and an in cl:
        return 85.0
    a_tok = _tokenize(an)
    c_tok = _tokenize(cl)
    if not a_tok:
        return 0.0
    overlap = len(a_tok & c_tok) / len(a_tok)
    if overlap >= 1.0:
        return 75.0
    if overlap >= 0.67:
        return 45.0
    return 0.0


def _album_title_in_clues(album_name: str, clues: list[str]) -> bool:
    name = (album_name or "").lower().strip()
    if len(name) < 2:
        return False
    # убрать типичные суффиксы изданий
    import re

    bare = re.sub(
        r"(?i)\s*[\(\[]?(remaster(ed)?|deluxe|super\s*deluxe|"
        r"expanded|anniversary|edition|mix|remix)[^\)]*[\)\]]?",
        "",
        name,
    ).strip(" -–—")
    for clue in clues:
        cl = clue.lower()
        if bare and len(bare) >= 3 and bare in cl:
            return True
        if name in cl:
            return True
        n_tok = _tokenize(bare or name)
        c_tok = _tokenize(cl)
        if n_tok and len(n_tok & c_tok) == len(n_tok) and len(n_tok) >= 1:
            return True
    return False


async def _resolve_artists_from_cover_clues(
    clues: list[str],
    *,
    country: str,
    max_artists: int = 3,
) -> list[dict[str, Any]]:
    """
    По подсказкам с картинки находим артистов в iTunes
    (не «гуглим текст», а резолвим musicArtist).
    """
    scores: dict[str, float] = {}
    info_by_id: dict[str, dict[str, Any]] = {}

    async def _add(artist: dict[str, Any], score: float) -> None:
        aid = str(artist.get("artist_id") or "")
        if not aid or score <= 0:
            return
        prev = scores.get(aid, 0.0)
        if score > prev:
            scores[aid] = score
            info_by_id[aid] = artist
        else:
            scores[aid] = prev + score * 0.15  # повторные попадания

    # 1) Прямой поиск артистов по вариантам строки
    seen_variants: set[str] = set()
    for clue in clues[:6]:
        for variant in _cover_clue_variants(clue):
            key = variant.lower()
            if key in seen_variants:
                continue
            seen_variants.add(key)
            try:
                found = await search_artists(variant, country=country, limit=5)
            except Exception as exc:  # noqa: BLE001
                logger.debug("search_artists %r: %s", variant, exc)
                continue
            for art in found:
                score = _artist_name_in_clue(art["artist_name"], clue)
                # короткий запрос = сам артист
                if art["artist_name"].lower().strip() == variant.lower():
                    score = max(score, 90.0)
                if score > 0:
                    await _add(art, score)

    # 2) Поиск альбомов по подсказке → артист релиза, если имя стыкуется
    for clue in clues[:4]:
        try:
            hits = await search_itunes_albums(clue, limit=8, country=country)
        except Exception as exc:  # noqa: BLE001
            logger.debug("cover album clue %r: %s", clue, exc)
            continue
        for alb in hits:
            art_score = _artist_name_in_clue(alb.artist_name, clue)
            alb_hit = _album_title_in_clues(alb.collection_name, [clue])
            if art_score < 40 and not alb_hit:
                continue
            try:
                arts = await search_artists(
                    alb.artist_name, country=country, limit=3
                )
            except Exception:  # noqa: BLE001
                continue
            for art in arts:
                if art["artist_name"].lower() == alb.artist_name.lower():
                    bonus = 30.0 if alb_hit else 0.0
                    await _add(art, max(art_score, 50.0) + bonus)
                    break

    ranked = sorted(scores.items(), key=lambda x: -x[1])
    # отсечь слабых и «артистов», которые на деле названия альбомов/треков
    clue_blob = " ".join(clues).lower()
    strong: list[tuple[str, float]] = []
    for aid, sc in ranked:
        if sc < 70:
            continue
        name = (info_by_id[aid].get("artist_name") or "").strip()
        # имя целиком есть в подсказке как отдельная сущность — ок;
        # но если это 1–2 слова и в подсказке рядом есть другой явный артист —
        # проверим ниже относительно лидера
        strong.append((aid, sc))

    if not strong:
        strong = [(aid, sc) for aid, sc in ranked if sc >= 45][:max_artists]

    if strong:
        best = strong[0][1]
        # оставляем только близких к лидеру (The Beatles 150 vs Abbey Road 141 —
        # если лидер заметно выше и имя лидера длиннее/полнее — режем «альбомных» артистов)
        filtered: list[tuple[str, float]] = []
        leader_name = (info_by_id[strong[0][0]].get("artist_name") or "").lower()
        for aid, sc in strong:
            name = (info_by_id[aid].get("artist_name") or "").strip()
            name_l = name.lower()
            # если артист = короткое имя, которое выглядит как название релиза лидера
            if (
                aid != strong[0][0]
                and best >= 100
                and sc < best * 0.92
                and len(name.split()) <= 3
                and name_l in clue_blob
                and leader_name
                and leader_name in clue_blob
                and name_l != leader_name
            ):
                # «Abbey Road» рядом с «The Beatles» — не артист
                logger.info("cover skip album-like artist: %s (score=%s)", name, round(sc, 1))
                continue
            if best >= 120 and sc < best * 0.55:
                continue
            filtered.append((aid, sc))
        strong = filtered or strong[:1]

    out = [info_by_id[aid] for aid, _ in strong[:max_artists]]
    logger.info(
        "cover artists: %s",
        [(a["artist_name"], round(scores[a["artist_id"]], 1)) for a in out],
    )
    return out


async def _artist_discography(
    artist_id: str,
    *,
    country: str,
    limit: int = 50,
) -> list[AlbumCandidate]:
    """Релизы артиста (альбомы + синглы-коллекции) в регионе и US."""
    albums = await albums_by_artist_id(
        artist_id, country=country, limit=limit
    )
    if country.lower() != "us":
        try:
            us = await albums_by_artist_id(artist_id, country="us", limit=limit)
            albums = merge_albums(albums, us, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.debug("US discography: %s", exc)
    return albums


async def find_album_by_cover(
    image_bytes: bytes,
    extracted_text: str,
    *,
    country: str = "ru",
    limit: int = 35,
    extra_queries: Optional[list[str]] = None,
    run_reverse: bool = True,
) -> tuple[list[AlbumCandidate], bool, list[str]]:
    """
    Обложка → артист(ы) → дискография релизов → pHash/dHash.

    Не ищем «что написано на картинке» как свободный текст в каталоге,
    а резолвим артиста и сверяем обложку с его выпущенными альбомами/синглами.
    """
    from cover_match import rank_albums_by_phash
    from parser import extract_search_query

    ocr_queries = extract_search_query(extracted_text or "")[:4]
    if not ocr_queries and (extracted_text or "").strip():
        ocr_queries = [(extracted_text or "").strip()[:80]]

    rev_queries: list[str] = list(extra_queries or [])
    if run_reverse and image_bytes:
        try:
            from reverse_image import reverse_cover_queries

            got = await reverse_cover_queries(image_bytes, limit=6)
            rev_queries.extend(got)
        except Exception as exc:  # noqa: BLE001
            logger.warning("reverse_image in find_album_by_cover: %s", exc)

    clues: list[str] = []
    seen_q: set[str] = set()
    for q in [*rev_queries, *ocr_queries]:
        key = (q or "").strip().lower()
        if len(key) < 2 or key in seen_q:
            continue
        seen_q.add(key)
        clues.append(q.strip())
    clues = clues[:8]

    if not clues:
        return [], False, []

    logger.info("cover search clues: %s", clues)

    artists = await _resolve_artists_from_cover_clues(
        clues, country=country, max_artists=3
    )

    discog: list[AlbumCandidate] = []
    artist_labels: list[str] = []
    if artists:
        batches = await asyncio.gather(
            *[
                _artist_discography(
                    a["artist_id"], country=country, limit=50
                )
                for a in artists
            ],
            return_exceptions=True,
        )
        for art, batch in zip(artists, batches):
            if isinstance(batch, Exception):
                logger.warning(
                    "discography %s: %s", art.get("artist_name"), batch
                )
                continue
            artist_labels.append(art["artist_name"])
            discog.extend(batch)  # type: ignore[arg-type]

    discog = merge_albums(discog, limit=80)

    # Поднять в начало сверки релизы, чьё название уже видно в подсказках
    hinted: list[AlbumCandidate] = []
    if discog and clues:
        hinted = [
            a for a in discog if _album_title_in_clues(a.collection_name, clues)
        ]
        rest = [
            a for a in discog if a.source_id not in {x.source_id for x in hinted}
        ]
        discog = hinted + rest

    queries_used = artist_labels + clues[:3]

    if discog:
        logger.info(
            "cover discography: %d releases from %s (title hints=%d)",
            len(discog),
            artist_labels,
            len(hinted),
        )
        matches, others = await rank_albums_by_phash(
            image_bytes,
            discog,
            max_check=min(40, len(discog)),
        )
        if matches:
            match_ids = {m.source_id for m in matches}
            ordered = matches + [
                a for a in others if a.source_id not in match_ids
            ]
            return ordered[:limit], True, queries_used

        # Нет pHash — отдать дискографию артиста (не чужой текстовый поиск)
        return discog[:limit], False, queries_used

    # Fallback: артиста не нашли — старый путь по тексту (хуже, но лучше чем пусто)
    logger.info("cover: artists not resolved, fallback text search")
    albums = await search_albums_multi(
        clues, limit=limit, country=country, max_queries=min(5, len(clues))
    )
    if not albums:
        return [], False, clues

    matches, others = await rank_albums_by_phash(image_bytes, albums)
    if matches:
        match_ids = {m.source_id for m in matches}
        ordered = matches + [a for a in others if a.source_id not in match_ids]
        return ordered, True, clues
    return sort_albums_newest(albums), False, clues


async def search_itunes_songs(
    query: str,
    *,
    limit: int = 25,
    country: str = "ru",
) -> list[TrackCandidate]:
    """Поиск синглов/треков: entity=song."""
    query = (query or "").strip()
    if not query:
        return []
    cache_key = f"it:song:v1:{country}:{query}:{limit}"
    hit = search_cache_get(cache_key)
    if hit is not None:
        return hit

    async def _once(cc: str) -> list[TrackCandidate]:
        try:
            payload = await _itunes_get(
                {
                    "term": query,
                    "media": "music",
                    "entity": "song",
                    "limit": str(min(limit, 50)),
                    "country": cc.lower(),
                }
            )
        except MusicAPIError:
            return []
        out: list[TrackCandidate] = []
        seen: set[str] = set()
        for item in payload.get("results") or []:
            if item.get("kind") != "song" and item.get("wrapperType") != "track":
                continue
            tid = item.get("trackId")
            name = (item.get("trackName") or "").strip()
            if not tid or not name:
                continue
            sid = str(int(tid))
            if sid in seen:
                continue
            seen.add(sid)
            out.append(
                TrackCandidate(
                    source="itunes",
                    track_id=sid,
                    track_name=name,
                    artist_name=(item.get("artistName") or "").strip(),
                    collection_name=(item.get("collectionName") or "").strip(),
                    artwork_url=_artwork_hires(
                        item.get("artworkUrl100") or item.get("artworkUrl60") or ""
                    ),
                    release_date=(item.get("releaseDate") or "")[:10],
                    preview_url=item.get("previewUrl") or "",
                    track_view_url=item.get("trackViewUrl") or "",
                    collection_id=str(int(item["collectionId"]))
                    if item.get("collectionId")
                    else "",
                    duration_ms=int(item.get("trackTimeMillis") or 0),
                )
            )
        return out

    songs = await _once(country)
    if not songs and country.lower() != "us":
        songs = await _once("us")
    songs = sorted(songs, key=lambda t: t.sort_date_key, reverse=True)[:limit]
    search_cache_set(cache_key, songs)
    return songs


def filter_exact_artist_tracks(
    tracks: list[TrackCandidate],
    query: str,
    *,
    artist_hint: str = "",
) -> list[TrackCandidate]:
    """Оставляет треки артиста. needle — artist_hint или весь query (для mode=artist)."""
    import re

    needle = (artist_hint or query or "").strip()
    if not needle:
        return tracks
    pattern = re.compile(rf"(?i)\b{re.escape(needle)}\b")
    n_low = needle.lower()
    return [
        t
        for t in tracks
        if t.artist_name.lower().strip() == n_low
        or pattern.search(t.artist_name)
        or _fuzzy_ratio(t.artist_name, needle) >= 0.85
    ]


def filter_tracks_by_artist_title(
    tracks: list[TrackCandidate],
    *,
    artist: str = "",
    title: str = "",
    query: str = "",
) -> list[TrackCandidate]:
    """
    Треки по артисту + названию (Flying Bird у Boris Brejcha и т.п.).
    Если title пуст — пробует разбиения полного query.
    """
    from utils import split_artist_album_query

    art = (artist or "").strip()
    tit = (title or "").strip()
    pairs: list[tuple[str, str]] = []
    if art and tit:
        pairs.append((art, tit))
    elif (query or "").strip():
        pairs.extend(split_artist_album_query(query.strip())[:4])
    if not pairs:
        return []

    scored: list[tuple[float, TrackCandidate]] = []
    seen: set[str] = set()
    for a_hint, t_hint in pairs:
        for t in tracks:
            key = t.track_id or f"{t.artist_name}|{t.track_name}|{t.collection_name}"
            if key in seen:
                continue
            art_ok = (
                t.artist_name.lower().strip() == a_hint.lower()
                or _whole_word_match(t.artist_name, a_hint)
                or _fuzzy_ratio(t.artist_name, a_hint) >= 0.82
            )
            if not art_ok:
                continue
            tn = _norm_title(t.track_name)
            th = _norm_title(t_hint)
            if not th:
                continue
            if tn == th:
                ratio = 1.0
            elif th in tn or tn in th:
                ratio = 0.92
            elif _whole_word_match(t.track_name, t_hint):
                ratio = 0.9
            else:
                ratio = _fuzzy_ratio(t.track_name, t_hint)
            if ratio >= 0.72:
                seen.add(key)
                scored.append((ratio, t))
    scored.sort(key=lambda x: -x[0])
    return [t for _, t in scored]


async def search_text(
    query: str,
    *,
    limit: int = 25,
    country: str = "ru",
    prefer_exact_artist: bool = True,
) -> list[AlbumCandidate]:
    from parser import extract_search_query

    cleaned = extract_search_query(query)
    queries = [query.strip()]
    for c in cleaned:
        if c.lower() != query.strip().lower() and c not in queries:
            queries.append(c)
    albums = await search_albums_multi(
        queries, limit=max(limit, 15), country=country, max_queries=3
    )
    if prefer_exact_artist and albums:
        exact = filter_exact_artist(albums, query.strip())
        if exact:
            albums = exact
    return sort_albums_newest(albums)[:limit]


async def search_text_split(
    query: str,
    *,
    limit: int = 25,
    country: str = "ru",
    mode: str = "both",
    artist_hint: str = "",
    album_hint: str = "",
) -> SearchSplitResult:
    """
    Albums + songs со строгой фильтрацией.
    mode: artist | album | combo | both
    """
    from utils import split_artist_album_query

    q = (query or "").strip()
    exact = True
    note = ""

    # combo / «артист + альбом/трек»: дискография + поиск треков
    if mode == "combo" or (not artist_hint and len(q.split()) >= 3):
        via_discog = await find_album_via_artist_discography(
            q, country=country, limit=limit
        )
        if via_discog and (via_discog.albums or via_discog.singles):
            return via_discog

        pairs = split_artist_album_query(q)
        best_albums: list[AlbumCandidate] = []
        best_songs: list[TrackCandidate] = []
        best_exact = False
        for art, alb in pairs[:4]:
            raw, _ = await search_itunes(
                f"{art} {alb}",
                entity="album",
                country=country,
                limit=limit,
                strict=True,
                mode="combo",
                artist_hint=art,
                album_hint=alb,
            )
            songs_raw = await search_itunes_songs(
                f"{art} {alb}", limit=min(limit, 15), country=country
            )
            track_hits = filter_tracks_by_artist_title(
                songs_raw, artist=art, title=alb
            )
            if track_hits and not best_songs:
                best_songs = track_hits
                artist_hint, album_hint = art, alb
                best_exact = _fuzzy_ratio(track_hits[0].track_name, alb) >= 0.9
            if raw:
                both = [
                    a
                    for a in raw
                    if (
                        a.artist_name.lower().strip() == art.lower()
                        or _fuzzy_ratio(a.artist_name, art) >= 0.85
                    )
                    and (
                        _whole_word_match(a.collection_name, alb)
                        or _fuzzy_ratio(a.collection_name, alb) >= 0.85
                        or _norm_title(alb) in _norm_title(a.collection_name)
                    )
                ]
                if both:
                    best_albums = both
                    best_exact = True
                    artist_hint, album_hint = art, alb
                    break
                if not best_albums:
                    best_albums = raw
                    artist_hint, album_hint = art, alb

        if best_albums or best_songs:
            albums = sort_albums_newest(best_albums)[:limit] if best_albums else []
            exact = best_exact
            if not exact:
                note = "Точного совпадения нет. Возможно, вы искали:"
            if best_songs:
                songs = best_songs
            else:
                songs = await search_itunes_songs(
                    f"{artist_hint} {album_hint}".strip() or q,
                    limit=min(limit, 15),
                    country=country,
                )
                songs = filter_tracks_by_artist_title(
                    songs, artist=artist_hint, title=album_hint, query=q
                ) or filter_exact_artist_tracks(
                    songs, q, artist_hint=artist_hint
                ) or songs[:3]
            return SearchSplitResult(
                albums=albums,
                singles=sorted(songs, key=lambda t: t.sort_date_key, reverse=True)[
                    :limit
                ],
                exact=exact,
                note=note,
            )

    if mode == "artist":
        albums, exact = await search_itunes(
            q,
            entity="musicArtist",
            country=country,
            limit=limit,
            strict=True,
            mode="artist",
        )
        songs = await search_itunes_songs(q, limit=limit, country=country)
        songs = filter_exact_artist_tracks(songs, q) or songs[:3]
        if not exact:
            note = "Точного совпадения нет. Возможно, вы искали:"
            albums = albums[:3]
            songs = songs[:3]
        return SearchSplitResult(
            albums=sort_albums_newest(albums)[:limit],
            singles=sorted(songs, key=lambda t: t.sort_date_key, reverse=True)[:limit],
            exact=exact,
            note=note,
        )

    # album / both — подсказки из разбиения query
    pairs = split_artist_album_query(q)
    if not artist_hint and pairs:
        artist_hint, album_hint = pairs[0][0], pairs[0][1]

    search_mode = "album" if mode == "album" else "both"
    albums, exact = await search_itunes(
        q,
        entity="album",
        country=country,
        limit=max(limit, 25),
        strict=True,
        mode=search_mode,
        artist_hint=artist_hint,
        album_hint=album_hint or q,
    )
    songs = await search_itunes_songs(q, limit=limit, country=country)
    if mode == "album":
        songs = [
            t
            for t in songs
            if _whole_word_match(t.track_name, q) or t.track_name.lower() == q.lower()
        ] or songs[:3]
    else:
        by_title = filter_tracks_by_artist_title(
            songs, artist=artist_hint, title=album_hint, query=q
        )
        if by_title:
            songs = by_title
            exact = exact or _fuzzy_ratio(by_title[0].track_name, album_hint or q) >= 0.85
        else:
            exact_songs = filter_exact_artist_tracks(
                songs, q, artist_hint=artist_hint
            )
            songs = exact_songs if exact_songs else songs[:3]

    # не выкидывать синглы, если по названию это и есть запрос
    if album_hint or mode == "both":
        title_needle = _norm_title(album_hint or q)
        keep_singles = [
            a
            for a in albums
            if title_needle
            and (
                title_needle in _norm_title(a.collection_name)
                or _fuzzy_ratio(a.collection_name, album_hint or q) >= 0.78
            )
        ]
        full_albums = [
            a
            for a in albums
            if "single" not in a.collection_name.lower()
            and (a.track_count == 0 or a.track_count > 1)
        ]
        albums = full_albums or keep_singles or albums

    if not exact and not songs:
        note = "Точного совпадения нет. Возможно, вы искали:"
        albums = albums[:3]

    return SearchSplitResult(
        albums=sort_albums_newest(albums)[:limit],
        singles=sorted(songs, key=lambda t: t.sort_date_key, reverse=True)[:limit],
        exact=exact,
        note=note,
    )


async def albums_by_artist_id(
    artist_id: str,
    *,
    country: str = "ru",
    limit: int = 50,
) -> list[AlbumCandidate]:
    """Дискография артиста по iTunes artistId."""
    if not str(artist_id or "").isdigit():
        return []
    try:
        payload = await _itunes_get(
            {
                "id": str(artist_id),
                "entity": "album",
                "limit": str(min(limit, 50)),
                "country": country.lower(),
            },
            url=ITUNES_LOOKUP_URL,
        )
    except MusicAPIError as exc:
        logger.warning("albums_by_artist_id %s: %s", artist_id, exc)
        return []
    albums: list[AlbumCandidate] = []
    seen: set[str] = set()
    for item in payload.get("results") or []:
        if item.get("wrapperType") == "artist":
            continue
        album = _album_from_itunes(item)
        if album and album.source_id not in seen:
            seen.add(album.source_id)
            albums.append(album)
    return sort_albums_newest(albums)[:limit]


async def search_artists(
    query: str,
    *,
    country: str = "ru",
    limit: int = 5,
    allow_spotify: bool = True,
) -> list[dict[str, Any]]:
    """Поиск артистов: iTunes (+ транслит) с soft-fail, затем Spotify."""
    from parser import artist_match_score, search_term_variants

    terms = search_term_variants(query) or [(query or "").strip()]
    terms = [t for t in terms if t][:6]

    scored: list[tuple[int, dict[str, Any]]] = []
    seen_ids: set[str] = set()
    itunes_ok = False
    itunes_dead = False

    for term in terms:
        if itunes_dead:
            break
        for cc in (country.lower(), "us"):
            if itunes_dead:
                break
            try:
                payload = await _itunes_get(
                    {
                        "term": term,
                        "media": "music",
                        "entity": "musicArtist",
                        "limit": str(max(limit, 10)),
                        "country": cc,
                    }
                )
                itunes_ok = True
            except MusicAPIError as exc:
                logger.warning("search_artists iTunes %r/%s: %s", term, cc, exc)
                # circuit open / серия 403 — сразу Spotify
                if _itunes_fail_streak >= 3:
                    itunes_dead = True
                continue
            except Exception as exc:  # noqa: BLE001
                logger.warning("search_artists iTunes err: %s", exc)
                continue
            for item in payload.get("results") or []:
                name = (item.get("artistName") or "").strip()
                aid = item.get("artistId")
                if not name or not aid:
                    continue
                sid = str(aid)
                if sid in seen_ids:
                    continue
                if len(name) > 60 and name.count(",") >= 2:
                    continue
                seen_ids.add(sid)
                row = {
                    "artist_id": sid,
                    "artist_name": name,
                    "genre": item.get("primaryGenreName") or "",
                    "source": "itunes",
                }
                score = max(
                    artist_match_score(query, name),
                    artist_match_score(term, name),
                )
                if len(name.split()) <= 3:
                    score += 2
                # точное совпадение имени — в топ
                if name.casefold() == (query or "").strip().casefold():
                    score += 40
                if name.casefold() == term.casefold():
                    score += 25
                scored.append((score, row))
        if any(s >= 110 for s, _ in scored):
            break

    # Spotify fallback / дополнение, если iTunes пуст или лежит
    if allow_spotify and (not scored or not itunes_ok):
        try:
            spotify_hits = await _spotify_search_artists(query, limit=limit)
            for row in spotify_hits:
                sid = f"sp:{row['artist_id']}"
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                score = artist_match_score(query, row["artist_name"])
                if row["artist_name"].casefold() == (query or "").strip().casefold():
                    score += 40
                scored.append((score, row))
        except Exception as exc:  # noqa: BLE001
            logger.warning("spotify artists fallback: %s", exc)

    if not scored and not itunes_ok:
        raise MusicAPIError("iTunes временно недоступен.")

    scored.sort(key=lambda x: -x[0])
    # Не разбавляем точными совпадениями чужими «похожими» (Lil Tecca / Sofia Carson
    # для запроса Ken Carson). Слабые результаты — только если сильных нет.
    strong = [row for s, row in scored if s >= 80]
    if strong:
        return strong[:limit]
    medium = [row for s, row in scored if s >= 40]
    if medium:
        return medium[:limit]
    return [row for _, row in scored][:limit]


async def _spotify_search_tracks_as_candidates(
    query: str, *, limit: int = 8
) -> list[TrackCandidate]:
    """Трек-кандидаты из Spotify Search (fallback когда iTunes 403)."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        from links import _spotify_access_token
    except Exception:  # noqa: BLE001
        return []
    session = await get_session()
    token = await _spotify_access_token(session)
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    out: list[TrackCandidate] = []
    try:
        async with session.get(
            "https://api.spotify.com/v1/search",
            params={
                "q": q,
                "type": "track",
                "limit": str(min(limit, 15)),
                "market": "US",
            },
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("spotify track search: %s", exc)
        return []
    for item in ((payload.get("tracks") or {}).get("items")) or []:
        name = (item.get("name") or "").strip()
        tid = (item.get("id") or "").strip()
        if not name or not tid:
            continue
        artists = item.get("artists") or []
        art = (artists[0].get("name") if artists else "") or ""
        album = item.get("album") or {}
        imgs = album.get("images") or []
        cover = (imgs[0].get("url") if imgs else "") or ""
        out.append(
            TrackCandidate(
                source="spotify",
                track_id=tid,
                track_name=name,
                artist_name=art,
                collection_name=(album.get("name") or "").strip(),
                artwork_url=cover,
                release_date=(album.get("release_date") or "")[:10],
                preview_url=item.get("preview_url") or "",
                track_view_url=(
                    (item.get("external_urls") or {}).get("spotify") or ""
                ),
                collection_id=(album.get("id") or ""),
            )
        )
    return out[:limit]


async def _spotify_search_artists(
    query: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """Артисты через Spotify Search API (нужны SPOTIFY_CLIENT_*)."""
    q = (query or "").strip()
    if not q:
        return []
    try:
        from links import _spotify_access_token
    except Exception:  # noqa: BLE001
        return []
    session = await get_session()
    token = await _spotify_access_token(session)
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, Any]] = []
    for market in ("RU", "US"):
        try:
            async with session.get(
                "https://api.spotify.com/v1/search",
                params={
                    "q": q,
                    "type": "artist",
                    "limit": str(min(limit, 10)),
                    "market": market,
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    continue
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("spotify artist search: %s", exc)
            continue
        for item in ((payload.get("artists") or {}).get("items")) or []:
            name = (item.get("name") or "").strip()
            sid = (item.get("id") or "").strip()
            if not name or not sid:
                continue
            genres = item.get("genres") or []
            out.append(
                {
                    "artist_id": sid,
                    "artist_name": name,
                    "genre": (genres[0] if genres else "") or "",
                    "source": "spotify",
                }
            )
        if out:
            break
    return out[:limit]


async def lookup_itunes_id(
    itunes_id: str,
    *,
    country: str = "ru",
) -> list[AlbumCandidate]:
    itunes_id = (itunes_id or "").strip()
    if not itunes_id.isdigit():
        raise MusicAPIError("Некорректный ID Apple Music / iTunes.")

    results: list[dict[str, Any]] = []
    for cc in (country.lower(), "us", "ru", "de"):
        payload = await _itunes_get(
            {"id": itunes_id, "entity": "song", "country": cc},
            url=ITUNES_LOOKUP_URL,
        )
        results = payload.get("results") or []
        if results:
            break
    if not results:
        raise MusicAPIError("По ссылке ничего не найдено в iTunes.")

    albums: list[AlbumCandidate] = []
    seen: set[str] = set()
    for item in results:
        album = _album_from_itunes(item)
        if album and album.source_id not in seen:
            seen.add(album.source_id)
            albums.append(album)
    if not albums:
        raise MusicAPIError("Не удалось определить альбом по ссылке.")
    return albums


async def lookup_itunes_song(
    track_id: str,
    *,
    country: str = "ru",
) -> Optional[TrackCandidate]:
    """Lookup одного трека по iTunes/Apple Music track id (?i=…)."""
    track_id = (track_id or "").strip()
    if not track_id.isdigit():
        return None
    for cc in (country.lower(), "us", "ru"):
        try:
            payload = await _itunes_get(
                {"id": track_id, "entity": "song", "country": cc},
                url=ITUNES_LOOKUP_URL,
            )
        except MusicAPIError:
            continue
        for item in payload.get("results") or []:
            if item.get("kind") != "song" and item.get("wrapperType") != "track":
                continue
            tid = item.get("trackId")
            name = (item.get("trackName") or "").strip()
            if not tid or not name:
                continue
            return TrackCandidate(
                source="itunes",
                track_id=str(int(tid)),
                track_name=name,
                artist_name=(item.get("artistName") or "").strip(),
                collection_name=(item.get("collectionName") or "").strip(),
                artwork_url=_artwork_hires(
                    item.get("artworkUrl100") or item.get("artworkUrl60") or ""
                ),
                release_date=(item.get("releaseDate") or "")[:10],
                preview_url=item.get("previewUrl") or "",
                track_view_url=item.get("trackViewUrl") or "",
                collection_id=str(int(item["collectionId"]))
                if item.get("collectionId")
                else "",
                duration_ms=int(item.get("trackTimeMillis") or 0),
            )
    return None


async def lookup_itunes_tracks(
    collection_id: str,
    *,
    country: str = "ru",
) -> AlbumDetails:
    payload = await _itunes_get(
        {
            "id": str(collection_id),
            "entity": "song",
            "country": country.lower(),
        },
        url=ITUNES_LOOKUP_URL,
    )
    results = payload.get("results") or []
    if not results:
        raise MusicAPIError("Альбом не найден в iTunes.")

    collection_meta: Optional[dict[str, Any]] = None
    tracks_raw: list[dict[str, Any]] = []
    for item in results:
        wrapper = item.get("wrapperType")
        if wrapper == "collection" or item.get("collectionType") == "Album":
            collection_meta = item
        elif wrapper == "track" or item.get("kind") == "song":
            tracks_raw.append(item)
    if collection_meta is None:
        collection_meta = results[0]

    artist = (collection_meta.get("artistName") or "").strip()
    tracks: list[TrackInfo] = []
    first_preview = ""
    for item in tracks_raw:
        name = (item.get("trackName") or "").strip()
        if not name:
            continue
        preview = item.get("previewUrl") or ""
        if preview and not first_preview:
            first_preview = preview
        tracks.append(
            TrackInfo(
                track_name=name,
                track_number=int(item.get("trackNumber") or 0),
                preview_url=preview or None,
                track_view_url=item.get("trackViewUrl"),
                artist_name=(item.get("artistName") or artist).strip(),
                duration_ms=int(item.get("trackTimeMillis") or 0),
            )
        )
    tracks.sort(key=lambda t: (t.track_number or 999, t.track_name.lower()))

    return AlbumDetails(
        source="itunes",
        source_id=str(int(collection_meta.get("collectionId") or collection_id)),
        artist_name=artist or "Неизвестный исполнитель",
        collection_name=(collection_meta.get("collectionName") or "Без названия").strip(),
        artwork_url=_artwork_hires(
            collection_meta.get("artworkUrl100")
            or collection_meta.get("artworkUrl60")
            or ""
        ),
        collection_view_url=collection_meta.get("collectionViewUrl") or "",
        release_year=_year_from_date(collection_meta.get("releaseDate") or ""),
        tracks=tracks,
        preview_url=first_preview,
    )


async def lookup_musicbrainz_tracks(mbid: str) -> AlbumDetails:
    session = await get_session()
    url = MUSICBRAINZ_RELEASE_LOOKUP.format(mbid=mbid)
    async with session.get(
        url,
        params={"inc": "recordings+artists"},
        headers=MB_HEADERS,
        timeout=aiohttp.ClientTimeout(total=8),
    ) as resp:
        if resp.status != 200:
            raise MusicAPIError("Не удалось загрузить треклист MusicBrainz.")
        xml_text = await resp.text()

    ns = {"mb": "http://musicbrainz.org/ns/mmd-2.0"}
    root = ET.fromstring(xml_text)
    release = root.find("mb:release", ns)
    if release is None and root.tag.endswith("release"):
        release = root
    if release is None:
        raise MusicAPIError("Релиз не найден.")

    title_el = release.find("mb:title", ns)
    title = (title_el.text or "").strip() if title_el is not None else "Без названия"
    names: list[str] = []
    for credit in release.findall("mb:artist-credit/mb:name-credit", ns):
        name_el = credit.find("mb:artist/mb:name", ns) or credit.find("mb:name", ns)
        if name_el is not None and name_el.text:
            names.append(name_el.text.strip())
    artist = ", ".join(names) or "Неизвестный исполнитель"
    date_el = release.find("mb:date", ns)
    year = _year_from_date((date_el.text or "") if date_el is not None else "")

    tracks: list[TrackInfo] = []
    for medium in release.findall("mb:medium-list/mb:medium", ns):
        for track in medium.findall("mb:track-list/mb:track", ns):
            pos_el = track.find("mb:position", ns)
            try:
                num = int((pos_el.text or "0") if pos_el is not None else 0) or len(tracks) + 1
            except ValueError:
                num = len(tracks) + 1
            rec = track.find("mb:recording", ns)
            name = ""
            if rec is not None:
                t_el = rec.find("mb:title", ns)
                name = (t_el.text or "").strip() if t_el is not None else ""
            if not name:
                continue
            tracks.append(
                TrackInfo(
                    track_name=name,
                    track_number=num,
                    track_view_url=youtube_search_url(f"{artist} {name}"),
                    artist_name=artist,
                )
            )
    tracks.sort(key=lambda t: (t.track_number or 999, t.track_name.lower()))

    return AlbumDetails(
        source="musicbrainz",
        source_id=mbid,
        artist_name=artist,
        collection_name=title,
        artwork_url=COVER_ART_FRONT.format(mbid=mbid),
        collection_view_url=f"https://musicbrainz.org/release/{mbid}",
        release_year=year,
        tracks=tracks,
    )


async def lookup_spotify_album_details(
    album_id: str,
    *,
    country: str = "ru",
) -> AlbumDetails:
    """Треклист Spotify-альбома (после точного match по обложке)."""
    from spotify_cover import _spotify_get_album

    album = await _spotify_get_album(album_id)
    if not album:
        raise MusicAPIError("Альбом Spotify не найден.")

    artists = ", ".join(
        (a.get("name") or "").strip()
        for a in (album.get("artists") or [])
        if a.get("name")
    )
    images = album.get("images") or []
    art = ""
    if images:
        art = max(images, key=lambda im: int(im.get("width") or 0)).get("url") or ""

    tracks: list[TrackInfo] = []
    items = ((album.get("tracks") or {}).get("items")) or []
    for item in items:
        name = (item.get("name") or "").strip()
        if not name:
            continue
        t_artists = ", ".join(
            (a.get("name") or "").strip()
            for a in (item.get("artists") or [])
            if a.get("name")
        )
        tracks.append(
            TrackInfo(
                track_name=name,
                track_number=int(item.get("track_number") or len(tracks) + 1),
                preview_url=item.get("preview_url") or None,
                track_view_url=(item.get("external_urls") or {}).get("spotify"),
                artist_name=t_artists or artists,
                duration_ms=int(item.get("duration_ms") or 0),
            )
        )

    release = (album.get("release_date") or "")[:10]
    return AlbumDetails(
        source="spotify",
        source_id=album_id,
        artist_name=artists or "Unknown",
        collection_name=(album.get("name") or "").strip(),
        artwork_url=art,
        collection_view_url=(
            (album.get("external_urls") or {}).get("spotify")
            or f"https://open.spotify.com/album/{album_id}"
        ),
        release_year=release[:4] if release else "",
        tracks=tracks,
    )


async def lookup_album_details(
    source: Source,
    source_id: str,
    *,
    country: str = "ru",
) -> AlbumDetails:
    if source == "itunes":
        return await lookup_itunes_tracks(source_id, country=country)
    if source == "musicbrainz":
        return await lookup_musicbrainz_tracks(source_id)
    if source == "spotify":
        return await lookup_spotify_album_details(source_id, country=country)
    raise MusicAPIError(f"Неизвестный источник: {source}")


def format_album_header(album: AlbumDetails) -> str:
    year = f" ({album.release_year})" if album.release_year else ""
    lines = [
        f"🎧 <b>{escape_html(album.collection_name)}</b> – "
        f"<b>{escape_html(album.artist_name)}</b>{escape_html(year)}"
    ]
    if album.collection_view_url:
        if album.source == "itunes":
            label = "Apple Music"
        elif album.source == "spotify":
            label = "Spotify"
        else:
            label = "MusicBrainz"
        lines.append(
            f'<a href="{escape_html(album.collection_view_url)}">Открыть в {label}</a>'
        )
    lines.append(f"Треков: <b>{len(album.tracks)}</b>")
    return truncate("\n".join(lines), limit=900)


def format_tracklist(album: AlbumDetails, *, limit: int = 2800) -> str:
    """Треклист без ссылок на каждый трек — иначе truncate ломает HTML в Telegram."""
    lines = ["<b>Треклист:</b>"]
    if not album.tracks:
        lines.append("<i>Треки не найдены.</i>")
        return "\n".join(lines)

    for idx, track in enumerate(album.tracks, start=1):
        num = track.track_number or idx
        name = escape_html(track.track_name)
        line = f"{num}. {name}"
        # запас под «… и ещё N»
        if len("\n".join(lines)) + len(line) + 40 > limit:
            left = len(album.tracks) - idx + 1
            lines.append(f"<i>… и ещё {left}</i>")
            break
        lines.append(line)
    return "\n".join(lines)


def format_album_caption(album: AlbumDetails) -> str:
    text = format_album_header(album) + "\n\n" + format_tracklist(album)
    # безопасный лимит: не режем посередине тега
    if len(text) <= 3500:
        return text
    cut = text[:3490]
    # убрать незакрытый хвост после последнего >
    if "<" in cut and cut.rfind("<") > cut.rfind(">"):
        cut = cut[: cut.rfind("<")]
    return cut.rstrip() + "\n…"


def format_queries_hint(queries: list[str]) -> str:
    shown = ", ".join(f"<code>{escape_html(q)}</code>" for q in queries[:3])
    return f"Запросы: {shown}"


def format_candidate_card(album: AlbumCandidate) -> str:
    year = f" ({album.release_year})" if album.release_year else ""
    return (
        f"💿 <b>{escape_html(album.collection_name)}</b>{escape_html(year)}\n"
        f"👤 {escape_html(album.artist_name)}"
    )
