"""
Локальные плейлисты (SQLite):
  • недавно скачанные (file_id)
  • пользовательские плейлисты из скачанных треков
  • история поиска → рекомендации
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from db import resolve_playlist_db

logger = logging.getLogger(__name__)

DB_PATH = resolve_playlist_db()
MAX_TRACKS_PER_USER = 80
MAX_PLAYLISTS_PER_USER = 20
MAX_TRACKS_PER_PLAYLIST = 100
MAX_SEARCH_HISTORY = 80


@dataclass(frozen=True)
class PlaylistTrack:
    id: int
    user_id: int
    track_name: str
    artist: str
    file_id: str
    downloaded_at: str


@dataclass(frozen=True)
class UserPlaylist:
    id: int
    user_id: int
    name: str
    created_at: str
    track_count: int = 0


@dataclass(frozen=True)
class SearchHistoryItem:
    id: int
    user_id: int
    artist: str
    title: str
    kind: str  # track | album | artist | query
    created_at: str


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _init_sync() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS recent_downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                track_name TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL,
                downloaded_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_recent_user "
            "ON recent_downloads(user_id, downloaded_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_upl_user "
            "ON user_playlists(user_id, created_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_playlist_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                track_name TEXT NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                file_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                recent_id INTEGER,
                UNIQUE(playlist_id, file_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_upt_pl "
            "ON user_playlist_tracks(playlist_id, added_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                artist TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'query',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sh_user "
            "ON search_history(user_id, created_at DESC)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def init_playlist_db() -> None:
    _init_sync()
    logger.info("Playlist DB ready: %s", DB_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _save_sync(user_id: int, track_name: str, artist: str, file_id: str) -> int:
    _init_sync()
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO recent_downloads
                (user_id, track_name, artist, file_id, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, track_name[:200], artist[:200], file_id, now),
        )
        new_id = int(cur.lastrowid)
        conn.execute(
            """
            DELETE FROM recent_downloads
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM recent_downloads
                WHERE user_id = ?
                ORDER BY downloaded_at DESC, id DESC
                LIMIT ?
              )
            """,
            (user_id, user_id, MAX_TRACKS_PER_USER),
        )
        conn.commit()
        return new_id


def _list_sync(user_id: int, *, offset: int, limit: int) -> list[PlaylistTrack]:
    _init_sync()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, track_name, artist, file_id, downloaded_at
            FROM recent_downloads
            WHERE user_id = ?
            ORDER BY downloaded_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ).fetchall()
    return [
        PlaylistTrack(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            track_name=r["track_name"] or "",
            artist=r["artist"] or "",
            file_id=r["file_id"] or "",
            downloaded_at=r["downloaded_at"] or "",
        )
        for r in rows
    ]


def _count_sync(user_id: int) -> int:
    _init_sync()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM recent_downloads WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return int(row["c"] if row else 0)


def _delete_sync(user_id: int, track_id: int) -> bool:
    _init_sync()
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM recent_downloads WHERE id = ? AND user_id = ?",
            (track_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _get_sync(user_id: int, track_id: int) -> Optional[PlaylistTrack]:
    _init_sync()
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT id, user_id, track_name, artist, file_id, downloaded_at
            FROM recent_downloads
            WHERE id = ? AND user_id = ?
            """,
            (track_id, user_id),
        ).fetchone()
    if not r:
        return None
    return PlaylistTrack(
        id=int(r["id"]),
        user_id=int(r["user_id"]),
        track_name=r["track_name"] or "",
        artist=r["artist"] or "",
        file_id=r["file_id"] or "",
        downloaded_at=r["downloaded_at"] or "",
    )


# --- user playlists ---


def _create_playlist_sync(user_id: int, name: str) -> Optional[UserPlaylist]:
    _init_sync()
    name = (name or "").strip()[:64]
    if not name:
        return None
    with _connect() as conn:
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM user_playlists WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if int(cnt["c"] if cnt else 0) >= MAX_PLAYLISTS_PER_USER:
            return None
        now = _now()
        cur = conn.execute(
            "INSERT INTO user_playlists (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, now),
        )
        conn.commit()
        return UserPlaylist(
            id=int(cur.lastrowid),
            user_id=user_id,
            name=name,
            created_at=now,
            track_count=0,
        )


def _list_playlists_sync(user_id: int) -> list[UserPlaylist]:
    _init_sync()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.user_id, p.name, p.created_at,
                   COUNT(t.id) AS track_count
            FROM user_playlists p
            LEFT JOIN user_playlist_tracks t ON t.playlist_id = p.id
            WHERE p.user_id = ?
            GROUP BY p.id
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        UserPlaylist(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            name=r["name"] or "",
            created_at=r["created_at"] or "",
            track_count=int(r["track_count"] or 0),
        )
        for r in rows
    ]


def _get_playlist_sync(user_id: int, playlist_id: int) -> Optional[UserPlaylist]:
    _init_sync()
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT p.id, p.user_id, p.name, p.created_at,
                   COUNT(t.id) AS track_count
            FROM user_playlists p
            LEFT JOIN user_playlist_tracks t ON t.playlist_id = p.id
            WHERE p.id = ? AND p.user_id = ?
            GROUP BY p.id
            """,
            (playlist_id, user_id),
        ).fetchone()
    if not r:
        return None
    return UserPlaylist(
        id=int(r["id"]),
        user_id=int(r["user_id"]),
        name=r["name"] or "",
        created_at=r["created_at"] or "",
        track_count=int(r["track_count"] or 0),
    )


def _delete_playlist_sync(user_id: int, playlist_id: int) -> bool:
    _init_sync()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_playlist_tracks WHERE playlist_id = ? AND user_id = ?",
            (playlist_id, user_id),
        )
        cur = conn.execute(
            "DELETE FROM user_playlists WHERE id = ? AND user_id = ?",
            (playlist_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _rename_playlist_sync(user_id: int, playlist_id: int, name: str) -> bool:
    name = (name or "").strip()[:64]
    if not name:
        return False
    _init_sync()
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE user_playlists SET name = ? WHERE id = ? AND user_id = ?",
            (name, playlist_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _add_to_playlist_sync(
    user_id: int,
    playlist_id: int,
    *,
    track_name: str,
    artist: str,
    file_id: str,
    recent_id: Optional[int] = None,
) -> bool:
    if not file_id:
        return False
    _init_sync()
    with _connect() as conn:
        pl = conn.execute(
            "SELECT id FROM user_playlists WHERE id = ? AND user_id = ?",
            (playlist_id, user_id),
        ).fetchone()
        if not pl:
            return False
        cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM user_playlist_tracks WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        if int(cnt["c"] if cnt else 0) >= MAX_TRACKS_PER_PLAYLIST:
            return False
        try:
            conn.execute(
                """
                INSERT INTO user_playlist_tracks
                    (playlist_id, user_id, track_name, artist, file_id, added_at, recent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    playlist_id,
                    user_id,
                    (track_name or "Трек")[:200],
                    (artist or "")[:200],
                    file_id,
                    _now(),
                    recent_id,
                ),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def _add_recent_to_playlist_sync(
    user_id: int, playlist_id: int, recent_id: int
) -> bool:
    item = _get_sync(user_id, recent_id)
    if not item:
        return False
    return _add_to_playlist_sync(
        user_id,
        playlist_id,
        track_name=item.track_name,
        artist=item.artist,
        file_id=item.file_id,
        recent_id=item.id,
    )


def _list_playlist_tracks_sync(
    user_id: int, playlist_id: int, *, offset: int, limit: int
) -> list[PlaylistTrack]:
    _init_sync()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, track_name, artist, file_id, added_at AS downloaded_at
            FROM user_playlist_tracks
            WHERE playlist_id = ? AND user_id = ?
            ORDER BY added_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (playlist_id, user_id, limit, offset),
        ).fetchall()
    return [
        PlaylistTrack(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            track_name=r["track_name"] or "",
            artist=r["artist"] or "",
            file_id=r["file_id"] or "",
            downloaded_at=r["downloaded_at"] or "",
        )
        for r in rows
    ]


def _count_playlist_tracks_sync(user_id: int, playlist_id: int) -> int:
    _init_sync()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS c FROM user_playlist_tracks
            WHERE playlist_id = ? AND user_id = ?
            """,
            (playlist_id, user_id),
        ).fetchone()
    return int(row["c"] if row else 0)


def _remove_from_playlist_sync(
    user_id: int, playlist_id: int, track_row_id: int
) -> bool:
    _init_sync()
    with _connect() as conn:
        cur = conn.execute(
            """
            DELETE FROM user_playlist_tracks
            WHERE id = ? AND playlist_id = ? AND user_id = ?
            """,
            (track_row_id, playlist_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def _get_playlist_track_row_sync(
    user_id: int, track_row_id: int
) -> Optional[PlaylistTrack]:
    _init_sync()
    with _connect() as conn:
        r = conn.execute(
            """
            SELECT id, user_id, track_name, artist, file_id, added_at AS downloaded_at
            FROM user_playlist_tracks
            WHERE id = ? AND user_id = ?
            """,
            (track_row_id, user_id),
        ).fetchone()
    if not r:
        return None
    return PlaylistTrack(
        id=int(r["id"]),
        user_id=int(r["user_id"]),
        track_name=r["track_name"] or "",
        artist=r["artist"] or "",
        file_id=r["file_id"] or "",
        downloaded_at=r["downloaded_at"] or "",
    )


# --- search history / recommendations ---


def _record_search_sync(
    user_id: int, *, artist: str = "", title: str = "", kind: str = "query"
) -> None:
    artist = (artist or "").strip()[:200]
    title = (title or "").strip()[:200]
    if not artist and not title:
        return
    _init_sync()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO search_history (user_id, artist, title, kind, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, artist, title, (kind or "query")[:32], _now()),
        )
        conn.execute(
            """
            DELETE FROM search_history
            WHERE user_id = ?
              AND id NOT IN (
                SELECT id FROM search_history
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
              )
            """,
            (user_id, user_id, MAX_SEARCH_HISTORY),
        )
        conn.commit()


def _top_artists_sync(user_id: int, *, limit: int = 8) -> list[str]:
    _init_sync()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT artist FROM search_history
            WHERE user_id = ? AND artist != ''
            ORDER BY created_at DESC
            LIMIT 60
            """,
            (user_id,),
        ).fetchall()
        dl = conn.execute(
            """
            SELECT artist FROM recent_downloads
            WHERE user_id = ? AND artist != ''
            ORDER BY downloaded_at DESC
            LIMIT 40
            """,
            (user_id,),
        ).fetchall()
    counts: Counter[str] = Counter()
    for r in rows:
        name = (r["artist"] or "").strip()
        if name:
            counts[name] += 2
    for r in dl:
        name = (r["artist"] or "").strip()
        if name:
            counts[name] += 3
    return [a for a, _ in counts.most_common(limit)]


def _recent_titles_sync(user_id: int, *, limit: int = 12) -> list[tuple[str, str]]:
    _init_sync()
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT artist, title FROM search_history
            WHERE user_id = ? AND title != ''
            ORDER BY created_at DESC
            LIMIT 40
            """,
            (user_id,),
        ).fetchall()
    for r in rows:
        art = (r["artist"] or "").strip()
        tit = (r["title"] or "").strip()
        key = f"{art.casefold()}|{tit.casefold()}"
        if key in seen:
            continue
        seen.add(key)
        out.append((art, tit))
        if len(out) >= limit:
            break
    return out


# --- async API ---


async def save_to_playlist(
    user_id: int,
    track_name: str,
    artist: str,
    file_id: str,
) -> None:
    if not user_id or not file_id:
        return
    try:
        await asyncio.to_thread(
            _save_sync, user_id, track_name or "Трек", artist or "", file_id
        )
        logger.info(
            "Playlist save user=%s: %s – %s", user_id, artist, track_name
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("playlist save failed: %s", exc)


async def list_recent(
    user_id: int, *, page: int = 0, per_page: int = 10
) -> tuple[list[PlaylistTrack], int]:
    offset = max(0, page) * per_page
    tracks, total = await asyncio.gather(
        asyncio.to_thread(_list_sync, user_id, offset=offset, limit=per_page),
        asyncio.to_thread(_count_sync, user_id),
    )
    return tracks, total


async def delete_from_playlist(user_id: int, track_id: int) -> bool:
    return await asyncio.to_thread(_delete_sync, user_id, track_id)


async def get_playlist_track(
    user_id: int, track_id: int
) -> Optional[PlaylistTrack]:
    return await asyncio.to_thread(_get_sync, user_id, track_id)


async def create_user_playlist(user_id: int, name: str) -> Optional[UserPlaylist]:
    return await asyncio.to_thread(_create_playlist_sync, user_id, name)


async def list_user_playlists(user_id: int) -> list[UserPlaylist]:
    return await asyncio.to_thread(_list_playlists_sync, user_id)


async def get_user_playlist(
    user_id: int, playlist_id: int
) -> Optional[UserPlaylist]:
    return await asyncio.to_thread(_get_playlist_sync, user_id, playlist_id)


async def delete_user_playlist(user_id: int, playlist_id: int) -> bool:
    return await asyncio.to_thread(_delete_playlist_sync, user_id, playlist_id)


async def rename_user_playlist(
    user_id: int, playlist_id: int, name: str
) -> bool:
    return await asyncio.to_thread(
        _rename_playlist_sync, user_id, playlist_id, name
    )


async def add_recent_to_user_playlist(
    user_id: int, playlist_id: int, recent_id: int
) -> bool:
    return await asyncio.to_thread(
        _add_recent_to_playlist_sync, user_id, playlist_id, recent_id
    )


async def add_file_to_user_playlist(
    user_id: int,
    playlist_id: int,
    *,
    track_name: str,
    artist: str,
    file_id: str,
) -> bool:
    return await asyncio.to_thread(
        _add_to_playlist_sync,
        user_id,
        playlist_id,
        track_name=track_name,
        artist=artist,
        file_id=file_id,
    )


async def list_user_playlist_tracks(
    user_id: int, playlist_id: int, *, page: int = 0, per_page: int = 10
) -> tuple[list[PlaylistTrack], int]:
    offset = max(0, page) * per_page
    tracks, total = await asyncio.gather(
        asyncio.to_thread(
            _list_playlist_tracks_sync,
            user_id,
            playlist_id,
            offset=offset,
            limit=per_page,
        ),
        asyncio.to_thread(_count_playlist_tracks_sync, user_id, playlist_id),
    )
    return tracks, total


async def remove_from_user_playlist(
    user_id: int, playlist_id: int, track_row_id: int
) -> bool:
    return await asyncio.to_thread(
        _remove_from_playlist_sync, user_id, playlist_id, track_row_id
    )


async def get_user_playlist_track(
    user_id: int, track_row_id: int
) -> Optional[PlaylistTrack]:
    return await asyncio.to_thread(
        _get_playlist_track_row_sync, user_id, track_row_id
    )


async def record_search(
    user_id: int, *, artist: str = "", title: str = "", kind: str = "query"
) -> None:
    if not user_id:
        return
    try:
        await asyncio.to_thread(
            _record_search_sync, user_id, artist=artist, title=title, kind=kind
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("record_search: %s", exc)


async def recommendation_seeds(
    user_id: int,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Топ артистов + недавние (artist, title) для рекомендаций."""
    artists, titles = await asyncio.gather(
        asyncio.to_thread(_top_artists_sync, user_id, limit=6),
        asyncio.to_thread(_recent_titles_sync, user_id, limit=10),
    )
    return artists, titles


def _get_user_language_sync(user_id: int) -> Optional[str]:
    if not user_id:
        return None
    _init_sync()
    with _connect() as conn:
        row = conn.execute(
            "SELECT language FROM user_prefs WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    lang = (row["language"] or "").strip().lower()
    return lang or None


def _set_user_language_sync(user_id: int, language: str) -> str:
    _init_sync()
    lang = (language or "ru").strip().lower()
    if lang not in {"ru", "en"}:
        lang = "ru"
    now = _now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (user_id, language, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                language = excluded.language,
                updated_at = excluded.updated_at
            """,
            (user_id, lang, now),
        )
        conn.commit()
    return lang


async def get_user_language(user_id: int) -> Optional[str]:
    """Сохранённый язык пользователя или None, если ещё не выбирал."""
    return await asyncio.to_thread(_get_user_language_sync, user_id)


async def set_user_language(user_id: int, language: str) -> str:
    return await asyncio.to_thread(_set_user_language_sync, user_id, language)
