"""
Качество скачивания: не превью, не лайв, не remix, длина ≈ каталог.

Запуск:
  .venv/bin/python -m pytest test_download_quality.py -q
  .venv/bin/python test_download_quality.py --live   # сеть + реальные скачивания
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import download as dl


def test_reject_junk_titles() -> None:
    assert dl._looks_like_match(
        "How 'wheredoistart' By Ken Carson Was Made",
        artist="Ken Carson",
        title="wheredoistart",
    ) is False
    assert dl._looks_like_match(
        "Ken Carson - wheredoistart (new snippet)",
        artist="Ken Carson",
        title="wheredoistart",
    ) is False
    assert dl._looks_like_match(
        "Ken Carson - deaf note live @ Rolling Loud",
        artist="Ken Carson",
        title="deaf note",
    ) is False
    assert dl._looks_like_match(
        "Ken Carson - deaf note (Instrumental)",
        artist="Ken Carson",
        title="deaf note",
    ) is False
    assert dl._looks_like_match(
        "Ken Carson - deaf note (remix)",
        artist="Ken Carson",
        title="deaf note",
    ) is False
    assert dl._looks_like_match(
        "Ken Carson - wheredoistart (Remaster)",
        artist="Ken Carson",
        title="wheredoistart",
    ) is True
    assert dl._looks_like_match(
        "deaf note - ken carson with Playboi Carti.mp3",
        artist="Ken Carson",
        title="deaf note",
    ) is True


def test_duration_vs_catalog() -> None:
    # каталог 163с — отсекаем превью и сильно обрезанный remaster
    assert dl._is_full_track_duration(28, expected=163) is False
    assert dl._is_full_track_duration(120, expected=163) is False
    assert dl._is_full_track_duration(144, expected=163) is True
    assert dl._is_full_track_duration(167, expected=163) is True
    assert dl._is_full_track_duration(200, expected=163) is False
    # без каталога — просто полный трек
    assert dl._is_full_track_duration(28) is False
    assert dl._is_full_track_duration(90) is True


def test_youtube_score_rejects_live_and_howto() -> None:
    junk = {
        "title": "(99% ACCURATE) How 'wheredoistart' By Ken Carson Was Made",
        "uploader": "v4mq",
        "duration": 264,
    }
    live = {
        "title": "Ken Carson - Deaf Note live @ Openair",
        "uploader": "x",
        "duration": 95,
    }
    short = {
        "title": "Ken Carson - wheredoistart (Remaster)",
        "uploader": "NOT.E",
        "duration": 120,
    }
    # чужой трек с подписью /wheredoistart — фейк, не студия
    fake = {
        "title": "Ken Carson - Grow Apart/wheredoistart (remaster)",
        "uploader": "OpiumGlazer2009*",
        "duration": 167,
    }
    good = {
        "title": "Ken Carson - wheredoistart (Official Audio)",
        "uploader": "Ken Carson",
        "duration": 164,
    }
    assert dl._score_youtube_entry(junk, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_youtube_entry(live, artist="Ken Carson", title="deaf note", expected=198) <= -90
    assert dl._score_youtube_entry(short, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_youtube_entry(fake, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_youtube_entry(good, artist="Ken Carson", title="wheredoistart", expected=163) >= 40


def test_soundcloud_score_rejects_teaser_and_slowed() -> None:
    teaser = {
        "title": "wheredoistart",
        "user": {"username": "Ken Carson"},
        "duration": 28445,  # ms
        "permalink_url": "https://soundcloud.com/kencarson/wheredoistart",
    }
    slowed = {
        "title": "ken carson - wheredoistart (slowed)",
        "user": {"username": "x"},
        "duration": 177800,
        "permalink_url": "https://soundcloud.com/x/y",
    }
    ok = {
        "title": "Ken Carson - wheredoistart (Official Audio)",
        "user": {"username": "Ken Carson"},
        "duration": 163000,
        "permalink_url": "https://soundcloud.com/kencarson/wheredoistart-full",
    }
    fake = {
        "title": "Ken_Carson_-_wheredoistart_Grow_Apart",
        "user": {"username": "Future The Real Me"},
        "duration": 144400,
        "permalink_url": "https://soundcloud.com/x/z",
    }
    assert dl._score_soundcloud_track(teaser, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_soundcloud_track(slowed, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_soundcloud_track(fake, artist="Ken Carson", title="wheredoistart", expected=163) <= -200
    assert dl._score_soundcloud_track(ok, artist="Ken Carson", title="wheredoistart", expected=163) >= 40


def test_ytmusic_score_yale() -> None:
    good = {
        "title": "Yale",
        "artists": [{"name": "Ken Carson"}],
        "videoId": "NaEl1gDI124",
        "duration": "1:47",
        "album": {"name": "Teen X"},
    }
    remix = {
        "title": "Yale (whotfiskapo Remix)",
        "artists": [{"name": "Ken Carson"}],
        "videoId": "x",
        "duration": "1:49",
    }
    wrong = {
        "title": "Fighting Demons",
        "artists": [{"name": "Juice WRLD"}],
        "videoId": "y",
        "duration": "3:21",
    }
    assert dl._score_ytmusic_song(good, artist="Ken Carson", title="Yale", expected=106) >= 70
    assert dl._score_ytmusic_song(remix, artist="Ken Carson", title="Yale", expected=106) <= -200
    assert dl._score_ytmusic_song(wrong, artist="Ken Carson", title="Yale", expected=106) <= -200


def test_pick_closest_duration() -> None:
    """Как в download_track: из двух кандидатов берём ближе к каталогу."""
    a = dl.DownloadedAudio(path=Path("a.mp3"), title="a", artist="x", duration=144)
    b = dl.DownloadedAudio(path=Path("b.mp3"), title="b", artist="x", duration=167)
    expected = 163

    def closeness(x: dl.DownloadedAudio) -> tuple:
        return (abs((x.duration or 0) - expected), -(x.duration or 0))

    best = min([a, b], key=closeness)
    assert best.duration == 167


def test_artist_name_ok_no_partial() -> None:
    """MikeCarson не должен считаться Ken Carson (ломало Yale → 236с)."""
    assert dl._artist_name_ok("Ken Carson", "Ken Carson") is True
    assert dl._artist_name_ok("Ken Carson", "Ken Car$on") is True
    assert dl._artist_name_ok("Ken Carson", "MikeCarson") is False
    assert dl._artist_name_ok("Ken Carson", "Mike Carson") is False
    assert dl._artist_name_ok("Markul", "Markul") is True


async def _live_one(artist: str, title: str, *, min_sec: int, max_sec: int) -> None:
    from download import cleanup_download, download_single_track

    r = await download_single_track(artist, title, timeout=150)
    try:
        assert r.duration is not None, f"{artist} - {title}: no duration"
        assert min_sec <= r.duration <= max_sec, (
            f"{artist} - {title}: duration {r.duration}s not in [{min_sec},{max_sec}]"
        )
        assert len(r.payload()) >= 500_000, f"{artist} - {title}: file too small"
        low = (r.path.name + " " + (r.title or "")).lower()
        for bad in ("live", "snippet", "how ", "remix", "instrumental", "slowed"):
            assert bad not in low, f"{artist} - {title}: junk in name {low!r}"
        print(f"OK {artist} - {title}: {r.duration}s, {len(r.payload())} bytes")
    finally:
        cleanup_download(r.path)


async def _live_suite() -> None:
    # сложный свежий релиз (часто без Topic / official audio)
    await _live_one("Ken Carson", "wheredoistart", min_sec=140, max_sec=190)
    # обычный старый трек того же артиста — должен стабильно находиться
    await _live_one("Ken Carson", "Yale", min_sec=90, max_sec=150)
    # другой артист / другой язык
    await _live_one("Markul", "Baddie", min_sec=120, max_sec=280)
    dur = await dl._lookup_catalog_duration("Ken Carson", "wheredoistart")
    assert dur and 150 <= dur <= 180, dur
    print(f"catalog duration ok: {dur}s")


def test_catalog_duration_lookup() -> None:
    dur = asyncio.run(dl._lookup_catalog_duration("Ken Carson", "wheredoistart"))
    assert dur is not None
    assert 150 <= dur <= 180, dur


def test_catalog_duration_yale_not_slowed_impostor() -> None:
    """Короткий title + чужой артист с похожим именем не должен давать 236с."""
    dur = asyncio.run(dl._lookup_catalog_duration("Ken Carson", "Yale"))
    # либо точная длина Yale (~105–110), либо None (тогда качаем без якоря)
    assert dur is None or 90 <= dur <= 130, dur


def test_itunes_artist_scoped_yale_and_xperiment() -> None:
    """iTunes search «Ken Carson Yale» врёт; lookup по artistId — нет."""
    yale = asyncio.run(dl._itunes_catalog_duration("Ken Carson", "Yale"))
    assert yale is not None and 100 <= yale <= 115, yale
    where = asyncio.run(dl._itunes_catalog_duration("Ken Carson", "wheredoistart"))
    assert where is not None and 150 <= where <= 180, where


if __name__ == "__main__":
    # unit
    test_reject_junk_titles()
    test_duration_vs_catalog()
    test_youtube_score_rejects_live_and_howto()
    test_soundcloud_score_rejects_teaser_and_slowed()
    test_ytmusic_score_yale()
    test_pick_closest_duration()
    test_artist_name_ok_no_partial()
    print("unit OK")
    if "--live" in sys.argv:
        asyncio.run(_live_suite())
        print("live OK")
    else:
        async def _catalog_suite() -> None:
            dur = await dl._lookup_catalog_duration("Ken Carson", "wheredoistart")
            assert dur is not None and 150 <= dur <= 180, dur
            yale = await dl._lookup_catalog_duration("Ken Carson", "Yale")
            assert yale is None or 90 <= yale <= 130, yale
            iy = await dl._itunes_catalog_duration("Ken Carson", "Yale")
            assert iy is not None and 100 <= iy <= 115, iy
            iw = await dl._itunes_catalog_duration("Ken Carson", "wheredoistart")
            assert iw is not None and 150 <= iw <= 180, iw
            url = await dl._resolve_best_ytmusic(
                artist="Ken Carson", title="Yale", expected=106
            )
            assert "NaEl1gDI124" in url or "watch?v=" in url, url
            print("ytmusic Yale OK:", url)
            print("itunes Yale/wheredoistart OK:", iy, iw)

        asyncio.run(_catalog_suite())
        print("catalog OK (run with --live for full downloads)")
