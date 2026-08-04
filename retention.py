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
from i18n import DEFAULT_LANG, normalize_lang, t
from playlists import get_user_language
from referrals import (
    format_season_dates,
    get_open_season,
    referral_link,
    referral_stats,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "playlist.db"


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
    logger.info("Retention columns ready")


def _candidates_sync(limit: int) -> list[int]:
    init_retention_db()
    now = _now()
    min_idle = now - timedelta(hours=max(1, int(RETENTION_INACTIVE_HOURS)))
    max_idle = now - timedelta(days=max(1, int(RETENTION_MAX_IDLE_DAYS)))
    min_gap = now - timedelta(hours=max(1, int(RETENTION_INTERVAL_HOURS)))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT user_id, last_active_at, last_reminder_at, reminders_enabled
            FROM bot_users
            WHERE COALESCE(reminders_enabled, 1) = 1
            ORDER BY last_active_at DESC
            LIMIT ?
            """,
            (max(limit * 5, 200),),
        ).fetchall()
    out: list[int] = []
    for r in rows:
        uid = int(r["user_id"])
        last_active = _parse_iso(r["last_active_at"] or "")
        last_rem = _parse_iso(r["last_reminder_at"] or "")
        if not last_active:
            continue
        # слишком свежий — не трогаем
        if last_active > min_idle:
            continue
        # совсем пропал — не спамим
        if last_active < max_idle:
            continue
        if last_rem and last_rem > min_gap:
            continue
        out.append(uid)
        if len(out) >= limit:
            break
    return out


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
    bot: Bot, *, force_user_ids: Optional[list[int]] = None
) -> dict[str, int]:
    """Одна волна напоминаний. Возвращает счётчики."""
    if not RETENTION_REMINDERS and not force_user_ids:
        return {"sent": 0, "skip": 0, "fail": 0, "blocked": 0}

    ids = force_user_ids or await asyncio.to_thread(
        _candidates_sync, max(1, int(RETENTION_MAX_PER_RUN))
    )
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
        "retention batch sent=%s blocked=%s fail=%s candidates=%s",
        sent,
        blocked,
        fail,
        len(ids),
    )
    return {
        "sent": sent,
        "skip": skip,
        "fail": fail,
        "blocked": blocked,
        "candidates": len(ids),
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
