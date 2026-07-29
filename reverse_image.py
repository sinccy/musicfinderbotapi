"""
Reverse image search для обложек альбомов.

1) SerpAPI Google Lens (если задан SERPAPI_API_KEY)
2) Яндекс.Картинки (бесплатно) — litterbox / tmpfiles / multipart

Временный хостинг нужен только для URL-поиска; есть и upload напрямую.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import aiohttp
from bs4 import BeautifulSoup

from cache import get_session
from config import SERPAPI_API_KEY
from parser import extract_search_query

logger = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_MUSIC_HINT = re.compile(
    r"(?i)\b("
    r"album|vinyl|lp|ep|single|soundtrack|discogs|bandcamp|"
    r"spotify|apple\s*music|itunes|genius|last\.?fm|"
    r"альбом|винилл?|пластинка|обложка"
    r")\b"
)
_BY_RE = re.compile(r"(?i)\s+by\s+")
_DASH_RE = re.compile(r"\s+[–—\-]\s+")
_NOISE_TITLE = re.compile(
    r"(?i)^("
    r"yandex|яндекс|google|images?|поиск|search|wikipedia|youtube|"
    r"pinterest|reddit|tiktok|instagram|facebook|twitter|x\.com|"
    r"картинки|image\s*search|поиск по изображению|"
    r"\$query|всё изображение|установите расширение|"
    r"платье|растение|брюки|пиджак|москва"
    r")"
)
_DIM_RE = re.compile(r"^\d+\s*[×xX]\s*\d+$")
_MARKET_RE = re.compile(
    r"(?i)\b(ozon|wildberries|avito|ebay|amazon|купить|цена|collectible)\b"
)
_SPOTIFY_LINK_RE = re.compile(
    r"open\.spotify\.com/(?:intl-[a-z]{2}/)?(album|track)/([a-zA-Z0-9]{22})",
    re.IGNORECASE,
)


async def reverse_cover_queries(
    image_bytes: bytes,
    *,
    limit: int = 6,
) -> list[str]:
    """Возвращает поисковые запросы (артист / альбом) из reverse image."""
    if not image_bytes:
        return []

    titles: list[str] = []
    public_url = await _upload_temp(image_bytes)

    if SERPAPI_API_KEY and public_url:
        titles.extend(await _serpapi_lens(public_url))

    if public_url:
        logger.info("reverse_image public url: %s", public_url[:80])
        titles.extend(await _yandex_url_search(public_url))

    if len(titles) < 2:
        titles.extend(await _yandex_upload_search(image_bytes))

    queries = _titles_to_queries(titles, limit=limit)
    logger.info("reverse_image raw_titles=%d queries=%s", len(titles), queries)
    return queries


def _titles_to_queries(titles: list[str], *, limit: int) -> list[str]:
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for raw in titles:
        text = html_lib.unescape((raw or "").strip())
        text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 3 or len(text) > 100:
            continue
        if _DIM_RE.match(text) or _NOISE_TITLE.search(text):
            continue
        if _MARKET_RE.search(text) and len(text) > 50:
            # вытащить короткое «Artist Album» из маркетплейс-строки
            m = re.search(
                r"(?i)((?:the\s+)?[A-ZА-Я][\w''.&\-]+(?:\s+[\w''.&\-]+){1,5}"
                r".{0,5}(?:album|lp|ep|vinyl|альбом)?)",
                text,
            )
            if m:
                text = m.group(1).strip()
            else:
                continue

        text = re.sub(
            r"\s*[|\u2013\u2014\-]\s*"
            r"(Discogs|Spotify|Apple Music|YouTube|Wikipedia|Яндекс|OZON).*$",
            "",
            text,
            flags=re.I,
        )
        text = text.strip(" ·|-–—\"'")
        if len(text) < 3 or _DIM_RE.match(text):
            continue

        words = text.split()
        boost = 0
        # короткие cbir-теги вроде «the beatles abbey road» — лучшие
        if 2 <= len(words) <= 6:
            boost += 5
        elif len(words) == 1:
            boost -= 1
        elif len(words) > 10:
            boost -= 3

        if _MUSIC_HINT.search(text) and len(words) <= 8:
            boost += 2
        if _DASH_RE.search(text) or _BY_RE.search(text):
            boost += 2

        variants = [text]
        m = _BY_RE.split(text, maxsplit=1)
        if len(m) == 2 and len(m[0]) > 1 and len(m[1]) > 1:
            variants.append(f"{m[1].strip()} {m[0].strip()}")

        for v in variants:
            # для коротких тегов не дробить через OCR-парсер
            if 2 <= len(v.split()) <= 6 and len(v) <= 60:
                candidates = [v]
            else:
                parsed = extract_search_query(v)[:2]
                candidates = parsed if parsed else [v[:80]]
            for q in candidates:
                q = q.strip()
                key = q.lower()
                if len(key) < 3 or key in seen or _DIM_RE.match(q):
                    continue
                if _NOISE_TITLE.search(q):
                    continue
                seen.add(key)
                q_boost = boost
                qw = q.split()
                if 2 <= len(qw) <= 6:
                    q_boost += 2
                scored.append((q_boost, q))

    scored.sort(key=lambda x: (-x[0], len(x[1])))
    return [q for _, q in scored[:limit]]


async def _upload_temp(image_bytes: bytes) -> str:
    session = await get_session()
    for uploader in (_upload_litterbox, _upload_tmpfiles, _upload_catbox):
        try:
            url = await uploader(session, image_bytes)
            if url and url.startswith("http"):
                return url
        except Exception as exc:  # noqa: BLE001
            logger.warning("upload %s: %s", uploader.__name__, exc)
    return ""


async def _upload_litterbox(session: aiohttp.ClientSession, data: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", "1h")
    form.add_field(
        "fileToUpload",
        data,
        filename="cover.jpg",
        content_type="image/jpeg",
    )
    async with session.post(
        "https://litterbox.catbox.moe/resources/internals/api.php",
        data=form,
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=40, connect=10),
    ) as resp:
        text = (await resp.text()).strip()
        if resp.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"litterbox HTTP {resp.status}: {text[:120]}")


async def _upload_catbox(session: aiohttp.ClientSession, data: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("reqtype", "fileupload")
    form.add_field(
        "fileToUpload",
        data,
        filename="cover.jpg",
        content_type="image/jpeg",
    )
    async with session.post(
        "https://catbox.moe/user/api.php",
        data=form,
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=40, connect=10),
    ) as resp:
        text = (await resp.text()).strip()
        if resp.status == 200 and text.startswith("http"):
            return text
        raise RuntimeError(f"catbox HTTP {resp.status}: {text[:120]}")


async def _upload_tmpfiles(session: aiohttp.ClientSession, data: bytes) -> str:
    form = aiohttp.FormData()
    form.add_field("file", data, filename="cover.jpg", content_type="image/jpeg")
    async with session.post(
        "https://tmpfiles.org/api/v1/upload",
        data=form,
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=40, connect=10),
    ) as resp:
        if resp.status != 200:
            raise RuntimeError(f"tmpfiles HTTP {resp.status}")
        payload = await resp.json(content_type=None)
    url = ((payload.get("data") or {}).get("url") or "").strip()
    if "tmpfiles.org/" in url and "/dl/" not in url:
        url = url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
    return url


async def _serpapi_lens(image_url: str) -> list[str]:
    session = await get_session()
    params = {
        "engine": "google_lens",
        "url": image_url,
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
    }
    try:
        async with session.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=aiohttp.ClientTimeout(total=45, connect=10),
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                logger.warning("serpapi HTTP %s: %s", resp.status, body[:200])
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("serpapi: %s", exc)
        return []

    titles: list[str] = []
    kg = payload.get("knowledge_graph") or {}
    if isinstance(kg, dict):
        for key in ("title", "subtitle", "name"):
            if kg.get(key):
                titles.append(str(kg[key]))

    for item in payload.get("visual_matches") or []:
        if isinstance(item, dict) and item.get("title"):
            titles.append(str(item["title"]))

    for item in payload.get("text_results") or []:
        if isinstance(item, dict) and item.get("text"):
            titles.append(str(item["text"])[:120])

    logger.info("serpapi titles: %d", len(titles))
    return titles[:20]


async def _yandex_url_search(image_url: str) -> list[str]:
    session = await get_session()
    try:
        async with session.get(
            "https://yandex.ru/images/search",
            params={"rpt": "imageview", "url": image_url},
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=35, connect=10),
            allow_redirects=True,
        ) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                logger.warning("yandex url search HTTP %s", resp.status)
                return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex url search: %s", exc)
        return []
    return _parse_yandex_html(raw)


async def _yandex_upload_search(image_bytes: bytes) -> list[str]:
    session = await get_session()
    form = aiohttp.FormData()
    form.add_field("rpt", "imageview")
    form.add_field(
        "upfile",
        image_bytes,
        filename="cover.jpg",
        content_type="image/jpeg",
    )
    try:
        async with session.post(
            "https://yandex.ru/images/search",
            data=form,
            params={"rpt": "imageview"},
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=45, connect=10),
            allow_redirects=True,
        ) as resp:
            raw = await resp.text()
            if resp.status >= 400:
                logger.warning("yandex upload HTTP %s", resp.status)
                return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex upload: %s", exc)
        return []
    return _parse_yandex_html(raw)


def _parse_yandex_html(raw: str) -> list[str]:
    if not raw:
        return []

    # Яндекс часто отдаёт JSON внутри HTML с &quot;
    text = html_lib.unescape(raw)
    titles: list[str] = []

    # cbirTags — самый ценный сигнал
    for m in re.finditer(
        r'"text"\s*:\s*"([^"\\]{2,120})"',
        text,
    ):
        titles.append(_unescape_json(m.group(1)))

    for m in re.finditer(
        r'"(?:title|subtitle|snippet|caption|originalTitle|cbirTitle)"\s*:\s*"([^"\\]{2,160})"',
        text,
    ):
        titles.append(_unescape_json(m.group(1)))

    # title + subtitle рядом (Abbey Road / Альбом · The Beatles)
    for m in re.finditer(
        r'"title"\s*:\s*"([^"\\]{2,100})"\s*,\s*"subtitle"\s*:\s*"([^"\\]{2,120})"',
        text,
    ):
        t = _unescape_json(m.group(1))
        sub = _unescape_json(m.group(2))
        titles.append(t)
        titles.append(sub)
        # «Альбом · The Beatles» → Beatles + title
        artist = re.sub(r"(?i)^\s*альбом\s*[·•|\-–—]?\s*", "", sub).strip()
        if artist and t:
            titles.append(f"{artist} {t}")

    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:  # noqa: BLE001
        soup = BeautifulSoup(raw, "html.parser")

    for a in soup.select("a[href*='text=']"):
        href = a.get("href") or ""
        q = _query_from_href(html_lib.unescape(href))
        if q:
            titles.append(q)
        t = a.get_text(" ", strip=True)
        if t and 3 <= len(t) <= 160:
            titles.append(t)

    for sel in (
        ".CbirItem-Title",
        ".CbirTags-Item",
        ".OrganicTitle",
        ".OrganicTitle-LinkText",
        ".Meta-Title",
        "[class*='CbirTags']",
    ):
        for el in soup.select(sel)[:50]:
            t = el.get_text(" ", strip=True)
            if t and 3 <= len(t) <= 160:
                titles.append(t)

    out: list[str] = []
    seen: set[str] = set()
    for t in titles:
        key = re.sub(r"\s+", " ", t).strip().lower()
        if key in seen or len(key) < 3:
            continue
        if _NOISE_TITLE.search(key):
            continue
        seen.add(key)
        out.append(re.sub(r"\s+", " ", t).strip())

    logger.info("yandex parsed titles: %d (sample=%s)", len(out), out[:5])
    return out[:40]


def _query_from_href(href: str) -> str:
    try:
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("text", "query", "q"):
            if key in qs and qs[key]:
                return unquote(qs[key][0]).strip()
    except Exception:  # noqa: BLE001
        return ""
    return ""


def _unescape_json(s: str) -> str:
    return (
        s.replace("\\/", "/")
        .replace("\\n", " ")
        .replace("\\t", " ")
        .replace('\\"', '"')
        .replace("\\u0026", "&")
        .strip()
    )


def _extract_spotify_links(text: str) -> list[tuple[str, str]]:
    """Только ссылки Spotify album/track — без текстовых «объектов»."""
    if not text:
        return []
    text = html_lib.unescape(text)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for m in _SPOTIFY_LINK_RE.finditer(text):
        ent, eid = m.group(1).lower(), m.group(2)
        key = f"{ent}:{eid}"
        if key in seen:
            continue
        seen.add(key)
        out.append((ent, eid))
    return out


async def find_spotify_links_from_image(
    image_bytes: bytes,
) -> list[tuple[str, str]]:
    """
    Поиск по картинке → список (album|track, id) со Spotify.
    Игнорируем подписи/объекты — берём только URL релизов.
    """
    if not image_bytes:
        return []

    blobs: list[str] = []
    public_url = await _upload_temp(image_bytes)

    if public_url:
        logger.info("spotify-links public url: %s", public_url[:80])
        if SERPAPI_API_KEY:
            blobs.append(await _serpapi_lens_blob(public_url))
        blobs.append(await _yandex_html_by_url(public_url))

    # если URL-хост не дал Spotify-ссылок — multipart в Яндекс
    links = _merge_spotify_links(blobs)
    if not links:
        blobs.append(await _yandex_html_upload(image_bytes))
        links = _merge_spotify_links(blobs)

    logger.info("spotify links from image: %s", links[:12])
    return links


def _merge_spotify_links(blobs: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for blob in blobs:
        for ent, eid in _extract_spotify_links(blob):
            key = f"{ent}:{eid}"
            if key in seen:
                continue
            seen.add(key)
            out.append((ent, eid))
    return out


async def _serpapi_lens_blob(image_url: str) -> str:
    """Сырой JSON SerpAPI — из него вытащим spotify-ссылки."""
    session = await get_session()
    params = {
        "engine": "google_lens",
        "url": image_url,
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
    }
    try:
        async with session.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=aiohttp.ClientTimeout(total=45, connect=10),
        ) as resp:
            return await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.warning("serpapi blob: %s", exc)
        return ""


async def _yandex_html_by_url(image_url: str) -> str:
    session = await get_session()
    try:
        async with session.get(
            "https://yandex.ru/images/search",
            params={"rpt": "imageview", "url": image_url},
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=35, connect=10),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return ""
            return await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex html url: %s", exc)
        return ""


async def _yandex_html_upload(image_bytes: bytes) -> str:
    session = await get_session()
    form = aiohttp.FormData()
    form.add_field("rpt", "imageview")
    form.add_field(
        "upfile",
        image_bytes,
        filename="cover.jpg",
        content_type="image/jpeg",
    )
    try:
        async with session.post(
            "https://yandex.ru/images/search",
            data=form,
            params={"rpt": "imageview"},
            headers={"User-Agent": UA, "Accept-Language": "ru,en;q=0.9"},
            timeout=aiohttp.ClientTimeout(total=45, connect=10),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return ""
            return await resp.text()
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex html upload: %s", exc)
        return ""
