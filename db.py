"""Единый путь к SQLite — на Bothost должен жить в /app/data, не в коде."""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

_DB: Path | None = None


def resolve_playlist_db() -> Path:
    """
    Приоритет:
      1) PLAYLIST_DB / DB_PATH env
      2) $DATA_DIR/playlist.db или /app/data/playlist.db (персистентно)
      3) ./data/playlist.db
      4) рядом с кодом (legacy)
    Если в /app/data пусто, а рядом с кодом есть старая БД — копируем один раз.
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

    # миграция: старая БД в корне репо → persistent
    if (
        chosen != code_db
        and code_db.is_file()
        and code_db.stat().st_size > 0
        and (not chosen.is_file() or chosen.stat().st_size < 1000)
    ):
        try:
            shutil.copy2(code_db, chosen)
            logger.info("Migrated playlist.db %s → %s", code_db, chosen)
        except OSError as exc:
            logger.warning("DB migrate failed: %s", exc)

    _DB = chosen
    logger.info("SQLite DB: %s", _DB)
    return _DB
