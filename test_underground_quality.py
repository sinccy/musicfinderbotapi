"""
Андерграунд / короткие названия — системные проверки, не один пример.

Запуск:
  .venv/bin/python test_underground_quality.py
  .venv/bin/python test_underground_quality.py --live
"""

from __future__ import annotations

import asyncio
import sys

import download as dl


def test_short_title_collab_match() -> None:
    assert (
        dl._title_core_match(
            "XAVIERSOBASED & CHE - BLEAU [OFFICIAL MUSIC VIDEO]",
            "che",
            artist="xaviersobased",
        )
        is True
    )
    assert (
        dl._looks_like_match(
            "BLEAU XAVIERSOBASED & CHE XAVIERSOBASED CHE - BLEAU.mp3",
            artist="xaviersobased",
            title="che",
        )
        is True
    )
    # cheap ≠ che
    assert (
        dl._title_core_match("Cheap Thrills", "che", artist="Sia") is False
    )
    # hotel room service всё ещё нет
    assert dl._title_core_match("hotel room service", "hotel room") is False
    assert dl._title_core_match(
        "iPhone 16 (og version)", "iPhone 16", artist="xaviersobased"
    )
    assert (
        dl._looks_like_match(
            "xaviersobased Clorox Reverse Music",
            artist="xaviersobased",
            title="Clorox",
        )
        is False
    )


def test_youtube_scores_bleau_for_che() -> None:
    entry = {
        "title": "XAVIERSOBASED & CHE - BLEAU [OFFICIAL MUSIC VIDEO]",
        "uploader": "dash",
        "duration": 102,
    }
    sc = dl._score_youtube_entry(
        entry, artist="xaviersobased", title="che", expected=None
    )
    assert sc >= 40, sc
    junk = {
        "title": "xaviersobased Clorox Reverse Music",
        "uploader": "x",
        "duration": 94,
    }
    assert (
        dl._score_youtube_entry(
            junk, artist="xaviersobased", title="Clorox", expected=92
        )
        <= -200
    )


async def _live_ok(
    artist: str,
    title: str,
    *,
    album: str = "",
    min_sec: int = 55,
    max_sec: int = 400,
) -> None:
    r = await dl.download_single_track(
        artist,
        title,
        timeout=180,
        album=album,
        strict=bool(album),
    )
    try:
        assert r.duration and min_sec <= r.duration <= max_sec, (
            artist,
            title,
            r.duration,
        )
        assert len(r.payload()) >= 350_000
        blob = f"{r.title or ''} {r.path.name}".lower()
        for bad in ("reverse music", "type beat", "instrumental", "slowed"):
            assert bad not in blob, blob
        print(f"OK {artist} - {title}: {r.duration}s")
    finally:
        dl.cleanup_download(r.path)


async def _live_suite() -> None:
    # доступные андеграунд-треки (есть полные заливки)
    await _live_ok("xaviersobased", "hotel room", min_sec=95, max_sec=130)
    await _live_ok("xaviersobased", "che", min_sec=85, max_sec=130)  # BLEAU
    await _live_ok(
        "xaviersobased", "iPhone 16", album="Xavier", min_sec=180, max_sec=250
    )
    await _live_ok(
        "xaviersobased", "Harajuku", album="Xavier", min_sec=180, max_sec=230
    )
    await _live_ok("Nettspend", "shootin", album="HIM", min_sec=120, max_sec=160)
    await _live_ok("Ken Carson", "Yale", min_sec=90, max_sec=130)
    # другой «сложный» короткий/underground стиль
    await _live_ok("Nettspend", "beep beep", album="HIM", min_sec=110, max_sec=150)


if __name__ == "__main__":
    test_short_title_collab_match()
    test_youtube_scores_bleau_for_che()
    print("unit OK")
    if "--live" in sys.argv:
        asyncio.run(_live_suite())
        print("live OK")
