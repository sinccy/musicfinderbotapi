"""
Системный тест: альбом из каталога (через артиста) → YTM map → скачивание.

Как в боте: search_artists → albums_by_artist_id → lookup_album_details,
затем download_single_track(..., album=..., strict=True).

Запуск:
  .venv/bin/python test_album_ytm_path.py
  .venv/bin/python test_album_ytm_path.py --live
"""

from __future__ import annotations

import asyncio
import re
import sys

import download as dl
from music import (
    albums_by_artist_id,
    enrich_artists,
    lookup_album_details,
    pick_dominant_artist,
    search_artists,
)


async def _resolve_album(artist_q: str, album_q: str):
    raw = await search_artists(artist_q, limit=8, allow_spotify=False)
    assert raw, f"артист не найден: {artist_q}"
    # не тащим чужих (Lil Tecca для Ken Carson)
    from parser import artist_names_match

    named = [
        a for a in raw if artist_names_match(artist_q, a.get("artist_name") or "")
    ] or raw
    enriched = await enrich_artists(named)
    winner = pick_dominant_artist(enriched, query=artist_q) or enriched[0]
    assert artist_names_match(artist_q, winner.get("artist_name") or "") or (
        artist_q.casefold() in (winner.get("artist_name") or "").casefold()
    ), f"выбран чужой артист {winner.get('artist_name')!r} для {artist_q!r}"
    aid = str(winner.get("artistId") or winner.get("artist_id") or "")
    assert aid.isdigit(), f"нет artistId: {winner}"
    discog = await albums_by_artist_id(aid, country="us", limit=50)
    if not discog:
        discog = await albums_by_artist_id(aid, country="ru", limit=50)
    assert discog, f"пустая дискография {winner.get('artist_name')}"
    needle = album_q.casefold().strip()
    match = None
    for a in discog:
        cn = (a.collection_name or "").casefold().strip()
        cn_base = re.sub(r"\s*\(.*?\)\s*$", "", cn).strip()
        if cn == needle or cn_base == needle:
            match = a
            break
    if match is None and len(needle) >= 4:
        for a in discog:
            cn = (a.collection_name or "").casefold()
            if needle in cn:
                match = a
                break
    assert match is not None, (
        f"альбом {album_q!r} не найден у {winner.get('artist_name')}. "
        f"есть: {[a.collection_name for a in discog[:12]]}"
    )
    details = await lookup_album_details("itunes", match.source_id, country="us")
    return winner, details


async def test_ytm_map_covers_itunes_titles() -> None:
    winner, details = await _resolve_album("Nettspend", "HIM")
    print(
        f"artist={winner.get('artist_name')} score={winner.get('catalog_score')} "
        f"album={details.collection_name!r} tracks={len(details.tracks)}"
    )
    assert details.tracks, "пустой треклист"
    mapping = await dl._ytmusic_album_track_map(
        details.artist_name, details.collection_name
    )
    assert len(mapping) >= 20, f"YTM map слишком маленький: {len(mapping)}"
    missed: list[str] = []
    for t in details.tracks:
        name = t.track_name
        vid = dl._match_album_video_id(name, mapping)
        if not vid:
            missed.append(name)
        else:
            print(f"  map OK {name!r} → {vid}")
    assert not missed, f"нет videoId для: {missed}"
    print(f"OK map matched {len(details.tracks)} itunes tracks")


async def test_pick_dominant_nettspend() -> None:
    raw = await search_artists("Nettspend", limit=8, allow_spotify=False)
    enriched = await enrich_artists(raw)
    for r in enriched[:5]:
        print(
            " ",
            r.get("artist_name"),
            r.get("artist_id"),
            "score",
            r.get("catalog_score"),
            "albums",
            r.get("album_count"),
        )
    winner = pick_dominant_artist(enriched, query="Nettspend")
    assert winner is not None, "Nettspend должен автовыбираться без пикера"
    assert "nettspend" in (winner.get("artist_name") or "").casefold()
    print("OK pick_dominant", winner.get("artist_name"), winner.get("catalog_score"))

    raw_k = await search_artists("Ken Carson", limit=8, allow_spotify=False)
    assert all(
        "ken" in (a.get("artist_name") or "").casefold() for a in raw_k
    ), [a.get("artist_name") for a in raw_k]
    en_k = await enrich_artists(raw_k)
    w_k = pick_dominant_artist(en_k, query="Ken Carson")
    assert w_k is not None
    assert "ken carson" in (w_k.get("artist_name") or "").casefold().replace(
        "$", "s"
    )
    print(
        "OK Ken pick",
        w_k.get("artist_name"),
        w_k.get("artist_id"),
        w_k.get("catalog_score"),
    )

async def _live_album_tracks(
    artist: str,
    album: str,
    *,
    sample: int = 5,
) -> None:
    from download import cleanup_download, download_single_track

    _winner, details = await _resolve_album(artist, album)
    tracks = list(details.tracks)[:sample]
    assert tracks, "нет треков"

    mapping = await dl._ytmusic_album_track_map(
        details.artist_name, details.collection_name
    )
    print(
        f"LIVE {details.artist_name} — {details.collection_name} "
        f"({len(details.tracks)} tracks, YTM keys={len(mapping)})"
    )

    for t in tracks:
        name = t.track_name
        art = t.artist_name or details.artist_name
        exp = (t.duration_ms // 1000) if t.duration_ms else None
        expected_vid = dl._match_album_video_id(name, mapping)
        assert expected_vid, f"нет YTM id для {name!r}"

        r = await download_single_track(
            art,
            name,
            timeout=180,
            expected_duration=exp,
            album=details.collection_name,
            strict=True,
        )
        try:
            assert r.duration and r.duration >= 55, (name, r.duration)
            if exp:
                assert abs(r.duration - exp) <= max(18, int(exp * 0.18)), (
                    f"{name}: got {r.duration}s expected ~{exp}s"
                )
            blob = f"{r.title or ''} {r.path.name}".lower()
            for bad in ("live @", "snippet", "how ", "instrumental", "slowed"):
                assert bad not in blob, f"{name}: junk {blob!r}"
            assert len(r.payload()) >= 400_000, f"{name}: too small"
            print(
                f"  OK {name!r}: {r.duration}s (exp={exp}) "
                f"ytm={expected_vid} bytes={len(r.payload())}"
            )
        finally:
            cleanup_download(r.path)


if __name__ == "__main__":
    asyncio.run(test_pick_dominant_nettspend())
    asyncio.run(test_ytm_map_covers_itunes_titles())
    print("map OK")
    if "--live" in sys.argv:
        asyncio.run(_live_album_tracks("Nettspend", "HIM", sample=6))
        asyncio.run(_live_album_tracks("Ken Carson", "A Great Chaos", sample=3))
        asyncio.run(_live_album_tracks("xaviersobased", "Keeping tabs", sample=2))
        print("live album OK")
