"""
Принудительный возврат RAM ОС (Linux glibc).

После bootstrap / yt-dlp Python и allocator часто держат high-water
(у тебя: ~600MB → через час ~230MB). trim ускоряет сброс.
"""

from __future__ import annotations

import gc
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_TRIM_ENABLED = os.getenv("MEMORY_TRIM", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def _rss_mb() -> Optional[int]:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    # kB
                    return int(parts[1]) // 1024
    except Exception:  # noqa: BLE001
        return None
    return None


def trim_memory(reason: str = "") -> Optional[int]:
    """gc + malloc_trim. Возвращает VmRSS MB после trim (Linux) или None."""
    if not _TRIM_ENABLED:
        return _rss_mb()
    before = _rss_mb()
    try:
        gc.collect()
        gc.collect()
    except Exception:  # noqa: BLE001
        pass
    trimmed = False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        if hasattr(libc, "malloc_trim"):
            libc.malloc_trim(0)
            trimmed = True
    except Exception:  # noqa: BLE001
        trimmed = False
    after = _rss_mb()
    if before is not None and after is not None:
        freed = before - after
        if freed >= 5 or reason in {"startup", "bootstrap"}:
            logger.info(
                "memory trim%s: %s→%s MB (freed %s) malloc_trim=%s",
                f" [{reason}]" if reason else "",
                before,
                after,
                freed,
                "yes" if trimmed else "no",
            )
    return after
