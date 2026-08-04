"""
Удержание: мягкие напоминания неактивным юзерам (~раз в 10 часов).

Правила антиспама:
  • только если юзер молчит ≥ RETENTION_INACTIVE_HOURS
  • не чаще чем раз в RETENTION_INTERVAL_HOURS
  • не пишем «мёртвым» > RETENTION_MAX_IDLE_DAYS
  • можно выключить: RETENTION_REMINDERS=0 или кнопка «Не напоминать»
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    RETENTION_BATCH_PAUSE_MS,
    RETENTION_INACTIVE_HOURS,
    RETENTION_INTERVAL_HOURS,
    RETENTION_MAX_IDLE_DAYS,
    RETENTION_MAX_PER_RUN,
    RETENTION_REMINDERS,
)
from db import iter_user_ids_from_db_file, legacy_playlist_db_paths, resolve_playlist_db
from i18n import DEFAULT_LANG, normalize_lang, t
from playlists import get_user_language
from referrals import (
    format_season_dates,
    get_open_season,
    referral_link,
    referral_stats,
)

logger = logging.getLogger(__name__)

DB_PATH = resolve_playlist_db()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(value: str) -> Optional[datetime]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def init_retention_db() -> None:
    with _connect() as conn:
        # bot_users могла ещё не создаться, если referrals.init не вызвали
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                action_count INTEGER NOT NULL DEFAULT 0,
                first_action_at TEXT NOT NULL DEFAULT '',
                last_action_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info(bot_users)").fetchall()
        }
        if "last_reminder_at" not in cols:
            conn.execute(
                "ALTER TABLE bot_users "
                "ADD COLUMN last_reminder_at TEXT NOT NULL DEFAULT ''"
            )
        if "reminders_enabled" not in cols:
            conn.execute(
                "ALTER TABLE bot_users "
                "ADD COLUMN reminders_enabled INTEGER NOT NULL DEFAULT 1"
            )
        conn.commit()
    n = _backfill_bot_users_sync()
    n2 = _merge_legacy_user_ids_sync()
    logger.info(
        "Retention ready db=%s backfilled=%s legacy_merged=%s",
        DB_PATH,
        n,
        n2,
    )


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone()
    return bool(row)


def _insert_user_ids(conn: sqlite3.Connection, ids: set[int]) -> int:
    now = _now_iso()
    added = 0
    for uid in ids:
        if not uid:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO bot_users
                (user_id, username, first_seen_at, last_active_at,
                 action_count, first_action_at, last_action_at,
                 last_reminder_at, reminders_enabled)
            VALUES (?, '', ?, ?, 0, '', '', '', 1)
            """,
            (uid, now, now),
        )
        added += int(cur.rowcount or 0)
    return added


def _backfill_bot_users_sync() -> int:
    """Подтянуть user_id из истории скачиваний/поиска/prefs/рефералок."""
    added = 0
    with _connect() as conn:
        sources: list[str] = []
        if _table_exists(conn, "recent_downloads"):
            sources.append("SELECT DISTINCT user_id FROM recent_downloads")
        if _table_exists(conn, "search_history"):
            sources.append("SELECT DISTINCT user_id FROM search_history")
        if _table_exists(conn, "user_prefs"):
            sources.append("SELECT DISTINCT user_id FROM user_prefs")
        if _table_exists(conn, "user_playlists"):
            sources.append("SELECT DISTINCT user_id FROM user_playlists")
        if _table_exists(conn, "referrals"):
            sources.append("SELECT DISTINCT referrer_id AS user_id FROM referrals")
            sources.append("SELECT DISTINCT referee_id AS user_id FROM referrals")
        if not sources:
            return 0
        union = " UNION ".join(sources)
        rows = conn.execute(union).fetchall()
        ids = {int(r[0] or 0) for r in rows if r[0]}
        added = _insert_user_ids(conn, ids)
        conn.commit()
    return added


def _merge_legacy_user_ids_sync() -> int:
    """Достать user_id из старых playlist.db (если остались после деплоев)."""
    added = 0
    current = DB_PATH.resolve()
    all_ids: set[int] = set()
    for path in legacy_playlist_db_paths():
        try:
            if path.resolve() == current:
                continue
        except OSError:
            pass
        found = iter_user_ids_from_db_file(path)
        if found:
            logger.info("Legacy DB %s → %s user ids", path, len(found))
            all_ids |= found
    if not all_ids:
        return 0
    with _connect() as conn:
        added = _insert_user_ids(conn, all_ids)
        conn.commit()
    return added


def _candidates_sync(limit: int, *, force: bool = False) -> list[int]:
    """
    force=True (/remind now): ВСЕ с reminders_enabled=1 (без idle/gap фильтров).
    Обычный режим: молчит ≥INACTIVE_HOURS, не чаще INTERVAL, не старше MAX_IDLE_DAYS.
    """
    init_retention_db()
    now = _now()
    min_idle = now - timedelta(hours=max(1, int(RETENTION_INACTIVE_HOURS)))
    max_idle = now - timedelta(days=max(1, int(RETENTION_MAX_IDLE_DAYS)))
    min_gap = now - timedelta(hours=max(1, int(RETENTION_INTERVAL_HOURS)))
    with _connect() as conn:
        if force:
            rows = conn.execute(
                """
                SELECT user_id, last_active_at, last_reminder_at
                FROM bot_users
                WHERE COALESCE(reminders_enabled, 1) = 1
                ORDER BY last_active_at DESC
                LIMIT ?
                """,
                (max(limit, 1),),
            ).fetchall()
            return [int(r["user_id"]) for r in rows]

        rows = conn.execute(
            """
            SELECT user_id, last_active_at, last_reminder_at, reminders_enabled
            FROM bot_users
            WHERE COALESCE(reminders_enabled, 1) = 1
            ORDER BY last_active_at DESC
            LIMIT ?
            """,
            (max(limit * 8, 400),),
        ).fetchall()
    out: list[int] = []
    for r in rows:
        uid = int(r["user_id"])
        last_active = _parse_iso(r["last_active_at"] or "")
        last_rem = _parse_iso(r["last_reminder_at"] or "")
        if not last_active:
            continue
        if last_active < max_idle:
            continue
        if last_active > min_idle:
            continue
        if last_rem and last_rem > min_gap:
            continue
        out.append(uid)
        if len(out) >= limit:
            break
    return out


def _users_count_sync() -> int:
    init_retention_db()
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM bot_users").fetchone()
    return int(row["c"] or 0) if row else 0


def _mark_reminded_sync(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE bot_users SET last_reminder_at = ? WHERE user_id = ?",
            (_now_iso(), user_id),
        )
        conn.commit()


def _disable_reminders_sync(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE bot_users SET reminders_enabled = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


def _enable_reminders_sync(user_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE bot_users SET reminders_enabled = 1 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()


async def disable_reminders(user_id: int) -> None:
    await asyncio.to_thread(_disable_reminders_sync, user_id)


async def enable_reminders(user_id: int) -> None:
    await asyncio.to_thread(_enable_reminders_sync, user_id)


def retention_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang_n = normalize_lang(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("ret_btn_search", lang_n),
                    callback_data="mode:text",
                ),
                InlineKeyboardButton(
                    text=t("ret_btn_ref", lang_n),
                    callback_data="ref:home",
                ),
            ],
        ]
    )


async def build_reminder_text(user_id: int) -> tuple[str, str]:
    """Возвращает (lang, html_text)."""
    saved = await get_user_language(user_id)
    lang = normalize_lang(saved or DEFAULT_LANG)
    season, _stats = await referral_stats(user_id)
    day = _now().timetuple().tm_yday
    variant = (user_id + day) % 3

    if season and season.status == "open":
        dates = format_season_dates(season)
        link = referral_link(user_id)
        winners = season.winners_count
        if lang == "en":
            # EN: музыкальные варианты + лёгкий сезон
            if variant == 0:
                text = t("ret_music", lang)
            elif variant == 1:
                text = t("ret_music_alt", lang)
            else:
                text = t(
                    "ret_season",
                    lang,
                    season=season.name,
                    dates=dates,
                    winners=winners,
                    link=link,
                )
        elif variant == 0:
            text = t("ret_music", lang)
        elif variant == 1:
            text = t(
                "ret_season",
                lang,
                season=season.name,
                dates=dates,
                winners=winners,
                link=link,
            )
        else:
            text = t(
                "ret_invite",
                lang,
                season=season.name,
                winners=winners,
                link=link,
            )
    else:
        text = t(
            "ret_music_alt" if lang == "en" and variant else "ret_music",
            lang,
        )
    return lang, text


async def send_retention_batch(
    bot: Bot,
    *,
    force_user_ids: Optional[list[int]] = None,
    force: bool = False,
) -> dict[str, int]:
    """Одна волна напоминаний. force=True — админский /remind now без idle-фильтров."""
    users_total = await asyncio.to_thread(_users_count_sync)
    if not RETENTION_REMINDERS and not force_user_ids and not force:
        return {
            "sent": 0,
            "skip": 0,
            "fail": 0,
            "blocked": 0,
            "candidates": 0,
            "users_total": users_total,
        }

    if force_user_ids is not None:
        ids = force_user_ids
    else:
        # force = вся аудитория из bot_users; auto = лимит за прогон
        lim = 50_000 if force else max(1, int(RETENTION_MAX_PER_RUN))
        ids = await asyncio.to_thread(_candidates_sync, lim, force=force)
    sent = skip = fail = blocked = 0
    pause = max(0, int(RETENTION_BATCH_PAUSE_MS)) / 1000.0

    for uid in ids:
        try:
            lang, text = await build_reminder_text(uid)
            await bot.send_message(
                uid,
                text,
                reply_markup=retention_kb(lang),
                disable_web_page_preview=True,
            )
            await asyncio.to_thread(_mark_reminded_sync, uid)
            sent += 1
        except TelegramForbiddenError:
            blocked += 1
            await asyncio.to_thread(_disable_reminders_sync, uid)
        except TelegramBadRequest as exc:
            fail += 1
            logger.debug("retention bad request %s: %s", uid, exc)
        except Exception as exc:  # noqa: BLE001
            fail += 1
            logger.warning("retention send %s: %s", uid, exc)
        if pause:
            await asyncio.sleep(pause)

    logger.info(
        "retention batch sent=%s blocked=%s fail=%s candidates=%s users_total=%s force=%s",
        sent,
        blocked,
        fail,
        len(ids),
        users_total,
        force,
    )
    return {
        "sent": sent,
        "skip": skip,
        "fail": fail,
        "blocked": blocked,
        "candidates": len(ids),
        "users_total": users_total,
    }


async def retention_loop(bot: Bot) -> None:
    """Фон: раз в RETENTION_INTERVAL_HOURS шлём волну."""
    if not RETENTION_REMINDERS:
        logger.info("Retention reminders disabled (RETENTION_REMINDERS=0)")
        return
    # первый прогон не сразу после рестарта — дать боту подняться
    await asyncio.sleep(min(600, max(60, int(RETENTION_INTERVAL_HOURS) * 60)))
    interval = max(1, int(RETENTION_INTERVAL_HOURS)) * 3600
    while True:
        try:
            await send_retention_batch(bot)
        except Exception as exc:  # noqa: BLE001
            logger.exception("retention loop: %s", exc)
        await asyncio.sleep(interval)
