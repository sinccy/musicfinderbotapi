"""
Сезонная рефералка:
  • deep-link t.me/bot?start=ref_<user_id>
  • реферал квалифицируется только после реальной активности
  • в конце сезона — топ пригласивших → NFT-подарок Telegram (вручную админом)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config import (
    BOT_USERNAME,
    REF_ADMIN_IDS,
    REF_MIN_ACTIONS,
    REF_MIN_ACTIVE_DAYS,
    REF_SEASON_END,
    REF_SEASON_NAME,
    REF_SEASON_START,
    REF_WINNERS_COUNT,
)

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).resolve().parent / "playlist.db"


@dataclass(frozen=True)
class Season:
    id: int
    name: str
    starts_at: str
    ends_at: str
    status: str  # open | closed
    winners_count: int
    gift_note: str


@dataclass(frozen=True)
class ReferralRow:
    id: int
    season_id: int
    referrer_id: int
    referee_id: int
    status: str  # pending | qualified | rejected
    created_at: str
    qualified_at: str
    reject_reason: str


@dataclass(frozen=True)
class LeaderboardEntry:
    referrer_id: int
    qualified: int
    pending: int


@dataclass(frozen=True)
class PrizeRow:
    season_id: int
    user_id: int
    rank: int
    qualified_refs: int
    status: str  # pending | notified | sent
    note: str


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
        try:
            dt = datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _init_sync() -> None:
    with _connect() as conn:
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                winners_count INTEGER NOT NULL DEFAULT 10,
                gift_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                referrer_id INTEGER NOT NULL,
                referee_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                qualified_at TEXT NOT NULL DEFAULT '',
                reject_reason TEXT NOT NULL DEFAULT '',
                UNIQUE(season_id, referee_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_ref_referrer "
            "ON referrals(season_id, referrer_id, status)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_prizes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                qualified_refs INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(season_id, user_id)
            )
            """
        )
        conn.commit()
    _ensure_default_season_sync()


def init_referral_db() -> None:
    _init_sync()
    logger.info(
        "Referral DB ready: season=%s min_days=%s min_actions=%s winners=%s",
        REF_SEASON_NAME,
        REF_MIN_ACTIVE_DAYS,
        REF_MIN_ACTIONS,
        REF_WINNERS_COUNT,
    )


def _ensure_default_season_sync() -> None:
    """Создаёт первый сезон из env только если таблицы сезонов пусты.

    Если сезон уже закрывали — новый НЕ поднимаем автоматически
    (иначе /refseason close бессмысленен).
    """
    with _connect() as conn:
        open_row = conn.execute(
            "SELECT id FROM referral_seasons WHERE status = 'open' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if open_row:
            return
        any_row = conn.execute(
            "SELECT id FROM referral_seasons LIMIT 1"
        ).fetchone()
        if any_row:
            return
        start = _parse_iso(REF_SEASON_START) or _now()
        end = _parse_iso(REF_SEASON_END) or (start + timedelta(days=90))
        if end <= start:
            end = start + timedelta(days=90)
        conn.execute(
            """
            INSERT INTO referral_seasons
                (name, starts_at, ends_at, status, winners_count, gift_note, created_at)
            VALUES (?, ?, ?, 'open', ?, ?, ?)
            """,
            (
                REF_SEASON_NAME or "Season 1",
                start.isoformat(),
                end.isoformat(),
                max(1, int(REF_WINNERS_COUNT)),
                "Telegram NFT gift",
                _now_iso(),
            ),
        )
        conn.commit()
        logger.info(
            "Created referral season %s (%s → %s)",
            REF_SEASON_NAME,
            start.date(),
            end.date(),
        )


def _row_season(r: sqlite3.Row) -> Season:
    return Season(
        id=int(r["id"]),
        name=r["name"] or "",
        starts_at=r["starts_at"] or "",
        ends_at=r["ends_at"] or "",
        status=r["status"] or "closed",
        winners_count=int(r["winners_count"] or 10),
        gift_note=r["gift_note"] or "",
    )


def _get_open_season_sync() -> Optional[Season]:
    _init_sync()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM referral_seasons WHERE status = 'open' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return _row_season(row) if row else None


def _season_is_live(season: Season, now: Optional[datetime] = None) -> bool:
    if season.status != "open":
        return False
    now = now or _now()
    start = _parse_iso(season.starts_at)
    end = _parse_iso(season.ends_at)
    if start and now < start:
        return False
    if end and now > end:
        return False
    return True


def _touch_user_sync(
    user_id: int,
    *,
    username: str = "",
    is_action: bool = False,
) -> None:
    if not user_id:
        return
    _init_sync()
    now = _now_iso()
    with _connect() as conn:
        row = conn.execute(
            "SELECT user_id, action_count, first_action_at FROM bot_users "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO bot_users
                    (user_id, username, first_seen_at, last_active_at,
                     action_count, first_action_at, last_action_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    (username or "")[:64],
                    now,
                    now,
                    1 if is_action else 0,
                    now if is_action else "",
                    now if is_action else "",
                ),
            )
        else:
            if is_action:
                first_action = row["first_action_at"] or now
                conn.execute(
                    """
                    UPDATE bot_users
                    SET username = CASE WHEN ? != '' THEN ? ELSE username END,
                        last_active_at = ?,
                        action_count = action_count + 1,
                        first_action_at = CASE
                            WHEN first_action_at = '' OR first_action_at IS NULL
                            THEN ? ELSE first_action_at END,
                        last_action_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        username or "",
                        (username or "")[:64],
                        now,
                        first_action,
                        now,
                        user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    UPDATE bot_users
                    SET username = CASE WHEN ? != '' THEN ? ELSE username END,
                        last_active_at = ?
                    WHERE user_id = ?
                    """,
                    (username or "", (username or "")[:64], now, user_id),
                )
        conn.commit()


def _attach_referral_sync(referrer_id: int, referee_id: int) -> tuple[str, str]:
    """
    Returns (status, detail_key):
      ok_pending | already | self | no_season | season_closed | exists_other
    """
    if not referrer_id or not referee_id:
        return "rejected", "bad_ids"
    if referrer_id == referee_id:
        return "rejected", "self"
    season = _get_open_season_sync()
    if not season:
        return "rejected", "no_season"
    if not _season_is_live(season):
        return "rejected", "season_closed"

    _touch_user_sync(referee_id)
    _touch_user_sync(referrer_id)

    with _connect() as conn:
        existing = conn.execute(
            "SELECT referrer_id, status FROM referrals "
            "WHERE season_id = ? AND referee_id = ?",
            (season.id, referee_id),
        ).fetchone()
        if existing:
            if int(existing["referrer_id"]) == referrer_id:
                return "already", existing["status"] or "pending"
            return "rejected", "exists_other"

        # Уже был в боте раньше (first_seen > 1h ago) — ок, но всё равно pending
        # до активности. Новым «пустышкам» не даём сразу qualified.
        conn.execute(
            """
            INSERT INTO referrals
                (season_id, referrer_id, referee_id, status, created_at)
            VALUES (?, ?, ?, 'pending', ?)
            """,
            (season.id, referrer_id, referee_id, _now_iso()),
        )
        conn.commit()
    return "ok_pending", "pending"


def _qualify_sync(referee_id: int) -> bool:
    """Проверяет pending-реферала и при выполнении правил → qualified."""
    if not referee_id:
        return False
    season = _get_open_season_sync()
    if not season or not _season_is_live(season):
        return False

    with _connect() as conn:
        ref = conn.execute(
            "SELECT * FROM referrals WHERE season_id = ? AND referee_id = ? "
            "AND status = 'pending'",
            (season.id, referee_id),
        ).fetchone()
        if not ref:
            return False
        user = conn.execute(
            "SELECT * FROM bot_users WHERE user_id = ?",
            (referee_id,),
        ).fetchone()
        if not user:
            return False

        actions = int(user["action_count"] or 0)
        if actions < max(1, int(REF_MIN_ACTIONS)):
            return False

        first_action = _parse_iso(user["first_action_at"] or "")
        last_action = _parse_iso(user["last_action_at"] or "")
        first_seen = _parse_iso(user["first_seen_at"] or "")
        created = _parse_iso(ref["created_at"] or "")

        # Активность должна быть «размазана» во времени — не за один вечер
        anchor = first_action or first_seen or created
        latest = last_action or _now()
        if not anchor:
            return False
        active_span = latest - anchor
        if active_span < timedelta(days=max(1, int(REF_MIN_ACTIVE_DAYS))):
            return False

        # Последнее действие не слишком старое относительно квалификации
        if (_now() - latest) > timedelta(days=max(3, int(REF_MIN_ACTIVE_DAYS))):
            return False

        now = _now_iso()
        conn.execute(
            """
            UPDATE referrals
            SET status = 'qualified', qualified_at = ?, reject_reason = ''
            WHERE id = ?
            """,
            (now, int(ref["id"])),
        )
        conn.commit()
        logger.info(
            "Referral qualified: referrer=%s referee=%s season=%s actions=%s",
            ref["referrer_id"],
            referee_id,
            season.id,
            actions,
        )
        return True


def _stats_for_user_sync(user_id: int, season_id: int) -> dict[str, int]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM referrals "
            "WHERE season_id = ? AND referrer_id = ? GROUP BY status",
            (season_id, user_id),
        ).fetchall()
    out = {"pending": 0, "qualified": 0, "rejected": 0, "total": 0}
    for r in rows:
        st = r["status"] or ""
        c = int(r["c"] or 0)
        if st in out:
            out[st] = c
        out["total"] += c
    return out


def _leaderboard_sync(season_id: int, limit: int = 10) -> list[LeaderboardEntry]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT referrer_id,
                   SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) AS q,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS p
            FROM referrals
            WHERE season_id = ?
            GROUP BY referrer_id
            HAVING q > 0 OR p > 0
            ORDER BY q DESC, p DESC, referrer_id ASC
            LIMIT ?
            """,
            (season_id, limit),
        ).fetchall()
    return [
        LeaderboardEntry(
            referrer_id=int(r["referrer_id"]),
            qualified=int(r["q"] or 0),
            pending=int(r["p"] or 0),
        )
        for r in rows
    ]


def _close_season_sync(season_id: int) -> list[PrizeRow]:
    _init_sync()
    with _connect() as conn:
        season_row = conn.execute(
            "SELECT * FROM referral_seasons WHERE id = ?",
            (season_id,),
        ).fetchone()
        if not season_row:
            return []
        season = _row_season(season_row)
        if season.status == "closed":
            existing = conn.execute(
                "SELECT * FROM referral_prizes WHERE season_id = ? "
                "ORDER BY rank ASC",
                (season_id,),
            ).fetchall()
            return [
                PrizeRow(
                    season_id=season_id,
                    user_id=int(r["user_id"]),
                    rank=int(r["rank"]),
                    qualified_refs=int(r["qualified_refs"] or 0),
                    status=r["status"] or "pending",
                    note=r["note"] or "",
                )
                for r in existing
            ]

        board = conn.execute(
            """
            SELECT referrer_id AS uid,
                   SUM(CASE WHEN status = 'qualified' THEN 1 ELSE 0 END) AS q
            FROM referrals
            WHERE season_id = ?
            GROUP BY referrer_id
            HAVING q > 0
            ORDER BY q DESC, uid ASC
            LIMIT ?
            """,
            (season_id, max(1, season.winners_count)),
        ).fetchall()

        now = _now_iso()
        prizes: list[PrizeRow] = []
        for i, r in enumerate(board, start=1):
            uid = int(r["uid"])
            q = int(r["q"] or 0)
            conn.execute(
                """
                INSERT INTO referral_prizes
                    (season_id, user_id, rank, qualified_refs, status, note, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(season_id, user_id) DO UPDATE SET
                    rank = excluded.rank,
                    qualified_refs = excluded.qualified_refs
                """,
                (season_id, uid, i, q, season.gift_note or "Telegram NFT gift", now),
            )
            prizes.append(
                PrizeRow(
                    season_id=season_id,
                    user_id=uid,
                    rank=i,
                    qualified_refs=q,
                    status="pending",
                    note=season.gift_note or "Telegram NFT gift",
                )
            )
        conn.execute(
            "UPDATE referral_seasons SET status = 'closed' WHERE id = ?",
            (season_id,),
        )
        conn.commit()
    return prizes


def _mark_prize_sent_sync(season_id: int, user_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE referral_prizes SET status = 'sent' "
            "WHERE season_id = ? AND user_id = ?",
            (season_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def referral_link(user_id: int) -> str:
    uname = (BOT_USERNAME or "").lstrip("@")
    if not uname:
        return f"ref_{user_id}"
    return f"https://t.me/{uname}?start=ref_{user_id}"


def is_ref_admin(user_id: int) -> bool:
    return bool(user_id) and user_id in REF_ADMIN_IDS


def parse_ref_arg(args: str) -> Optional[int]:
    raw = (args or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("ref_"):
        raw = raw[4:]
    if not raw.isdigit():
        return None
    return int(raw)


# ----- async API -----


async def touch_user(
    user_id: int, *, username: str = "", is_action: bool = False
) -> None:
    await asyncio.to_thread(
        _touch_user_sync, user_id, username=username, is_action=is_action
    )
    if is_action:
        await asyncio.to_thread(_qualify_sync, user_id)


async def attach_referral(referrer_id: int, referee_id: int) -> tuple[str, str]:
    return await asyncio.to_thread(_attach_referral_sync, referrer_id, referee_id)


async def get_open_season() -> Optional[Season]:
    return await asyncio.to_thread(_get_open_season_sync)


async def referral_stats(user_id: int) -> tuple[Optional[Season], dict[str, int]]:
    season = await get_open_season()
    if not season:
        return None, {"pending": 0, "qualified": 0, "rejected": 0, "total": 0}
    stats = await asyncio.to_thread(_stats_for_user_sync, user_id, season.id)
    return season, stats


async def leaderboard(limit: int = 10) -> tuple[Optional[Season], list[LeaderboardEntry]]:
    season = await get_open_season()
    if not season:
        return None, []
    board = await asyncio.to_thread(_leaderboard_sync, season.id, limit)
    return season, board


async def close_current_season() -> tuple[Optional[Season], list[PrizeRow]]:
    season = await get_open_season()
    if not season:
        return None, []
    prizes = await asyncio.to_thread(_close_season_sync, season.id)
    closed = Season(
        id=season.id,
        name=season.name,
        starts_at=season.starts_at,
        ends_at=season.ends_at,
        status="closed",
        winners_count=season.winners_count,
        gift_note=season.gift_note,
    )
    return closed, prizes


async def mark_prize_sent(user_id: int) -> bool:
    """Пометить NFT отправленным для победителя последнего закрытого сезона."""
    def _run() -> bool:
        _init_sync()
        with _connect() as conn:
            row = conn.execute(
                "SELECT id FROM referral_seasons WHERE status = 'closed' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return False
            return _mark_prize_sent_sync(int(row["id"]), user_id)

    return await asyncio.to_thread(_run)


def format_season_dates(season: Season) -> str:
    start = _parse_iso(season.starts_at)
    end = _parse_iso(season.ends_at)
    s = start.strftime("%d.%m.%Y") if start else season.starts_at[:10]
    e = end.strftime("%d.%m.%Y") if end else season.ends_at[:10]
    return f"{s} — {e}"
