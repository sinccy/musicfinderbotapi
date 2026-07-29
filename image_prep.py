"""Подготовка фото обложки для OCR и reverse image."""

from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def prepare_cover_image(
    image_bytes: bytes,
    *,
    max_side: int = 1600,
    max_bytes: int = 900_000,
) -> bytes:
    """
    RGB → автоконтраст → ограничение стороны → JPEG ≤ max_bytes.
    Улучшает OCR и укладывается в лимит OCR.space free (~1 МБ).
    """
    if not image_bytes:
        return image_bytes
    try:
        from PIL import Image, ImageEnhance, ImageOps  # type: ignore
    except ImportError:
        return image_bytes

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img = ImageOps.exif_transpose(img)
        img = ImageOps.autocontrast(img, cutoff=1)
        img = ImageEnhance.Sharpness(img).enhance(1.15)
        img = ImageEnhance.Contrast(img).enhance(1.1)

        w, h = img.size
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / float(longest)
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.Resampling.LANCZOS,
            )

        quality = 90
        out = image_bytes
        while quality >= 55:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if len(out) <= max_bytes:
                break
            quality -= 10

        if len(out) > max_bytes:
            # ещё уменьшаем сторону
            w, h = img.size
            img = img.resize((max(1, w // 2), max(1, h // 2)), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=75, optimize=True)
            out = buf.getvalue()

        logger.info(
            "cover prep: %d → %d bytes (%sx%s)",
            len(image_bytes),
            len(out),
            img.size[0],
            img.size[1],
        )
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("prepare_cover_image: %s", exc)
        return image_bytes
