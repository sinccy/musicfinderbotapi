"""
Глобальная очередь скачиваний + оценка пиковой нагрузки по RAM.

Формула (консервативная):
  parallel_safe ≈ floor( (RAM_MB - IDLE_MB - SAFETY_MB) / PER_DOWNLOAD_MB )

Пик одновременных MP3 важнее числа людей в /start:
  поиск/меню почти не добавляет RAM; жрёт активный yt-dlp/ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Optional, Union

from config import (
    HOST_RAM_MB,
    IDLE_RAM_ESTIMATE_MB,
    MAX_PARALLEL_DOWNLOADS,
    PEAK_PER_DOWNLOAD_MB,
    RAM_SAFETY_MB,
)

logger = logging.getLogger(__name__)

WaitCallback = Callable[[int, int], Union[None, Awaitable[None]]]

_sem: Optional[asyncio.Semaphore] = None
_active = 0
_waiting = 0
_total_done = 0
_peak_active = 0
_lock = asyncio.Lock()


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, int(MAX_PARALLEL_DOWNLOADS)))
    return _sem


@dataclass(frozen=True)
class CapacityPlan:
    host_ram_mb: int
    idle_mb: int
    per_download_mb: int
    safety_mb: int
    parallel_safe: int
    configured_parallel: int
    light_users_estimate: int


def estimate_capacity(
    *,
    host_ram_mb: int = HOST_RAM_MB,
    idle_mb: int = IDLE_RAM_ESTIMATE_MB,
    per_download_mb: int = PEAK_PER_DOWNLOAD_MB,
    safety_mb: int = RAM_SAFETY_MB,
    configured_parallel: int = MAX_PARALLEL_DOWNLOADS,
) -> CapacityPlan:
    budget = max(0, int(host_ram_mb) - int(idle_mb) - int(safety_mb))
    per = max(1, int(per_download_mb))
    parallel_safe = max(1, budget // per)
    # поиск/меню: грубо мало RAM на юзера
    light = max(50, (int(host_ram_mb) - int(safety_mb)) // 2)
    return CapacityPlan(
        host_ram_mb=int(host_ram_mb),
        idle_mb=int(idle_mb),
        per_download_mb=per,
        safety_mb=int(safety_mb),
        parallel_safe=parallel_safe,
        configured_parallel=max(1, int(configured_parallel)),
        light_users_estimate=light,
    )


def queue_stats() -> dict[str, int]:
    return {
        "active": _active,
        "waiting": _waiting,
        "peak_active": _peak_active,
        "total_done": _total_done,
        "max_parallel": max(1, int(MAX_PARALLEL_DOWNLOADS)),
    }


def format_capacity_report() -> str:
    plan = estimate_capacity()
    st = queue_stats()
    advised = min(plan.parallel_safe, plan.configured_parallel)
    peak_ram = plan.idle_mb + advised * plan.per_download_mb
    return (
        "📊 <b>Ёмкость по RAM</b>\n"
        f"Хост: <code>{plan.host_ram_mb} MB</code>\n"
        f"Idle-оценка: <code>{plan.idle_mb} MB</code>\n"
        f"На 1 скачивание: <code>~{plan.per_download_mb} MB</code>\n"
        f"Safety buffer: <code>{plan.safety_mb} MB</code>\n\n"
        f"Безопасный параллелизм MP3: <b>{plan.parallel_safe}</b>\n"
        f"В конфиге сейчас: <b>{plan.configured_parallel}</b>\n"
        f"Пик RAM при полной очереди: ~<b>{peak_ram} MB</b>\n"
        f"Лёгкие юзеры (поиск без MP3): ~<b>{plan.light_users_estimate}+</b>\n\n"
        f"Очередь: active=<b>{st['active']}</b> "
        f"waiting=<b>{st['waiting']}</b> "
        f"peak=<b>{st['peak_active']}</b>\n"
        f"Скачиваний с старта: <b>{st['total_done']}</b>\n\n"
        "<i>Считайте одновременные скачивания, не число /start.</i>"
    )


@asynccontextmanager
async def download_slot(
    *,
    on_wait: Optional[WaitCallback] = None,
    label: str = "download",
) -> AsyncIterator[None]:
    """Занять глобальный слот скачивания (yt-dlp/ffmpeg)."""
    global _active, _waiting, _total_done, _peak_active

    sem = _get_sem()
    async with _lock:
        _waiting += 1
        position = _waiting
        waiting_now = _waiting

    acquired = False
    try:
        if on_wait is not None and sem.locked():
            try:
                maybe = on_wait(position, waiting_now)
                if asyncio.iscoroutine(maybe):
                    await maybe
            except Exception as exc:  # noqa: BLE001
                logger.debug("on_wait: %s", exc)

        t0 = time.monotonic()
        await sem.acquire()
        acquired = True
        async with _lock:
            _waiting = max(0, _waiting - 1)
            _active += 1
            _peak_active = max(_peak_active, _active)
            active_now = _active
        logger.info(
            "download slot + label=%s wait=%.2fs active=%s/%s",
            label,
            time.monotonic() - t0,
            active_now,
            MAX_PARALLEL_DOWNLOADS,
        )
        try:
            yield
        finally:
            async with _lock:
                _active = max(0, _active - 1)
                _total_done += 1
            sem.release()
            try:
                from memory_trim import trim_memory

                trim_memory(f"after:{label}")
            except Exception:  # noqa: BLE001
                pass
            logger.debug("download slot - label=%s", label)
    finally:
        if not acquired:
            async with _lock:
                _waiting = max(0, _waiting - 1)
