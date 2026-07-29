"""
Сравнение загруженной обложки с artwork iTunes через perceptual hash.
Используем min(pHash, dHash) — устойчивее к фото с телефона.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, Optional

import aiohttp

from cache import get_session

if TYPE_CHECKING:
    from music import AlbumCandidate

logger = logging.getLogger(__name__)

PHASH_THRESHOLD = 12
NEAR_THRESHOLD = 18
MAX_CANDIDATES_TO_CHECK = 28


def compute_hashes(image_bytes: bytes) -> tuple:
    """(phash, dhash) изображения."""
    try:
        import imagehash  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "Нужны Pillow и ImageHash: pip install Pillow ImageHash"
        ) from exc

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    img = img.resize((256, 256))
    return imagehash.phash(img), imagehash.dhash(img)


def compute_phash(image_bytes: bytes):
    """Обратная совместимость — только pHash."""
    return compute_hashes(image_bytes)[0]


def hamming(a, b) -> int:
    try:
        return int(a - b)
    except Exception:  # noqa: BLE001
        return 999


def distance(user_hashes: tuple, cover_hashes: tuple) -> int:
    """Минимальное расстояние по pHash/dHash."""
    up, ud = user_hashes
    cp, cd = cover_hashes
    return min(hamming(up, cp), hamming(ud, cd))


async def _fetch_cover_bytes(url: str) -> Optional[bytes]:
    if not url:
        return None
    for size in ("100x100bb", "60x60bb", "30x30bb"):
        if size in url:
            url = url.replace(size, "600x600bb")
            break
    session = await get_session()
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=8, connect=4),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
            return data if len(data) > 200 else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("cover fetch failed %s: %s", url[:60], exc)
        return None


async def rank_albums_by_phash(
    user_image: bytes,
    albums: list["AlbumCandidate"],
    *,
    threshold: int = PHASH_THRESHOLD,
    max_check: int = MAX_CANDIDATES_TO_CHECK,
) -> tuple[list["AlbumCandidate"], list["AlbumCandidate"]]:
    """
    Возвращает (visual_matches, others).
    visual_matches отсортированы по возрастанию Hamming distance.
    """
    if not user_image or not albums:
        return [], list(albums)

    try:
        user_hashes = await asyncio.to_thread(compute_hashes, user_image)
    except Exception as exc:  # noqa: BLE001
        logger.warning("user hash failed: %s", exc)
        return [], list(albums)

    logger.info(
        "User cover hashes=%s/%s, checking up to %s albums",
        user_hashes[0],
        user_hashes[1],
        max_check,
    )

    scored: list[tuple[int, "AlbumCandidate"]] = []
    to_check = albums[:max_check]

    async def _one(album: "AlbumCandidate") -> Optional[tuple[int, "AlbumCandidate"]]:
        data = await _fetch_cover_bytes(album.artwork_url)
        if not data:
            return None
        try:
            cover_hashes = await asyncio.to_thread(compute_hashes, data)
            dist = distance(user_hashes, cover_hashes)
            logger.info(
                "hash dist=%s for %s – %s",
                dist,
                album.artist_name,
                album.collection_name,
            )
            return dist, album
        except Exception as exc:  # noqa: BLE001
            logger.debug("album hash failed: %s", exc)
            return None

    results = await asyncio.gather(*[_one(a) for a in to_check], return_exceptions=True)
    for res in results:
        if isinstance(res, tuple) and res is not None:
            scored.append(res)

    scored.sort(key=lambda x: x[0])
    matches = [a for d, a in scored if d <= threshold]
    match_ids = {a.source_id for a in matches}
    others = [a for a in albums if a.source_id not in match_ids]

    if not matches and scored:
        near = [a for d, a in scored if d <= NEAR_THRESHOLD][:5]
        if near:
            logger.info("No strict hash match; near=%s", len(near))
            near_ids = {a.source_id for a in near}
            return near, [a for a in albums if a.source_id not in near_ids]

    return matches, others
