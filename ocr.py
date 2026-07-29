"""
Распознавание обложек: OCR.space → Tesseract → EasyOCR.
Кэш успешных результатов на 1 час.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
from typing import Optional

import aiohttp
from cachetools import TTLCache

from cache import get_session
from parser import extract_search_query

logger = logging.getLogger(__name__)

OCR_URL = "https://api.ocr.space/parse/image"
DEFAULT_LANGUAGE = "eng"
MAX_FILE_BYTES = 1024 * 1024
MAX_RETRIES = 2

_ocr_cache: TTLCache = TTLCache(maxsize=128, ttl=3600)

MANUAL_HINT = (
    "Не удалось распознать обложку. Пожалуйста, введите название "
    "исполнителя или альбома вручную."
)


class OCRError(Exception):
    """Ошибка распознавания обложки."""


def clean_ocr_text(ocr_text: str) -> list[str]:
    """Кандидаты для поиска из OCR-текста."""
    return extract_search_query(ocr_text)


def _guess_filetype(filename: str, image_bytes: bytes) -> str:
    name = (filename or "").lower()
    if name.endswith(".png") or image_bytes[:8].startswith(b"\x89PNG"):
        return "PNG"
    if name.endswith(".webp"):
        return "WEBP"
    return "JPG"


def _cache_key(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


async def recognize_cover(
    image_bytes: bytes,
    *,
    api_key: str = "",
    filename: str = "cover.jpg",
    language: str = DEFAULT_LANGUAGE,
    session: Optional[aiohttp.ClientSession] = None,
) -> str:
    """
    Цепочка: OCR.space (2 retry + backoff) → Tesseract → EasyOCR.
    """
    if not image_bytes:
        raise OCRError("❌ Пустое изображение.")

    try:
        from image_prep import prepare_cover_image

        image_bytes = await asyncio.to_thread(prepare_cover_image, image_bytes)
    except Exception as exc:  # noqa: BLE001
        logger.debug("OCR prep skip: %s", exc)

    key = _cache_key(image_bytes)
    cached = _ocr_cache.get(key)
    if cached:
        logger.info("OCR cache hit (%d chars)", len(cached))
        return cached

    errors: list[str] = []
    lang = (language or DEFAULT_LANGUAGE).strip() or DEFAULT_LANGUAGE
    # OCR.space Engine 2: auto / eng / rus
    if lang.lower() in {"eng+rus", "rus+eng", "en+ru"}:
        lang = "auto"

    # 1) OCR.space
    if api_key:
        try:
            text = await _ocr_space_with_retry(
                image_bytes,
                api_key,
                filename=filename,
                language=lang,
                session=session,
            )
            if text.strip():
                logger.info("OCR method=ocr.space OK (%d chars)", len(text))
                _ocr_cache[key] = text
                return text
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR.space failed: %s", exc)
            errors.append(f"ocr.space: {exc}")
            # второй заход с eng, если auto/rus не дали текст
            if lang.lower() not in {"eng", "english"}:
                try:
                    text = await _ocr_space_with_retry(
                        image_bytes,
                        api_key,
                        filename=filename,
                        language="eng",
                        session=session,
                    )
                    if text.strip():
                        logger.info("OCR method=ocr.space(eng) OK (%d chars)", len(text))
                        _ocr_cache[key] = text
                        return text
                except Exception as exc2:  # noqa: BLE001
                    errors.append(f"ocr.space/eng: {exc2}")
    else:
        errors.append("ocr.space: нет API-ключа")

    # 2) Tesseract
    try:
        text = await _tesseract_ocr(image_bytes)
        if text.strip():
            logger.info("OCR method=tesseract OK (%d chars)", len(text))
            _ocr_cache[key] = text
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tesseract failed: %s", exc)
        errors.append(f"tesseract: {exc}")

    # 3) EasyOCR
    try:
        text = await _easyocr_ocr(image_bytes)
        if text.strip():
            logger.info("OCR method=easyocr OK (%d chars)", len(text))
            _ocr_cache[key] = text
            return text
    except Exception as exc:  # noqa: BLE001
        logger.warning("EasyOCR failed: %s", exc)
        errors.append(f"easyocr: {exc}")

    logger.error("All OCR methods failed: %s", "; ".join(errors)[:400])
    raise OCRError(MANUAL_HINT)


async def extract_text_from_image(
    image_bytes: bytes,
    api_key: str,
    *,
    filename: str = "cover.jpg",
    language: str = DEFAULT_LANGUAGE,
    session: Optional[aiohttp.ClientSession] = None,
    retries: int = MAX_RETRIES,
) -> str:
    """Обратная совместимость — делегирует в recognize_cover."""
    _ = retries
    return await recognize_cover(
        image_bytes,
        api_key=api_key,
        filename=filename,
        language=language,
        session=session,
    )


async def _ocr_space_with_retry(
    image_bytes: bytes,
    api_key: str,
    *,
    filename: str,
    language: str,
    session: Optional[aiohttp.ClientSession],
) -> str:
    if len(image_bytes) > MAX_FILE_BYTES:
        logger.warning(
            "OCR.space: файл %s байт > %s — возможна ошибка free tier",
            len(image_bytes),
            MAX_FILE_BYTES,
        )

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return await _ocr_space_once(
                image_bytes,
                api_key,
                filename=filename,
                language=language,
                session=session,
            )
        except OCRError as exc:
            msg = str(exc).lower()
            if "ключ" in msg or "403" in msg:
                raise
            last_error = exc
            logger.warning(
                "OCR.space attempt %s/%s: %s", attempt, MAX_RETRIES, exc
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.6 * (2 ** (attempt - 1)))  # 0.6, 1.2
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = exc
            logger.warning(
                "OCR.space network attempt %s/%s: %s",
                attempt,
                MAX_RETRIES,
                exc,
            )
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.6 * (2 ** (attempt - 1)))

    raise OCRError(str(last_error) if last_error else "OCR.space недоступен")


async def _ocr_space_once(
    image_bytes: bytes,
    api_key: str,
    *,
    filename: str,
    language: str,
    session: Optional[aiohttp.ClientSession],
) -> str:
    if session is None:
        session = await get_session()

    filetype = _guess_filetype(filename, image_bytes)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    mime = "image/png" if filetype == "PNG" else "image/jpeg"
    base64_image = f"data:{mime};base64,{b64}"

    form = aiohttp.FormData()
    form.add_field("apikey", api_key)
    form.add_field("language", language or DEFAULT_LANGUAGE)
    form.add_field("isOverlayRequired", "false")
    form.add_field("OCREngine", "2")
    form.add_field("scale", "true")
    form.add_field("scaleImage", "true")
    form.add_field("filetype", filetype)
    form.add_field("base64Image", base64_image)

    async with session.post(
        OCR_URL,
        data=form,
        timeout=aiohttp.ClientTimeout(total=20, connect=8),
    ) as resp:
        body_text = await resp.text()
        if resp.status == 403:
            raise OCRError("OCR.space ключ отклонён (403)")
        if resp.status == 429:
            raise OCRError("OCR.space лимит (429)")
        if resp.status >= 500:
            raise OCRError(f"OCR.space HTTP {resp.status}")
        if resp.status != 200:
            raise OCRError(f"OCR.space HTTP {resp.status}")
        import json

        try:
            payload = json.loads(body_text)
        except Exception as exc:  # noqa: BLE001
            raise OCRError("Некорректный JSON OCR.space") from exc

    if payload.get("OCRExitCode") == 99 or payload.get("IsErroredOnProcessing"):
        messages = payload.get("ErrorMessage") or payload.get("ErrorDetails") or []
        detail = "; ".join(str(m) for m in messages) if isinstance(messages, list) else str(messages)
        raise OCRError(detail or "OCR.space processing error")

    parts = [
        (item.get("ParsedText") or "").strip()
        for item in (payload.get("ParsedResults") or [])
    ]
    result = "\n".join(p for p in parts if p).strip()
    if not result:
        raise OCRError("OCR.space: пустой текст")
    return result


async def _tesseract_ocr(image_bytes: bytes) -> str:
    import io

    try:
        import pytesseract  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise OCRError("pytesseract/Pillow не установлены") from exc

    def _run() -> str:
        img = Image.open(io.BytesIO(image_bytes))
        try:
            return (pytesseract.image_to_string(img, lang="eng+rus") or "").strip()
        except Exception:  # noqa: BLE001
            return (pytesseract.image_to_string(img) or "").strip()

    text = await asyncio.to_thread(_run)
    if not text:
        raise OCRError("Tesseract: пустой текст")
    return text


_easyocr_reader = None


async def _easyocr_ocr(image_bytes: bytes) -> str:
    import io

    try:
        import numpy as np  # type: ignore
        from PIL import Image  # type: ignore
    except ImportError as exc:
        raise OCRError("numpy/Pillow не установлены для EasyOCR") from exc

    def _run() -> str:
        global _easyocr_reader
        try:
            import easyocr  # type: ignore
        except ImportError as exc:
            raise OCRError("easyocr не установлен") from exc

        if _easyocr_reader is None:
            logger.info("Инициализация EasyOCR (первый запуск может занять время)…")
            _easyocr_reader = easyocr.Reader(["en", "ru"], gpu=False, verbose=False)
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        arr = np.array(img)
        lines = _easyocr_reader.readtext(arr, detail=0, paragraph=True)
        return "\n".join(str(x) for x in lines).strip()

    text = await asyncio.to_thread(_run)
    if not text:
        raise OCRError("EasyOCR: пустой текст")
    return text
