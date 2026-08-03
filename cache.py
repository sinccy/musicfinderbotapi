"""In-memory TTL cache and shared aiohttp session."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Hashable, Optional, TypeVar

import aiohttp
from cachetools import TTLCache

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Результаты поиска (урезано под Bothost ~1GB)
_search_cache: TTLCache = TTLCache(maxsize=192, ttl=1800)
_charts_cache: TTLCache = TTLCache(maxsize=64, ttl=1800)
_lock = asyncio.Lock()

_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4, sock_read=8)


async def get_session() -> aiohttp.ClientSession:
    """Один ClientSession на текущий event loop (после asyncio.run — новый)."""
    global _session, _session_loop
    loop = asyncio.get_running_loop()
    stale = (
        _session is None
        or _session.closed
        or _session_loop is None
        or _session_loop is not loop
    )
    if stale:
        if _session is not None and not _session.closed:
            try:
                await _session.close()
            except Exception:  # noqa: BLE001
                pass
        _session = aiohttp.ClientSession(
            timeout=DEFAULT_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.5 Safari/605.1.15"
                )
            },
        )
        _session_loop = loop
        logger.info("Создан общий aiohttp.ClientSession")
    return _session


async def close_session() -> None:
    global _session, _session_loop
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None
    _session_loop = None


def cache_get(store: TTLCache, key: Hashable) -> Any:
    try:
        return store[key]
    except KeyError:
        return None


def cache_set(store: TTLCache, key: Hashable, value: Any) -> None:
    store[key] = value


def search_cache_get(key: Hashable) -> Any:
    return cache_get(_search_cache, key)


def search_cache_set(key: Hashable, value: Any) -> None:
    cache_set(_search_cache, key, value)


def charts_cache_get(key: Hashable) -> Any:
    return cache_get(_charts_cache, key)


def charts_cache_set(key: Hashable, value: Any) -> None:
    cache_set(_charts_cache, key, value)


async def cached_call(
    store: TTLCache,
    key: Hashable,
    factory: Callable[[], Any],
) -> Any:
    """Async-safe get-or-create для coroutine factory."""
    hit = cache_get(store, key)
    if hit is not None:
        return hit
    async with _lock:
        hit = cache_get(store, key)
        if hit is not None:
            return hit
        value = await factory()
        cache_set(store, key, value)
        return value
