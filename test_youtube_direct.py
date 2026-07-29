"""
Прямые YouTube-ссылки и underground-артисты.

  .venv/bin/python test_youtube_direct.py
  .venv/bin/python test_youtube_direct.py --live
"""

from __future__ import annotations

import asyncio
import sys

from utils import (
    extract_youtube_video_id,
    normalize_youtube_watch_url,
    parse_music_url,
)


def test_normalize_strips_playlist() -> None:
    dirty = (
        "https://www.youtube.com/watch?v=Dr3mGAT5JLU"
        "&list=RDDr3mGAT5JLU&start_radio=1"
    )
    assert extract_youtube_video_id(dirty) == "Dr3mGAT5JLU"
    assert normalize_youtube_watch_url(dirty) == (
        "https://www.youtube.com/watch?v=Dr3mGAT5JLU"
    )
    parsed = parse_music_url(dirty)
    assert parsed is not None
    assert parsed.platform == "youtube"
    assert parsed.entity_id == "Dr3mGAT5JLU"


def test_title_core_not_hotel_room_service() -> None:
    import download as dl

    assert dl._title_core_match("hotel room", "hotel room") is True
    assert dl._title_core_match(
        "hotel room (official video)", "hotel room", artist="xaviersobased"
    ) is True
    assert dl._title_core_match(
        "hotel room service", "hotel room", artist="xaviersobased"
    ) is False
    assert dl._looks_like_match(
        "hotel room service xaviersobased",
        artist="xaviersobased",
        title="hotel room",
    ) is False


async def _live_direct_xavier() -> None:
    from download import cleanup_download, download_track, parse_artist_title_from_yt

    dirty = (
        "https://www.youtube.com/watch?v=Dr3mGAT5JLU"
        "&list=RDDr3mGAT5JLU&start_radio=1"
    )
    r = await download_track(dirty, timeout=150, artist="", title="")
    try:
        assert r.duration and r.duration >= 55, r.duration
        assert len(r.payload()) >= 400_000
        blob = f"{r.title or ''} {r.path.name}".lower()
        assert "hotel" in blob or "xavier" in blob or "sobased" in blob, blob
        # не должны уехать в чужой apple-хит
        for bad in ("jennifer", "fighting my demons", "barbie"):
            assert bad not in blob, blob
        print(f"OK direct YT: {r.duration}s title={r.title!r} file={r.path.name}")
    finally:
        cleanup_download(r.path)

    # текстовый поиск того же трека
    from download import download_single_track

    r2 = await download_single_track("xaviersobased", "hotel room", timeout=150)
    try:
        assert r2.duration and r2.duration >= 55, r2.duration
        assert len(r2.payload()) >= 400_000
        blob = f"{r2.title or ''} {r2.path.name}".lower()
        assert "service" not in blob, blob
        assert 90 <= r2.duration <= 130, r2.duration
        print(
            f"OK text xaviersobased hotel room: {r2.duration}s file={r2.path.name}"
        )
    finally:
        cleanup_download(r2.path)

    # sanity: parse title from oEmbed-like string
    art, tit = parse_artist_title_from_yt(
        "xaviersobased - hotel room (official video)", "karma archives"
    )
    assert "xavier" in art.lower() or "hotel" in tit.lower(), (art, tit)
    print(f"OK parse: {art!r} / {tit!r}")


if __name__ == "__main__":
    test_normalize_strips_playlist()
    test_title_core_not_hotel_room_service()
    print("unit OK")
    if "--live" in sys.argv:
        asyncio.run(_live_direct_xavier())
        print("live OK")
