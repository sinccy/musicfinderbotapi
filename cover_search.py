"""
Поиск по обложке — два параллельных канала, один точный ответ:

  A) интернет: reverse image → названия
  B) OCR: текст с обложки

Каталог + pHash → возвращаем РОВНО один лучший альбом
(или пусто, если уверенности нет).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from music import (
    AlbumCandidate,
    merge_albums,
    search_albums_multi,
    search_itunes_albums,
)

logger = logging.getLogger(__name__)

_DASH = re.compile(r"\s+[–—\-]\s+")
_EDITION_PENALTY = [
    (re.compile(r"(?i)tribute|karaoke|piano covers?|lo-?fi"), 60),
    (re.compile(r"(?i)super\s*deluxe"), 35),
    (re.compile(r"(?i)\bdeluxe\b"), 20),
    (re.compile(r"(?i)live at|bootleg|anthology"), 25),
    (re.compile(r"(?i)soundtrack|from the|sings the"), 30),
    (re.compile(r"(?i)\bcovers?\b"), 40),
]

# минимальная уверенность по названию, если pHash не сработал
_MIN_TITLE_SCORE = 95


@dataclass
class CoverSearchResult:
    albums: list[AlbumCandidate] = field(default_factory=list)
    web_titles: list[str] = field(default_factory=list)
    ocr_titles: list[str] = field(default_factory=list)
    confidence: str = ""  # visual | title | ""

    @property
    def all_titles(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for t in [*self.web_titles, *self.ocr_titles]:
            key = t.lower().strip()
            if len(key) < 2 or key in seen:
                continue
            seen.add(key)
            out.append(t)
        return out

    @property
    def best(self) -> AlbumCandidate | None:
        return self.albums[0] if self.albums else None


async def find_albums_from_cover_image(
    image_bytes: bytes,
    *,
    country: str = "ru",
    limit: int = 1,
    ocr_api_key: str = "",
    ocr_language: str = "auto",
) -> CoverSearchResult:
    """Параллельно интернет + OCR → один лучший альбом."""
    from cover_match import rank_albums_by_phash
    from ocr import OCRError, clean_ocr_text, recognize_cover
    from reverse_image import reverse_cover_queries

    _ = limit  # всегда один результат

    async def _web() -> list[str]:
        try:
            return await asyncio.wait_for(
                reverse_cover_queries(image_bytes, limit=8),
                timeout=45,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cover web/reverse: %s", exc)
            return []

    async def _ocr() -> list[str]:
        if not ocr_api_key:
            return []
        try:
            text = await asyncio.wait_for(
                recognize_cover(
                    image_bytes,
                    api_key=ocr_api_key,
                    filename="cover.jpg",
                    language=ocr_language,
                ),
                timeout=35,
            )
            return clean_ocr_text(text)[:5] if text else []
        except OCRError as exc:
            logger.info("cover OCR empty: %s", exc)
            return []
        except Exception as exc:  # noqa: BLE001
            logger.warning("cover OCR: %s", exc)
            return []

    web_titles, ocr_titles = await asyncio.gather(_web(), _ocr())
    result = CoverSearchResult(web_titles=web_titles, ocr_titles=ocr_titles)
    titles = result.all_titles
    logger.info("cover parallel titles web=%s ocr=%s", web_titles, ocr_titles)

    if not titles:
        return result

    album_task = asyncio.create_task(
        search_albums_multi(
            titles,
            limit=25,
            country=country,
            max_queries=min(5, len(titles)),
        )
    )
    extra_tasks = [
        asyncio.create_task(
            search_itunes_albums(
                _normalize_title_query(t), limit=6, country=country
            )
        )
        for t in titles[:5]
        if len(_normalize_title_query(t)) >= 4
    ]

    gathered = await asyncio.gather(
        album_task, *extra_tasks, return_exceptions=True
    )
    groups: list[list[AlbumCandidate]] = []
    for item in gathered:
        if isinstance(item, Exception):
            logger.debug("catalog search err: %s", item)
            continue
        if item:
            groups.append(item)  # type: ignore[arg-type]

    albums = merge_albums(*groups, limit=40) if groups else []
    if not albums:
        return result

    matches, _others = await rank_albums_by_phash(
        image_bytes, albums, max_check=min(30, len(albums))
    )

    if matches:
        best = _pick_single(matches, titles)
        result.albums = [best]
        result.confidence = "visual"
        logger.info(
            "cover BEST visual: %s – %s",
            best.artist_name,
            best.collection_name,
        )
        return result

    # Без визуального матча — только если название однозначно
    scored = sorted(
        albums,
        key=lambda a: _confidence_score(a, titles),
        reverse=True,
    )
    top = scored[0]
    top_score = _confidence_score(top, titles)
    second = _confidence_score(scored[1], titles) if len(scored) > 1 else 0
    # нужен явный отрыв от второго места
    if top_score >= _MIN_TITLE_SCORE and top_score >= second + 25:
        result.albums = [top]
        result.confidence = "title"
        logger.info(
            "cover BEST title(%s): %s – %s",
            top_score,
            top.artist_name,
            top.collection_name,
        )
        return result

    logger.info(
        "cover: no confident single match (top=%s second=%s)",
        top_score,
        second,
    )
    return result


def _pick_single(
    matches: list[AlbumCandidate], titles: list[str]
) -> AlbumCandidate:
    """Один лучший среди визуальных совпадений."""
    return max(matches, key=lambda a: _confidence_score(a, titles))


def _edition_penalty(album: AlbumCandidate) -> int:
    name = album.collection_name
    pen = 0
    for rx, val in _EDITION_PENALTY:
        if rx.search(name):
            pen += val
    # чуть предпочитаем более короткое «чистое» название
    pen += min(len(name) // 8, 8)
    return pen


def _normalize_title_query(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    t = re.sub(r"(?i)\b(обложка|album cover|cover|vinyl|lp)\b", "", t).strip()
    t = re.sub(r"\s+", " ", t)
    return t[:80]


def _title_score(album: AlbumCandidate, titles: list[str]) -> int:
    art = album.artist_name.lower()
    name = album.collection_name.lower()
    # убрать скобки изданий для сравнения
    bare = re.sub(r"\s*[\(\[].*?[\)\]]", "", name).strip()
    score = 0
    for t in titles:
        tl = t.lower()
        if bare and bare in tl:
            score += 55
        elif name and name in tl:
            score += 45
        if art and art in tl:
            score += 45
        for tok in re.findall(r"[a-zа-я0-9']{3,}", tl, flags=re.I):
            if tok in bare or tok in art:
                score += 3
        if _DASH.search(t):
            parts = _DASH.split(t, maxsplit=1)
            if len(parts) == 2:
                left, right = parts[0].lower().strip(), parts[1].lower().strip()
                if left in art or art in left:
                    score += 20
                if right in bare or bare in right:
                    score += 20
    return score


def _confidence_score(album: AlbumCandidate, titles: list[str]) -> int:
    return _title_score(album, titles) - _edition_penalty(album)
