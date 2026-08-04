"""Единый путь к SQLite — на Bothost должен жить в /app/data, не в коде."""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

_DB: Path | None = None


def legacy_playlist_db_paths() -> list[Path]:
    """Все известные места, где могла лежать старая БД."""
    code_dir = Path(__file__).resolve().parent
    data_dir = Path(os.environ.get("DATA_DIR") or "/app/data")
    paths = [
        code_dir / "playlist.db",
        Path("./playlist.db"),
        Path("./data") / "playlist.db",
        data_dir / "playlist.db.bak",
        code_dir / "playlist.db.bak",
    ]
    # уникальные существующие файлы
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        try:
            key = str(p.resolve()) if p.exists() else str(p)
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if p.is_file() and p.stat().st_size > 0:
            out.append(p)
    return out


def resolve_playlist_db() -> Path:
    """
    Приоритет:
      1) PLAYLIST_DB / DB_PATH env
      2) $DATA_DIR/playlist.db или /app/data/playlist.db (персистентно)
      3) ./data/playlist.db
      4) рядом с кодом (legacy)
    """
    global _DB
    if _DB is not None:
        return _DB

    env = (os.environ.get("PLAYLIST_DB") or os.environ.get("DB_PATH") or "").strip()
    if env:
        path = Path(env)
        path.parent.mkdir(parents=True, exist_ok=True)
        _DB = path
        logger.info("SQLite DB (env): %s", _DB)
        return _DB

    code_db = Path(__file__).resolve().parent / "playlist.db"
    candidates: list[Path] = []
    data_dir = Path(os.environ.get("DATA_DIR") or "/app/data")
    candidates.append(data_dir / "playlist.db")
    candidates.append(Path("./data") / "playlist.db")
    candidates.append(code_db)

    chosen: Path | None = None
    for c in candidates:
        try:
            c.parent.mkdir(parents=True, exist_ok=True)
            probe = c.parent / ".db_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            chosen = c
            break
        except OSError:
            continue
    if chosen is None:
        chosen = code_db

    # миграция: если persistent пустой/крошечный — копируем самую большую legacy БД
    if not chosen.is_file() or chosen.stat().st_size < 1000:
        legacy = legacy_playlist_db_paths()
        legacy = [p for p in legacy if p.resolve() != chosen.resolve()]
        if legacy:
            best = max(legacy, key=lambda p: p.stat().st_size)
            try:
                shutil.copy2(best, chosen)
                logger.info("Migrated playlist.db %s → %s", best, chosen)
            except OSError as exc:
                logger.warning("DB migrate failed: %s", exc)

    _DB = chosen
    logger.info("SQLite DB: %s", _DB)
    return _DB


def iter_user_ids_from_db_file(path: Path) -> set[int]:
    """Достать все user_id из любой старой/новой playlist.db."""
    ids: set[int] = set()
    if not path.is_file():
        return ids
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error as exc:
            logger.debug("open legacy db %s: %s", path, exc)
            return ids
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        queries: list[str] = []
        if "bot_users" in tables:
            queries.append("SELECT user_id FROM bot_users")
        if "recent_downloads" in tables:
            queries.append("SELECT DISTINCT user_id FROM recent_downloads")
        if "search_history" in tables:
            queries.append("SELECT DISTINCT user_id FROM search_history")
        if "user_prefs" in tables:
            queries.append("SELECT DISTINCT user_id FROM user_prefs")
        if "user_playlists" in tables:
            queries.append("SELECT DISTINCT user_id FROM user_playlists")
        if "referrals" in tables:
            queries.append("SELECT DISTINCT referrer_id FROM referrals")
            queries.append("SELECT DISTINCT referee_id FROM referrals")
        for q in queries:
            try:
                for row in conn.execute(q):
                    uid = int(row[0] or 0)
                    if uid:
                        ids.add(uid)
            except sqlite3.Error:
                continue
    finally:
        conn.close()
    return ids
