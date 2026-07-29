"""
Скачивание MP3: asyncio subprocess yt-dlp + разбор прогресса из stderr.

Порядок:
  0) Каталог длины: Apple/iTunes (по artistId) → Deezer → Яндекс
  1) YouTube Music (официальный song match по артисту/названию/длине)
  2) SoundCloud API
  3) YouTube search (строгий отбор)
Аудио из Apple/Spotify API не скачивается — только метаданные (длина/название).
Spotify API с 2026 требует Premium у владельца приложения — не обязателен.
Превью / лайвы / «how it was made» / обрезанные remaster — нет.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence
from urllib.parse import quote_plus

import aiohttp

from cache import get_session
from config import (
    DOWNLOAD_TIMEOUT,
    MAX_AUDIO_BYTES,
    YTDLP_COOKIES_FILE,
    YTDLP_COOKIES_FROM_BROWSER,
    YTMUSIC_LANGUAGE,
    YTMUSIC_LOCATION,
    YTMUSIC_PROXY,
)

logger = logging.getLogger(__name__)

AUDIO_CAPTION = (
    '<a href="https://t.me/projectcover_bot">❤️projectcover❤️</a>'
)

ProgressCallback = Callable[[int], Awaitable[None]]
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
_SAFE = re.compile(r"[^\w\s\-().]+", re.UNICODE)
_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")

# yt-dlp возвращает 101 при --max-downloads — это успех, если файл есть
_OK_CODES = {0, 101}

# SoundCloud search часто зависает без ответа — не ждём полный DOWNLOAD_TIMEOUT
_SOUNDCLOUD_TIMEOUT = 12.0
_SC_CLIENT_ID: Optional[str] = None
_SC_CLIENT_TS: float = 0.0

# Полный трек: короче ≈ превью iTunes / snippet
_MIN_FULL_TRACK_SEC = 55
_MAX_TRACK_SEC = 15 * 60

# Ремиксы / не те версии — понижаем, если их нет в запросе
_BAD_VERSION_RE = re.compile(
    r"(slowed|reverb|speed\s*up|sped\s*up|nightcore|8d\s*audio|"
    r"karaoke|instrumental|fan\s*made|ai\s*cover|cover\s*by|"
    r"mashup|bootleg|minus|минус|минусовка|\bremix\b|"
    r"\breverse(?:d)?\b|reversed\s*music|"
    r"\blive\b|live\s*(from|at|performance|session|version)|"
    r"concert|rolling\s*loud|festival|audio\s*only|"
    r"snippet|teaser|leak\b|type\s*beat|"
    r"how\s+.+\s+was\s+made|was\s+made|fl\s*studio|remade|recreate|"
    r"reaction|explained|tutorial|breakdown|remake\b)",
    re.IGNORECASE,
)

# Жёсткий мусор — даже не рассматриваем как кандидат
_JUNK_VIDEO_RE = re.compile(
    r"(how\s+.+\s+was\s+made|"
    r"was\s+made\b|"
    r"fl\s*studio|"
    r"\bremade\b|"
    r"\bremake\b|"
    r"\bremix\b|"
    r"\breverse(?:d)?\b|"
    r"type\s*beat|"
    r"\binstrumental\b|"
    r"\bsnippet\b|"
    r"\bteaser\b|"
    r"\breaction\b|"
    r"\btutorial\b|"
    r"\bbreakdown\b|"
    r"extended\s+snippet|"
    r"new\s+snippet|"
    r"\bcover\s*by\b|"
    r"ai\s*cover)",
    re.IGNORECASE,
)


class DownloadError(Exception):
    """Не удалось скачать трек."""

    def __init__(
        self,
        message: str,
        *,
        unavailable_free: bool = False,
        artist: str = "",
        title: str = "",
    ) -> None:
        super().__init__(message)
        self.unavailable_free = unavailable_free
        self.artist = (artist or "").strip()
        self.title = (title or "").strip()


def raise_unavailable_free(artist: str, title: str, *, extra: str = "") -> None:
    """Единая ошибка: трека нет в открытых источниках."""
    msg = (
        "Нет полной студийной версии в свободной прослушке "
        "(YouTube / SoundCloud) — только instrumental / reverse / remix "
        "или ничего."
    )
    if extra:
        msg = f"{msg}\n{extra}"
    raise DownloadError(
        msg,
        unavailable_free=True,
        artist=artist,
        title=title,
    )


# кэш доступности свободной загрузки: ключ → (ok, ts)
_FREE_AVAIL_CACHE: dict[str, tuple[bool, float]] = {}
_FREE_AVAIL_TTL = 6 * 3600


def _free_avail_key(artist: str, album: str, *, track: str = "") -> str:
    return f"{artist.casefold()}::{album.casefold()}::{track.casefold()}"


CLOSED_DOWNLOAD_NOTICE = (
    "⛔ <b>Закрытый доступ к загрузке</b>\n"
    "Полных студийных версий нет в свободной прослушке "
    "(YouTube / SoundCloud).\n"
    "Обложка и ссылки на альбом — ниже.\n"
    "Чтобы скачать: найдите ролик на YouTube и <b>пришлите ссылку</b> боту."
)


def _yt_video_likely_blocked(err: str) -> bool:
    e = (err or "").lower()
    # «sign in / bot» — проблема cookies/прокси, не значит что трек закрыт навсегда
    return any(
        s in e
        for s in (
            "claimed content",
            "blocked due to",
            "copyright",
            "video unavailable",
            "private video",
            "is not available",
        )
    )


def _yt_video_status_sync(video_id: str) -> str:
    """
    ok | blocked | unknown
    unknown = bot-check / сеть (скачивание через android+proxy часто всё равно работает).
    """
    import yt_dlp  # type: ignore

    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "http_headers": {"User-Agent": UA},
        "extractor_args": {"youtube": {"player_client": ["android", "web"]}},
    }
    proxy = (YTMUSIC_PROXY or "").strip()
    if proxy:
        opts["proxy"] = proxy
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return "ok" if info and (info.get("id") or info.get("title")) else "unknown"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        if _yt_video_likely_blocked(msg):
            return "blocked"
        return "unknown"


async def _yt_video_downloadable(video_id: str) -> bool:
    """True только если ролик явно доступен (не bot/copyright)."""
    video_id = (video_id or "").strip()
    if not video_id:
        return False
    return (await asyncio.to_thread(_yt_video_status_sync, video_id)) == "ok"


async def _yt_video_status(video_id: str) -> str:
    video_id = (video_id or "").strip()
    if not video_id:
        return "blocked"
    return await asyncio.to_thread(_yt_video_status_sync, video_id)


async def probe_track_has_free_source(
    artist: str,
    title: str,
    *,
    album: str = "",
    expected: Optional[int] = None,
) -> bool:
    """Быстрая проверка: есть ли URL на YTM/SC/YT (без скачивания файла)."""
    artist = (artist or "").strip()
    title = (title or "").strip()
    album = (album or "").strip()
    if not title:
        return False

    t_norm = _norm_match(title)
    short_ambiguous = len(t_norm.split()) == 1 and len(t_norm) <= 5

    try:
        if album and artist:
            vid = await _ytmusic_album_video_for_track(artist, album, title)
            if vid:
                status = await _yt_video_status(vid)
                # ok / unknown (bot) — кнопка открыта; blocked (UMG) — ищем замену
                if status in {"ok", "unknown"}:
                    return True

        # короткий title вроде «che» часто ловит чужой коллаб (BLEAU) —
        # для «свободной загрузки» это не считается, нужен YTM/альбом или
        # точное primary-имя в источнике.
        sc = await _resolve_best_soundcloud(
            artist=artist, title=title, expected=expected, album=album
        )
        if sc and not short_ambiguous:
            return True
        if sc and short_ambiguous:
            pass

        yt = await _resolve_best_youtube(
            artist=artist,
            title=title,
            query=f"{artist} {title}".strip(),
            expected=expected,
            album=album,
        )
        if yt and not short_ambiguous:
            from utils import extract_youtube_video_id

            yvid = extract_youtube_video_id(yt) or ""
            if not yvid:
                return True
            st = await _yt_video_status(yvid)
            if st in {"ok", "unknown"}:
                return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe_track_has_free_source: %s", exc)
    return False


async def probe_album_free_download(
    artist: str,
    album: str,
    tracks: Sequence[dict[str, Any]],
) -> bool:
    """
    Системно: можно ли свободно качать альбом.
    True = показываем «Скачать MP3».
    False = закрытый доступ (обложка/ссылки остаются).
    """
    import time

    artist = (artist or "").strip()
    album = (album or "").strip()
    if not artist or not album or not tracks:
        return False

    cache_key = _free_avail_key(artist, album)
    hit = _FREE_AVAIL_CACHE.get(cache_key)
    if hit and (time.time() - hit[1]) < _FREE_AVAIL_TTL:
        return hit[0]

    # 1) карта альбома на YouTube Music — главный сигнал
    try:
        mapping = await _ytmusic_album_track_map(artist, album)
    except Exception as exc:  # noqa: BLE001
        logger.debug("probe album ytm: %s", exc)
        mapping = {}
    names = [
        (t.get("name") or t.get("track_name") or "").strip()
        for t in tracks
        if (t.get("name") or t.get("track_name") or "").strip()
    ]
    if mapping and names:
        matched = sum(1 for n in names if _match_album_video_id(n, mapping))
        ratio = matched / max(len(names), 1)
        if ratio >= 0.45 or matched >= 5:
            # карта есть, но ролики могут быть UMG-blocked — проверим 2–3 шт.
            sample_vids: list[str] = []
            seen_v: set[str] = set()
            for n in names:
                vid = _match_album_video_id(n, mapping)
                if not vid or vid in seen_v:
                    continue
                seen_v.add(vid)
                sample_vids.append(vid)
                if len(sample_vids) >= 3:
                    break
            ok_n = 0
            blocked_n = 0
            unknown_n = 0
            for vid in sample_vids:
                st = await _yt_video_status(vid)
                if st == "ok":
                    ok_n += 1
                elif st == "blocked":
                    blocked_n += 1
                else:
                    unknown_n += 1
            if sample_vids and blocked_n == len(sample_vids):
                logger.info(
                    "free download LOCK (YTM map copyright) %s — %s samples=%d",
                    artist,
                    album,
                    len(sample_vids),
                )
                # все sample под copyright — идём к SC/YT сэмплу ниже
            elif ok_n > 0 or unknown_n > 0 or not sample_vids:
                # bot-check ≠ закрытый контент: android-скачивание часто проходит
                _FREE_AVAIL_CACHE[cache_key] = (True, time.time())
                logger.info(
                    "free download OK via YTM map %s — %s (%d/%d, ok=%d blocked=%d unknown=%d)",
                    artist,
                    album,
                    matched,
                    len(names),
                    ok_n,
                    blocked_n,
                    unknown_n,
                )
                return True

    # 2) сэмпл треков через SC/YT (без скачивания)
    sample_idx: list[int] = []
    n = len(names)
    if n:
        sample_idx = sorted(
            {
                0,
                n // 3,
                (2 * n) // 3,
                n - 1,
            }
        )
    ok = 0
    checked = 0
    for i in sample_idx[:4]:
        name = names[i]
        art = (tracks[i].get("artist") or artist or "").strip()
        exp = int(tracks[i].get("duration") or 0) or None
        checked += 1
        if await probe_track_has_free_source(
            art, name, album=album, expected=exp
        ):
            ok += 1

    # без YTM-альбома — строже для больших релизов (иначе кривые ZIP),
    # но сингл/EP на 1–2 трека открываем, если все проверенные ок
    # (иначе «she wolf - Single» всегда LOCKED при 1/1 на YouTube)
    if not mapping:
        if n <= 2:
            available = checked > 0 and ok == checked
        else:
            available = checked >= min(3, n) and ok == checked
    else:
        available = checked > 0 and (ok / checked) >= 0.5
    _FREE_AVAIL_CACHE[cache_key] = (available, time.time())
    logger.info(
        "free download %s %s — %s (sample %d/%d, ytm_keys=%d)",
        "OK" if available else "LOCKED",
        artist,
        album,
        ok,
        checked,
        len(mapping),
    )
    return available


async def probe_single_track_free_download(
    artist: str,
    title: str,
    *,
    album: str = "",
    expected: Optional[int] = None,
) -> bool:
    import time

    key = _free_avail_key(artist, album or title, track=title)
    hit = _FREE_AVAIL_CACHE.get(key)
    if hit and (time.time() - hit[1]) < _FREE_AVAIL_TTL:
        return hit[0]
    ok = await probe_track_has_free_source(
        artist, title, album=album, expected=expected
    )
    _FREE_AVAIL_CACHE[key] = (ok, time.time())
    return ok


@dataclass
class DownloadedAudio:
    path: Path
    title: str
    artist: str
    duration: Optional[int] = None
    data: bytes = field(default_factory=bytes)

    def payload(self) -> bytes:
        if self.data:
            return self.data
        return self.path.read_bytes() if self.path.exists() else b""


@dataclass(frozen=True)
class AlbumZipResult:
    zip_paths: list[Path]
    send_individually: bool = False
    individual_files: list[DownloadedAudio] | None = None
    album_title: str = ""
    artist: str = ""
    too_large: bool = False


def _safe_name(text: str, limit: int = 80) -> str:
    text = _SAFE.sub("", text or "").strip() or "track"
    return text[:limit]


def _ffmpeg_bin() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return exe
    except Exception:  # noqa: BLE001
        pass
    desktop = Path.home() / "Desktop" / "ffmpeg"
    if desktop.is_dir():
        for exe in desktop.rglob("ffmpeg.exe"):
            return str(exe)
    return None


def _has_ffmpeg() -> bool:
    return _ffmpeg_bin() is not None


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    ff = _ffmpeg_bin()
    if ff:
        bin_dir = str(Path(ff).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    return env


def show_track_selection(
    album_id: str,
    track_list: Sequence[Any],
    *,
    page: int = 0,
    unavailable: Optional[set[int]] = None,
):
    from keyboards import build_track_selection_keyboard

    return build_track_selection_keyboard(
        track_list,
        page=page,
        session_key=album_id,
        unavailable=unavailable,
    )


@dataclass(frozen=True)
class YoutubeHit:
    video_id: str
    title: str
    uploader: str = ""
    duration: int = 0
    url: str = ""

    @property
    def button_label(self) -> str:
        label = self.title
        if self.uploader and self.uploader.lower() not in label.lower():
            label = f"{self.title} – {self.uploader}"
        return label if len(label) <= 64 else label[:61] + "…"

    @property
    def watch_url(self) -> str:
        from utils import normalize_youtube_watch_url

        if self.video_id:
            return normalize_youtube_watch_url(self.video_id)
        if self.url:
            return normalize_youtube_watch_url(self.url)
        return self.url or ""


def parse_artist_title_from_yt(title: str, uploader: str = "") -> tuple[str, str]:
    """
    Достаёт artist/title из названия ролика.
    Поддерживает: «Artist - Title», «Artist – Title», «Artist Title».
    """
    t = (title or "").strip()
    up = (uploader or "").replace(" - Topic", "").strip()
    # убрать типичный мусор в конце
    t_clean = re.sub(
        r"\s*[\(\[][^)\]]*(official|video|audio|lyrics|hd|4k|mv)[^)\]]*[\)\]]\s*",
        " ",
        t,
        flags=re.I,
    )
    t_clean = re.sub(r"\s+", " ", t_clean).strip(" .-–—|")

    for sep in (" - ", " – ", " — ", " | ", " —"):
        if sep in t_clean:
            left, right = t_clean.split(sep, 1)
            return left.strip(), right.strip()

    # «Boris Brejcha Flying Bird.» — артист = первые 1–3 слова, если uploader не Topic
    words = t_clean.split()
    if len(words) >= 3:
        # эвристика: 2 слова артиста + остальное трек (часто у techno/rap)
        for art_len in (2, 3, 1):
            if art_len >= len(words):
                continue
            art = " ".join(words[:art_len]).strip(" .")
            tit = " ".join(words[art_len:]).strip(" .")
            if len(tit) < 2:
                continue
            # не брать uploader-канал как артиста, если имя уже в title
            return art, tit

    if up and up.lower() not in {"various artists", "topic"}:
        # канал-загрузчик часто не артист — только если title короткий
        if len(words) <= 2:
            return up, t_clean or t
    return up, t_clean or t

async def search_youtube_tracks(
    query: str, *, limit: int = 8
) -> list[YoutubeHit]:
    """Поиск треков на YouTube (для релизов вне Apple/Spotify)."""
    import yt_dlp  # type: ignore

    q = (query or "").strip()
    if not q:
        return []

    def _search() -> list[YoutubeHit]:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "http_headers": {"User-Agent": UA},
        }
        out: list[YoutubeHit] = []
        seen: set[str] = set()
        with yt_dlp.YoutubeDL(opts) as ydl:
            try:
                info = ydl.extract_info(f"ytsearch{max(limit, 8)}:{q}", download=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("youtube search: %s", exc)
                return []
            for entry in (info or {}).get("entries") or []:
                if not entry:
                    continue
                vid = (entry.get("id") or "").strip()
                title = (entry.get("title") or "").strip()
                if not vid or not title or vid in seen:
                    continue
                # отсечь явный мусор
                if _BAD_VERSION_RE.search(title) and not _BAD_VERSION_RE.search(q):
                    # оставляем, но позже — пользователь сам выберет; не фильтруем жёстко
                    pass
                seen.add(vid)
                dur = int(entry.get("duration") or 0)
                out.append(
                    YoutubeHit(
                        video_id=vid,
                        title=title,
                        uploader=(entry.get("uploader") or entry.get("channel") or ""),
                        duration=dur,
                        url=f"https://www.youtube.com/watch?v={vid}",
                    )
                )
                if len(out) >= limit:
                    break
        return out

    hits = await asyncio.to_thread(_search)
    logger.info("youtube search %r → %d", q, len(hits))
    return hits


async def download_track(
    query: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress_cb: Optional[ProgressCallback] = None,
    artist: str = "",
    title: str = "",
    expected_duration: Optional[int] = None,
    album: str = "",
    direct_fallback_search: bool = False,
    strict_duration: bool = False,
    skip_video_ids: Optional[set[str]] = None,
) -> DownloadedAudio:
    """Скачивает полный трек: album-YTM → YouTube Music → SoundCloud → YouTube."""
    from utils import extract_youtube_video_id, normalize_youtube_watch_url

    query = (query or "").strip()
    if not query:
        raise DownloadError("Пустой запрос для скачивания.")

    artist = (artist or "").strip()
    title = (title or "").strip()
    album = (album or "").strip()
    skip_vids = {v for v in (skip_video_ids or set()) if v}
    yt_id = extract_youtube_video_id(query)
    direct_yt = bool(yt_id) or (
        query.startswith("http")
        and ("youtube.com/" in query or "youtu.be/" in query)
    )
    if direct_yt and yt_id:
        query = normalize_youtube_watch_url(yt_id)
    if not artist and " - " in query and not direct_yt:
        artist, title = [p.strip() for p in query.split(" - ", 1)]
    if not title:
        title = query

    timeout = max(30.0, float(timeout or DOWNLOAD_TIMEOUT))
    tmp_dir = Path(tempfile.mkdtemp(prefix="tgmusic_"))

    async def _set_pct(pct: int) -> None:
        if progress_cb:
            try:
                await progress_cb(max(0, min(100, int(pct))))
            except Exception as exc:  # noqa: BLE001
                logger.debug("progress_cb: %s", exc)

    def _accept(
        result: Optional[DownloadedAudio],
        *,
        label: str,
        expected: Optional[int],
        require_title_match: bool = True,
    ) -> Optional[DownloadedAudio]:
        if result is None:
            return None
        blob = f"{result.title or ''} {result.artist or ''} {result.path.name if result.path else ''}"
        if require_title_match and not _looks_like_match(
            blob, artist=artist, title=title
        ):
            logger.warning("%s rejected by title: %r", label, blob[:120])
            return None
        exp = expected
        if not _is_full_track_duration(
            result.duration, expected=exp, path=result.path
        ):
            if not require_title_match and not exp and result.path and result.path.exists():
                try:
                    if result.path.stat().st_size >= 400_000:
                        return result
                except OSError:
                    pass
            logger.warning(
                "%s rejected duration=%s expected=%s title=%r",
                label,
                result.duration,
                exp,
                result.title,
            )
            return None
        return result

    async def _collect_sc_yt(expected: Optional[int]) -> list[DownloadedAudio]:
        found: list[DownloadedAudio] = []
        from utils import extract_youtube_video_id as _ext_vid

        if album and artist and title:
            vid = await _ytmusic_album_video_for_track(artist, album, title)
            if vid and vid not in skip_vids:
                url = normalize_youtube_watch_url(vid)
                logger.info("ytmusic album track: %s", url)
                adir = Path(tempfile.mkdtemp(prefix="tgmusic_alb_"))
                result = await _download_ytdlp_async(
                    url, adir, source="", timeout=timeout, set_pct=_set_pct
                )
                ok = _accept(
                    result,
                    label="ytmusic-album",
                    expected=expected,
                    require_title_match=False,
                )
                if ok is not None:
                    return [ok]
                skip_vids.add(vid)

        ytm_url = await _resolve_best_ytmusic(
            artist=artist, title=title, expected=expected, album=album
        )
        ytm_vid = _ext_vid(ytm_url) if ytm_url else ""
        if ytm_url and ytm_vid not in skip_vids:
            logger.info("ytmusic pick: %s", ytm_url)
            ytm_dir = Path(tempfile.mkdtemp(prefix="tgmusic_ytm_"))
            result = await _download_ytdlp_async(
                ytm_url,
                ytm_dir,
                source="",
                timeout=timeout,
                set_pct=_set_pct,
            )
            ok = _accept(result, label="ytmusic", expected=expected)
            if ok is not None:
                return [ok]
            if ytm_vid:
                skip_vids.add(ytm_vid)

        sc_url = await _resolve_best_soundcloud(
            artist=artist, title=title, expected=expected, album=album
        )
        if sc_url:
            logger.info("soundcloud pick: %s", sc_url)
            result = await _download_ytdlp_async(
                sc_url,
                tmp_dir,
                source="",
                timeout=min(90.0, timeout),
                set_pct=_set_pct,
            )
            ok = _accept(result, label="soundcloud", expected=expected)
            if ok is not None:
                found.append(ok)

        search_q = (
            query
            if not direct_yt
            else f"{artist} {title} {album}".strip()
        )
        yt_url = await _resolve_best_youtube(
            artist=artist,
            title=title,
            query=search_q,
            expected=expected,
            album=album,
        )
        yt_vid = _ext_vid(yt_url) if yt_url else ""
        if yt_url and yt_vid not in skip_vids:
            logger.info("youtube pick: %s", yt_url)
            yt_dir = Path(tempfile.mkdtemp(prefix="tgmusic_yt_"))
            result = await _download_ytdlp_async(
                yt_url,
                yt_dir,
                source="",
                timeout=timeout,
                set_pct=_set_pct,
            )
            ok = _accept(result, label="youtube", expected=expected)
            if ok is not None:
                found.append(ok)
        return found

    await _set_pct(0)
    logger.info(
        "download_track start: %r artist=%r title=%r album=%r ffmpeg=%s direct_yt=%s",
        query,
        artist,
        title,
        album,
        _has_ffmpeg(),
        direct_yt,
    )

    try:
        await _set_pct(3)
        if direct_yt:
            result = await _download_ytdlp_async(
                query,
                tmp_dir,
                source="",
                timeout=timeout,
                set_pct=_set_pct,
            )
            ok = _accept(
                result,
                label="direct-yt",
                expected=expected_duration,
                require_title_match=False,
            )
            if ok is not None:
                await _set_pct(100)
                return DownloadedAudio(
                    path=ok.path,
                    title=title if (title and title != query) else (ok.title or title),
                    artist=artist or ok.artist,
                    duration=ok.duration,
                    data=ok.data,
                )
            if yt_id:
                skip_vids.add(yt_id)
            if not direct_fallback_search:
                raise DownloadError(
                    "Не удалось скачать этот YouTube-ролик. "
                    "Проверьте VPN / cookies (YTDLP_COOKIES_FROM_BROWSER)."
                )
            logger.warning("direct-yt rejected — falling back to search")

        await _set_pct(8)
        candidates = await _collect_sc_yt(expected_duration)
        used_expected = expected_duration
        if not candidates and expected_duration and not strict_duration:
            logger.warning(
                "no candidates with catalog duration=%ss — retry without anchor",
                expected_duration,
            )
            candidates = await _collect_sc_yt(None)
            used_expected = None

        if candidates:
            def _closeness(a: DownloadedAudio) -> tuple:
                dur = a.duration or _probe_duration_sec(a.path) or 0
                if used_expected and used_expected > 0:
                    return (abs(dur - used_expected), -dur)
                return (0, -dur)

            best = min(candidates, key=_closeness)
            logger.info(
                "chosen source dur=%ss expected=%s from %d candidates",
                best.duration,
                used_expected,
                len(candidates),
            )
            await _set_pct(100)
            return DownloadedAudio(
                path=best.path,
                title=title or best.title,
                artist=artist or best.artist,
                duration=best.duration,
                data=best.data,
            )

        hint = ""
        if not _node_bin():
            hint = "Установите Node.js (`brew install node`) для YouTube."
        elif not YTDLP_COOKIES_FILE and not YTDLP_COOKIES_FROM_BROWSER:
            hint = (
                "Если ролик есть, но не качает: в .env укажите "
                "YTDLP_COOKIES_FROM_BROWSER=safari"
            )
        raise_unavailable_free(artist, title, extra=hint)
    except DownloadError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("download_track error: %s", exc)
        raise DownloadError(f"Ошибка скачивания: {exc}") from exc


async def download_mp3(
    query: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress_cb: Optional[ProgressCallback] = None,
) -> DownloadedAudio:
    return await download_track(query, timeout=timeout, progress_cb=progress_cb)


async def download_single_track(
    artist: str,
    title: str,
    *,
    timeout: float = DOWNLOAD_TIMEOUT,
    progress_cb: Optional[ProgressCallback] = None,
    expected_duration: Optional[int] = None,
    album: str = "",
    strict: bool = False,
) -> DownloadedAudio:
    """
    Скачать трек. Если передан album — сначала точный videoId с YouTube Music
    альбома (не «похожий» трек из поиска).
    strict=True: не сдаёмся в мусор без каталожной длины (для ZIP альбома).
    """
    from utils import normalize_youtube_watch_url

    artist = (artist or "").strip()
    title = (title or "").strip()
    album = (album or "").strip()
    query = f"{artist} {title}".strip()
    if not query:
        raise DownloadError("Пустой запрос для скачивания.")
    if not expected_duration or expected_duration < 30:
        expected_duration = await _lookup_catalog_duration(artist, title)

    # 1) точный трек с YTM-альбома
    if album and artist and title:
        vid = await _ytmusic_album_video_for_track(artist, album, title)
        if vid:
            url = normalize_youtube_watch_url(vid)
            logger.info(
                "album-exact YTM %s / %s / %s → %s", artist, album, title, url
            )
            try:
                return await download_track(
                    url,
                    timeout=timeout,
                    progress_cb=progress_cb,
                    artist=artist,
                    title=title,
                    expected_duration=expected_duration,
                    album=album,
                    # тот же videoId не крутить; ищем только чистую замену
                    direct_fallback_search=True,
                    strict_duration=True,
                    skip_video_ids={vid},
                )
            except DownloadError as exc:
                if getattr(exc, "unavailable_free", False):
                    raise_unavailable_free(
                        artist,
                        title,
                        extra=(
                            "Официальный ролик на YouTube Music закрыт "
                            "(copyright / UMG), а замены без фейков нет. "
                            "Пришлите ссылку на нужный ролик."
                        ),
                    )
                logger.warning("album-exact failed (%s) — search fallback", exc)

    # короткий неоднозначный title без альбома → не угадываем коллаб
    t_norm = _norm_match(title)
    if (
        (not album)
        and len(t_norm.split()) == 1
        and len(t_norm) <= 5
        and artist
    ):
        raise_unavailable_free(
            artist,
            title,
            extra=(
                "Короткое название неоднозначно. "
                "Пришлите ссылку YouTube на нужный ролик."
            ),
        )

    return await download_track(
        query,
        timeout=timeout,
        progress_cb=progress_cb,
        artist=artist,
        title=title,
        expected_duration=expected_duration,
        album=album,
        strict_duration=strict,
    )


async def _lookup_catalog_duration(artist: str, title: str) -> Optional[int]:
    """
    Длительность трека из каталога (сек).
    Spotify → Deezer → iTunes → Яндекс.Музыка.
    Якорь против превью/лайвов/обрезанных remaster.
    """
    artist = (artist or "").strip()
    title = (title or "").strip()
    if not title:
        return None

    for name, fn in (
        ("itunes", _itunes_catalog_duration),
        ("deezer", _deezer_catalog_duration),
        ("yandex", _yandex_catalog_duration),
        ("spotify", _spotify_catalog_duration),  # опционально, нужен Premium
    ):
        try:
            sec = await fn(artist, title)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s catalog duration: %s", name, exc)
            continue
        if sec:
            return sec
    return None


async def _spotify_catalog_duration(artist: str, title: str) -> Optional[int]:
    """Официальная длина со Spotify (нужны SPOTIFY_CLIENT_ID/SECRET)."""
    try:
        from links import _spotify_access_token
    except Exception:  # noqa: BLE001
        return None

    session = await get_session()
    token = await _spotify_access_token(session)
    if not token:
        return None

    queries = []
    if artist:
        queries.append(f'track:{title} artist:{artist}')
        queries.append(f"{artist} {title}")
    queries.append(title)

    best: Optional[tuple[float, int]] = None
    headers = {"Authorization": f"Bearer {token}"}
    for q in queries:
        try:
            async with session.get(
                "https://api.spotify.com/v1/search",
                params={
                    "q": q,
                    "type": "track",
                    "limit": "10",
                    "market": "US",
                },
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status == 429:
                    logger.warning("spotify catalog rate-limited")
                    return None
                if resp.status != 200:
                    continue
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("spotify catalog search: %s", exc)
            continue

        for row in ((payload.get("tracks") or {}).get("items") or []):
            row_title = (row.get("name") or "").strip()
            arts = ", ".join(
                (a.get("name") or "").strip()
                for a in (row.get("artists") or [])
                if a.get("name")
            )
            ms = int(row.get("duration_ms") or 0)
            if ms < 45_000:
                continue
            if _JUNK_VIDEO_RE.search(row_title) or _BAD_VERSION_RE.search(row_title):
                if not _BAD_VERSION_RE.search(title or ""):
                    continue
            if not _title_core_match(row_title, title, artist=artist):
                continue
            if artist and not _artist_name_ok(artist, arts):
                continue
            score = _token_hit_ratio(row_title, title) * 50
            if artist:
                score += 40
            if row_title.casefold() == title.casefold():
                score += 35
            sec = int(round(ms / 1000))
            if best is None or score > best[0]:
                best = (score, sec)
        if best and best[0] >= 70:
            logger.info("spotify duration %s — %s = %ss", artist, title, best[1])
            return best[1]
    if best and best[0] >= 70:
        logger.info("spotify duration %s — %s = %ss", artist, title, best[1])
        return best[1]
    return None


async def _deezer_catalog_duration(artist: str, title: str) -> Optional[int]:
    """Длина из Deezer Search (без ключа) — запасной каталог."""
    session = await get_session()
    best: Optional[tuple[float, int]] = None
    terms = []
    if artist:
        terms.append(f"{artist} {title}")
    terms.append(f"{title} {artist}".strip())
    for term in terms:
        url = f"https://api.deezer.com/search?q={quote_plus(term)}&limit=12"
        try:
            async with session.get(
                url,
                headers={"User-Agent": UA},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    continue
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("deezer catalog: %s", exc)
            continue
        for row in payload.get("data") or []:
            row_title = (row.get("title") or row.get("title_short") or "").strip()
            row_artist = ((row.get("artist") or {}).get("name") or "").strip()
            sec = int(row.get("duration") or 0)
            if sec < 45:
                continue
            if _JUNK_VIDEO_RE.search(row_title) or _BAD_VERSION_RE.search(row_title):
                if not _BAD_VERSION_RE.search(title or ""):
                    continue
            if not _title_core_match(row_title, title, artist=artist):
                continue
            if artist and not _artist_name_ok(artist, row_artist):
                continue
            score = _token_hit_ratio(row_title, title) * 50
            if artist:
                score += 40
            if row_title.casefold() == title.casefold():
                score += 35
            if best is None or score > best[0]:
                best = (score, sec)
        if best and best[0] >= 70:
            logger.info("deezer duration %s — %s = %ss", artist, title, best[1])
            return best[1]
    if best and best[0] >= 70:
        logger.info("deezer duration %s — %s = %ss", artist, title, best[1])
        return best[1]
    return None


def _artist_name_ok(query: str, candidate: str) -> bool:
    """Артист совпадает целиком (не MikeCarson ≈ Ken Carson)."""
    # стилизация Car$on → Carson
    q_raw = (query or "").lower().replace("$", "s")
    c_raw = (candidate or "").lower().replace("$", "s")
    q = _norm_match(q_raw)
    c = _norm_match(c_raw)
    if not q or not c:
        return not q
    if q == c:
        return True
    q_key = "".join(ch for ch in q_raw if ch.isalnum())
    c_key = "".join(ch for ch in c_raw if ch.isalnum())
    if q_key and c_key and q_key == c_key:
        return True
    q_toks = [t for t in q.split() if len(t) > 1]
    if not q_toks:
        return False
    for tok in q_toks:
        if not re.search(rf"\b{re.escape(tok)}\b", c, flags=re.UNICODE):
            return False
    return True


_ITUNES_ARTIST_ID_CACHE: dict[str, str] = {}


async def _itunes_resolve_artist_id(artist: str) -> Optional[str]:
    """Находит numeric artistId в iTunes (кэш по имени)."""
    artist = (artist or "").strip()
    if not artist:
        return None
    key = artist.casefold()
    cached = _ITUNES_ARTIST_ID_CACHE.get(key)
    if cached:
        return cached

    session = await get_session()
    best: Optional[tuple[float, str]] = None
    for country in ("us", "ru"):
        url = (
            "https://itunes.apple.com/search"
            f"?term={quote_plus(artist)}&entity=musicArtist&limit=8"
            f"&country={country}"
        )
        try:
            async with session.get(
                url,
                headers={"User-Agent": UA},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    continue
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("itunes artist resolve: %s", exc)
            continue
        for row in payload.get("results") or []:
            name = (row.get("artistName") or "").strip()
            aid = row.get("artistId")
            if not aid or not name:
                continue
            if not _artist_name_ok(artist, name):
                continue
            score = 100.0 if name.casefold() == artist.casefold() else 70.0
            score += _token_hit_ratio(name, artist) * 20
            sid = str(int(aid))
            if best is None or score > best[0]:
                best = (score, sid)
        if best and best[0] >= 80:
            _ITUNES_ARTIST_ID_CACHE[key] = best[1]
            return best[1]
    if best and best[0] >= 70:
        _ITUNES_ARTIST_ID_CACHE[key] = best[1]
        return best[1]
    return None


def _itunes_row_duration_score(
    row: dict[str, Any],
    *,
    artist: str,
    title: str,
) -> Optional[tuple[float, int]]:
    row_title = (row.get("trackName") or "").strip()
    row_artist = (row.get("artistName") or "").strip()
    ms = int(row.get("trackTimeMillis") or 0)
    if ms < 45_000 or not row_title:
        return None
    if _JUNK_VIDEO_RE.search(row_title) or _BAD_VERSION_RE.search(row_title):
        if not _BAD_VERSION_RE.search(title or ""):
            return None
    # DJ mixes / Mixed часто короче и мусорные
    coll = (row.get("collectionName") or "").lower()
    if "dj mix" in coll or "(mixed)" in row_title.lower():
        if "mixed" not in (title or "").lower():
            return None
    t_ok = _title_core_match(row_title, title, artist=artist)
    if not t_ok:
        return None
    if artist and not _artist_name_ok(artist, row_artist):
        return None
    score = _token_hit_ratio(row_title, title) * 50
    if artist:
        score += 40
    if row_title.casefold() == title.casefold():
        score += 40
    elif _norm_match(row_title) == _norm_match(title):
        score += 35
    return (score, int(round(ms / 1000)))


async def _itunes_catalog_duration(artist: str, title: str) -> Optional[int]:
    """
    Длина из Apple/iTunes.
    Search по «Artist Title» часто возвращает чужие хиты артиста (Yale → Jennifer's Body).
    Поэтому сначала lookup всех песен по artistId, потом fallback search.
    """
    session = await get_session()
    best: Optional[tuple[float, int]] = None

    aid = await _itunes_resolve_artist_id(artist) if artist else None
    if aid:
        for country in ("us", "ru"):
            url = (
                "https://itunes.apple.com/lookup"
                f"?id={aid}&entity=song&limit=200&country={country}"
            )
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": UA},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("itunes artist songs: %s", exc)
                continue
            for row in payload.get("results") or []:
                if row.get("wrapperType") == "artist":
                    continue
                if row.get("kind") and row.get("kind") != "song":
                    continue
                scored = _itunes_row_duration_score(
                    row, artist=artist, title=title
                )
                if scored is None:
                    continue
                if best is None or scored[0] > best[0]:
                    best = scored
            if best and best[0] >= 90:
                logger.info(
                    "itunes duration(artistId) %s — %s = %ss",
                    artist,
                    title,
                    best[1],
                )
                return best[1]

    terms = []
    if artist:
        terms.append(f"{artist} {title}")
    terms.append(f"{title} {artist}".strip())
    for term in terms:
        for country in ("us", "ru"):
            url = (
                "https://itunes.apple.com/search"
                f"?term={quote_plus(term)}&media=music&entity=song&limit=25"
                f"&country={country}"
            )
            try:
                async with session.get(
                    url,
                    headers={"User-Agent": UA},
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("itunes catalog duration: %s", exc)
                continue
            for row in payload.get("results") or []:
                scored = _itunes_row_duration_score(
                    row, artist=artist, title=title
                )
                if scored is None:
                    continue
                if best is None or scored[0] > best[0]:
                    best = scored
            if best and best[0] >= 90:
                logger.info(
                    "itunes duration %s — %s = %ss", artist, title, best[1]
                )
                return best[1]
    if best and best[0] >= 90:
        logger.info("itunes duration %s — %s = %ss", artist, title, best[1])
        return best[1]
    return None


async def _yandex_catalog_duration(artist: str, title: str) -> Optional[int]:
    """Метаданные Яндекс.Музыки (длина) — без токена, без скачивания."""
    try:
        from yandex_music import Client
    except Exception:  # noqa: BLE001
        return None

    def _sync() -> Optional[int]:
        try:
            client = Client()
            client.init()
        except Exception:  # noqa: BLE001
            return None
        q = f"{artist} {title}".strip()
        try:
            res = client.search(q, type_="track")
        except Exception as exc:  # noqa: BLE001
            logger.debug("yandex search: %s", exc)
            return None
        tracks = (res.tracks.results if res and res.tracks else None) or []
        best: Optional[tuple[float, int]] = None
        for t in tracks[:10]:
            name = (getattr(t, "title", None) or "").strip()
            arts = ", ".join(
                (a.name or "").strip() for a in (getattr(t, "artists", None) or [])
            )
            ms = int(getattr(t, "duration_ms", 0) or 0)
            if ms < 45_000:
                continue
            if _JUNK_VIDEO_RE.search(name) or _BAD_VERSION_RE.search(name):
                if not _BAD_VERSION_RE.search(title or ""):
                    continue
            if not _title_core_match(name, title, artist=artist):
                continue
            if artist and not _artist_name_ok(artist, arts):
                continue
            score = _token_hit_ratio(name, title) * 50
            if artist:
                score += 40 if _artist_name_ok(artist, arts) else 0
            if name.casefold() == title.casefold():
                score += 35
            sec = int(round(ms / 1000))
            if best is None or score > best[0]:
                best = (score, sec)
        return best[1] if best and best[0] >= 70 else None

    try:
        sec = await asyncio.to_thread(_sync)
    except Exception as exc:  # noqa: BLE001
        logger.debug("yandex catalog duration: %s", exc)
        return None
    if sec:
        logger.info("yandex duration %s — %s = %ss", artist, title, sec)
    return sec


async def download_album_as_zip(
    tracks: Sequence[dict[str, str]],
    *,
    artist: str = "",
    album: str = "",
    timeout: float = DOWNLOAD_TIMEOUT,
) -> AlbumZipResult:
    if not tracks:
        raise DownloadError("Нет треков для скачивания.")

    artist = (artist or "").strip()
    album = (album or "").strip()
    # прогреть карту title→videoId с YouTube Music до параллельных скачиваний
    if artist and album:
        try:
            mapping = await _ytmusic_album_track_map(artist, album)
            logger.info(
                "album ZIP prefetch YTM map %s — %s → %d keys",
                artist,
                album,
                len(mapping),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("album ZIP YTM prefetch failed: %s", exc)

    sem = asyncio.Semaphore(3)

    async def _one(t: dict[str, str]) -> Optional[DownloadedAudio]:
        name = (t.get("name") or t.get("track_name") or "").strip()
        art = (t.get("artist") or artist or "").strip()
        if not name:
            return None
        exp = int(t.get("duration") or 0) or None
        async with sem:
            try:
                return await download_single_track(
                    art,
                    name,
                    timeout=timeout,
                    expected_duration=exp,
                    album=album,
                    strict=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ZIP skip %s: %s", name, exc)
                return None

    results = await asyncio.gather(*[_one(t) for t in tracks])
    downloaded = [r for r in results if r is not None]
    if not downloaded:
        raise DownloadError("Не удалось скачать ни одного трека.")

    total_size = sum(len(a.payload()) for a in downloaded)
    if total_size > MAX_AUDIO_BYTES:
        return AlbumZipResult(
            zip_paths=[],
            individual_files=downloaded,
            album_title=album,
            artist=artist,
            too_large=True,
        )

    zip_paths = await asyncio.to_thread(
        _pack_zips, downloaded, artist=artist, album=album
    )
    if not zip_paths:
        return AlbumZipResult(
            zip_paths=[],
            send_individually=True,
            individual_files=downloaded,
            album_title=album,
            artist=artist,
        )
    return AlbumZipResult(
        zip_paths=zip_paths, album_title=album, artist=artist
    )


def _norm_match(text: str) -> str:
    t = (text or "").lower().strip()
    # типографские кавычки/апострофы: Jennifer’s → jennifers (не jennifer s)
    t = re.sub(r"[\u0027\u2018\u2019\u201A\u2032\u0060\u00B4\u02BC\u02B9]", "", t)
    t = t.replace("$", "s")
    t = t.replace("_", " ")
    t = re.sub(r"[「」『』\[\]()（）]", " ", t)
    t = re.sub(r"[^\w\sА-Яа-яЁё]+", " ", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def _album_name_variants(album: str) -> list[str]:
    """A Great Chaos (Deluxe) → [full, A Great Chaos, …] для поиска на YTM."""
    raw = (album or "").strip()
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = re.sub(r"\s+", " ", (s or "").strip(" -–—"))
        if not s:
            return
        key = s.casefold()
        if key in seen:
            return
        seen.add(key)
        out.append(s)

    _add(raw)
    no_paren = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", raw).strip()
    _add(no_paren)
    stripped = re.sub(
        r"\s*[-:(]\s*(single|ep|album|deluxe|extended|expanded|"
        r"bonus|remaster(?:ed)?|anniversary|edition|explicit|clean)"
        r"[\w\s]*$",
        "",
        raw,
        flags=re.I,
    )
    _add(stripped)
    stripped2 = re.sub(
        r"\s*-\s*(single|ep|album)\s*$",
        "",
        raw,
        flags=re.I,
    )
    _add(stripped2)
    return out

_SOFT_TITLE_TOKS = frozenset(
    {
        "official",
        "video",
        "audio",
        "lyrics",
        "lyric",
        "visualizer",
        "hq",
        "hd",
        "4k",
        "mv",
        "prod",
        "prodby",
        "produced",
        "remaster",
        "remastered",
        "cdq",
        "version",
        "edit",
        "radio",
        "album",
        "single",
        "explicit",
        "clean",
        "mp3",
        "m4a",
        "flac",
        "wav",
        "ogg",
        "track",
        "og",
        "original",
        "bonus",
        "demo",
        "dir",
        "dirby",
        "leak",
        "leaked",
        "raw",
        "uncut",
    }
)


def _title_whole_word(haystack: str, needle: str) -> bool:
    """Целое слово (che ∈ 'xavier & che - bleau', но не в 'cheap')."""
    h = _norm_match(haystack)
    n = _norm_match(needle)
    if not h or not n:
        return False
    return bool(re.search(rf"(?:^|\s){re.escape(n)}(?:\s|$)", h))


def _short_or_collab_title_match(
    candidate: str, title: str, *, artist: str = ""
) -> bool:
    """
    Короткие/коллаб-тайтлы (che, 40, glo).
    После _norm_match «A & B - Song» → «a b song», поэтому смотрим токены.
    """
    t = _norm_match(title)
    c = _norm_match(candidate)
    if not t or not c or not _title_whole_word(candidate, title):
        return False
    toks = c.split()
    if t not in toks:
        return False
    # primary: начинается с title
    if toks[0] == t:
        return True
    if not artist:
        return False
    if not (
        _artist_name_ok(artist, candidate)
        or _token_hit_ratio(candidate, artist) >= 0.45
    ):
        return False
    a_toks = [w for w in _norm_match(artist).split() if len(w) > 1]
    if not a_toks:
        return False
    # все токены артиста есть + короткий title рядом (±3)
    if not all(a in toks for a in a_toks):
        return False
    ti = toks.index(t)
    window = toks[max(0, ti - 3) : ti + 4]
    return any(a in window for a in a_toks)


def _title_core_match(
    candidate: str, title: str, *, artist: str = ""
) -> bool:
    """
    «hotel room» ≠ «hotel room service».
    Разрешаем мягкие хвосты (official/audio) и токены артиста в имени файла.
    Короткие имена (che) — отдельная логика коллабов.
    """
    t = _norm_match(title)
    c = _norm_match(candidate)
    if not t or not c:
        return False
    if t == c:
        return True

    t_words = [w for w in t.split() if w]
    # короткий однословный title
    if len(t_words) == 1 and len(t_words[0]) <= 5:
        return _short_or_collab_title_match(candidate, title, artist=artist)

    # односложный/слитный title (wheredoistart):
    # «Grow Apart/wheredoistart (remaster)» — НЕ тот трек
    if " " not in t and len(t) >= 6:
        c_glued = re.sub(r"\s+", "", c)
        if t not in c and t not in c_glued:
            return False
        c_toks = [w for w in c.split() if len(w) > 1]
        a_toks = set(
            _norm_match((artist or "").replace("$", "s")).split()
        )
        extras: list[str] = []
        for w in c_toks:
            if w == t or t.startswith(w) or w.startswith(t):
                continue
            if w in _SOFT_TITLE_TOKS or w in a_toks:
                continue
            if w in {"feat", "ft", "featuring", "with"}:
                break
            extras.append(w)
        # любые чужие слова («grow apart») — отказ
        if extras and not all(w in _SOFT_TITLE_TOKS for w in extras):
            return False
        return True
    if _token_hit_ratio(candidate, title) < 0.55:
        return False
    c_toks_set = set(c.split())
    for tok in t.split():
        if len(tok) <= 1:
            continue
        if tok not in c_toks_set and tok not in c:
            return False
    c_toks = [w for w in c.split() if len(w) > 1]
    t_toks = set(t.split())
    a_toks = set(_norm_match((artist or "").replace("$", "s")).split())
    extras = []
    i = 0
    while i < len(c_toks):
        w = c_toks[i]
        if w in t_toks or w in _SOFT_TITLE_TOKS or w in a_toks:
            i += 1
            continue
        if w in {"feat", "ft", "featuring", "with"}:
            break  # дальше обычно feat-артисты
        extras.append(w)
        i += 1
    if not extras:
        return True
    # хвост вроде (og version) / (cru n mag) — ок; hotel room service — нет
    _hard_extra = {
        "service",
        "remix",
        "slowed",
        "reverb",
        "reverse",
        "reversed",
        "instrumental",
        "snippet",
        "nightcore",
        "karaoke",
        "cover",
        "mashup",
    }
    if any(w in _hard_extra for w in extras):
        return False
    if len(extras) <= 4 and all(
        w in _SOFT_TITLE_TOKS or len(w) <= 4 for w in extras
    ):
        return True
    return False


def _token_hit_ratio(haystack: str, needle: str) -> float:
    toks = [t for t in _norm_match(needle).split() if len(t) > 1]
    if not toks:
        # одно короткое слово вроде "x" — точное вхождение
        n = _norm_match(needle)
        if not n:
            return 0.0
        return 1.0 if n in _norm_match(haystack) else 0.0
    h = _norm_match(haystack)
    hits = sum(1 for t in toks if t in h)
    return hits / len(toks)


def _probe_duration_sec(path: Path) -> Optional[int]:
    """Длительность файла через ffprobe / mutagen fallback."""
    if not path or not path.exists():
        return None
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            import subprocess

            out = subprocess.check_output(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
            val = float((out or b"").decode().strip() or "0")
            if val > 0:
                return int(round(val))
        except Exception:  # noqa: BLE001
            pass
    # грубо по размеру: ~16 КБ/сек для 128kbps
    try:
        size = path.stat().st_size
        if size > 0:
            return max(1, int(size / 16000))
    except OSError:
        pass
    return None


def _is_full_track_duration(
    duration: Optional[int],
    *,
    expected: Optional[int] = None,
    path: Optional[Path] = None,
) -> bool:
    """True, если длина похожа на полный трек, а не на 30с превью / обрезанный remaster."""
    dur = duration
    if (not dur or dur <= 0) and path is not None:
        dur = _probe_duration_sec(path)
    if not dur or dur <= 0:
        # неизвестно — не режем, если файл явно не крошечный
        if path is not None and path.exists() and path.stat().st_size >= 700_000:
            return True
        return False
    if expected and expected >= 60:
        # к каталогу: отсекаем 30с превью и сильно обрезанные remaster
        lo = max(_MIN_FULL_TRACK_SEC, int(expected * 0.88))
        hi = int(expected * 1.15) + 10
        return lo <= dur <= hi
    if dur < _MIN_FULL_TRACK_SEC:
        return False
    if dur > _MAX_TRACK_SEC:
        return False
    return True


def _looks_like_match(candidate_title: str, *, artist: str, title: str) -> bool:
    """Грубая проверка, что скачали не совсем левый ролик."""
    if not candidate_title:
        return False
    if _JUNK_VIDEO_RE.search(candidate_title) and not _JUNK_VIDEO_RE.search(title or ""):
        return False
    if title and not _title_core_match(candidate_title, title, artist=artist):
        return False
    artist_ratio = _token_hit_ratio(candidate_title, artist) if artist else 0.0
    title_ratio = _token_hit_ratio(candidate_title, title) if title else 0.0
    t_norm = _norm_match(title)
    c_norm = _norm_match(candidate_title)
    if artist and artist_ratio < 0.2:
        if title_ratio < 0.75 and (not t_norm or t_norm not in c_norm):
            return False
    if _BAD_VERSION_RE.search(candidate_title) and not _BAD_VERSION_RE.search(title or ""):
        soft = re.search(
            r"\b(cdq|hq|official\s*audio)\b",
            candidate_title,
            re.I,
        )
        # remaster без точного title-core уже отсечён выше;
        # «remaster» сам по себе не амнистия для чужих треков
        if not soft and not re.search(r"\bremaster(?:ed)?\b", candidate_title, re.I):
            return False
        if _JUNK_VIDEO_RE.search(candidate_title):
            return False
    return True


async def _soundcloud_client_id() -> str:
    """Достаёт client_id SoundCloud из их JS (кэш ~12ч)."""
    global _SC_CLIENT_ID, _SC_CLIENT_TS
    if _SC_CLIENT_ID and (time.time() - _SC_CLIENT_TS) < 12 * 3600:
        return _SC_CLIENT_ID

    session = await get_session()
    async with session.get(
        "https://soundcloud.com",
        headers={"User-Agent": UA},
        timeout=aiohttp.ClientTimeout(total=15),
    ) as resp:
        html = await resp.text()
    scripts = re.findall(
        r'src="(https://a-v2\.sndcdn\.com/assets/[^"]+\.js)"', html
    )
    for script_url in scripts[:20]:
        try:
            async with session.get(
                script_url,
                headers={"User-Agent": UA},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                js = await resp.text()
        except Exception:  # noqa: BLE001
            continue
        m = re.search(r'client_id["\s:=]+([a-zA-Z0-9]{32})', js)
        if m:
            _SC_CLIENT_ID = m.group(1)
            _SC_CLIENT_TS = time.time()
            logger.info("soundcloud client_id ok")
            return _SC_CLIENT_ID
    raise DownloadError("Не удалось получить SoundCloud client_id")


def _score_soundcloud_track(
    item: dict[str, Any],
    *,
    artist: str,
    title: str,
    expected: Optional[int],
) -> float:
    et = (item.get("title") or "").strip()
    user = ((item.get("user") or {}).get("username") or "").strip()
    dur_ms = int(item.get("duration") or 0)
    dur = int(round(dur_ms / 1000)) if dur_ms else 0
    et_l = et.lower()

    if _JUNK_VIDEO_RE.search(et) and not _JUNK_VIDEO_RE.search(title or ""):
        return -999.0
    if re.search(r"slowed|reverb|speed\s*up|sped\s*up|nightcore", et_l):
        return -999.0
    if re.search(r"\blive\b|concert|rolling\s*loud|festival", et_l):
        return -999.0
    # официальный тизер артиста ~30с
    if dur and dur < _MIN_FULL_TRACK_SEC:
        return -999.0
    if expected and not _is_full_track_duration(dur, expected=expected):
        return -999.0

    if not _looks_like_match(et, artist=artist, title=title):
        # только короткие коллаб-тайтлы (che) — иначе «Grow Apart/wheredoistart» проходит
        t_norm = _norm_match(title)
        if not (
            len(t_norm.split()) == 1
            and len(t_norm) <= 5
            and _short_or_collab_title_match(et, title, artist=artist)
        ):
            return -999.0

    score = 0.0
    score += _token_hit_ratio(et, title) * 50
    score += _token_hit_ratio(et, artist) * 25
    score += _token_hit_ratio(user, artist) * 30
    if _norm_match(title) and _norm_match(title) in _norm_match(et):
        score += 30
    if (
        title
        and len(_norm_match(title).split()) == 1
        and len(_norm_match(title)) <= 5
        and _short_or_collab_title_match(et, title, artist=artist)
    ):
        score += 35
    if "full song" in et_l or "hq remaster" in et_l or "best remaster" in et_l:
        score += 12
    if "remaster" in et_l or "cdq" in et_l or "og version" in et_l or "(og)" in et_l:
        score += 8
    if "leak" in et_l and "snippet" not in et_l:
        score += 6
    if expected and dur:
        # чем ближе к каталогу — тем лучше
        score += max(0, 40 - abs(dur - expected) * 1.5)
    if artist and artist.lower() in user.lower():
        # сам артист часто заливает только тизер — уже отсечён по duration
        score += 10
    return score


async def _resolve_best_soundcloud(
    *,
    artist: str,
    title: str,
    expected: Optional[int] = None,
    album: str = "",
) -> str:
    """Ищет лучший SoundCloud URL через API (не через зависающий scsearch)."""
    try:
        client_id = await _soundcloud_client_id()
    except Exception as exc:  # noqa: BLE001
        logger.warning("soundcloud client_id: %s", exc)
        return ""

    queries: list[str] = []
    if artist and title:
        queries.extend(
            [
                f"{artist} {title}",
                f"{artist} - {title}",
                f"{title} {artist}",
                f"{artist} {title} og",
                f"{artist} {title} leak",
            ]
        )
        if album:
            queries.insert(0, f"{artist} {title} {album}")
        # короткие имена — чаще коллабы
        if len(_norm_match(title).split()) == 1 and len(_norm_match(title)) <= 5:
            queries.insert(0, f"{artist} & {title}")
            queries.insert(0, f"{title} {artist}")
    elif title:
        queries.append(title)
    session = await get_session()
    best_url = ""
    best_score = -999.0
    best_title = ""
    seen: set[str] = set()

    for q in queries[:7]:
        url = (
            "https://api-v2.soundcloud.com/search/tracks"
            f"?q={quote_plus(q)}&client_id={client_id}&limit=25"
        )
        try:
            async with session.get(
                url,
                headers={"User-Agent": UA, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                if resp.status != 200:
                    continue
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sc search %r: %s", q, exc)
            continue
        for item in payload.get("collection") or []:
            permalink = (item.get("permalink_url") or "").strip()
            if not permalink or permalink in seen:
                continue
            seen.add(permalink)
            sc = _score_soundcloud_track(
                item, artist=artist, title=title, expected=expected
            )
            user = ((item.get("user") or {}).get("username") or "").lower()
            # архивы артиста / оф. аккаунты
            if artist and (
                artist.casefold().replace(" ", "") in user.replace(" ", "")
                or "archive" in user
            ):
                sc += 12
            logger.debug(
                "sc candidate %.1f %ss %r",
                sc,
                int((item.get("duration") or 0) / 1000),
                item.get("title"),
            )
            if sc > best_score:
                best_score = sc
                best_url = permalink
                best_title = item.get("title") or ""

    if best_url and best_score >= 35:
        logger.info(
            "soundcloud pick score=%.1f title=%r url=%s",
            best_score,
            best_title,
            best_url,
        )
        return best_url
    logger.warning(
        "no good soundcloud match (best=%.1f %r)", best_score, best_title
    )
    return ""


def _ytmusic_duration_sec(row: dict[str, Any]) -> Optional[int]:
    if row.get("duration_seconds"):
        try:
            return int(row["duration_seconds"])
        except (TypeError, ValueError):
            pass
    raw = row.get("duration")
    if isinstance(raw, (int, float)) and raw > 0:
        return int(raw)
    if isinstance(raw, str) and ":" in raw:
        parts = raw.split(":")
        try:
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except ValueError:
            return None
    return None


def _score_ytmusic_song(
    row: dict[str, Any],
    *,
    artist: str,
    title: str,
    expected: Optional[int] = None,
) -> float:
    name = (row.get("title") or "").strip()
    arts = ", ".join(
        (a.get("name") or "").strip()
        for a in (row.get("artists") or [])
        if a.get("name")
    )
    if not name or not row.get("videoId"):
        return -999.0
    if _JUNK_VIDEO_RE.search(name) and not _JUNK_VIDEO_RE.search(title or ""):
        return -999.0
    if _BAD_VERSION_RE.search(name) and not _BAD_VERSION_RE.search(title or ""):
        return -999.0
    if artist and not _artist_name_ok(artist, arts):
        return -999.0
    if not _title_core_match(name, title, artist=artist):
        return -999.0

    title_ratio = _token_hit_ratio(name, title)
    t_norm = _norm_match(title)
    n_norm = _norm_match(name)

    dur = _ytmusic_duration_sec(row)
    if dur and dur < _MIN_FULL_TRACK_SEC:
        return -999.0
    if expected and dur and not _is_full_track_duration(dur, expected=expected):
        return -999.0

    score = 40.0 + title_ratio * 40
    if n_norm == t_norm:
        score += 35
    elif t_norm and t_norm in n_norm:
        score += 20
    if artist:
        score += 25
    album = ((row.get("album") or {}) or {}).get("name") or ""
    if album:
        score += 8
    if expected and dur:
        score += max(0, 40 - abs(dur - expected) * 1.5)
    return score


_YTM_ALBUM_CACHE: dict[str, dict[str, str]] = {}
_YTM_CLIENT = None


def _ytmusic_client():
    """
    Клиент YouTube Music с регионом из .env (по умолчанию US).
    Для реального каталога другой страны часто нужен YTMUSIC_PROXY
    (YouTube смотрит на IP, не только на gl=US).
    """
    global _YTM_CLIENT
    if _YTM_CLIENT is not None:
        return _YTM_CLIENT
    from ytmusicapi import YTMusic

    proxies = None
    proxy = (YTMUSIC_PROXY or "").strip()
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    loc = (YTMUSIC_LOCATION or "US").strip().upper() or "US"
    lang = (YTMUSIC_LANGUAGE or "en").strip() or "en"
    _YTM_CLIENT = YTMusic(
        language=lang,
        location=loc,
        proxies=proxies,
    )
    logger.info(
        "ytmusic client location=%s language=%s proxy=%s",
        loc,
        lang,
        "yes" if proxy else "no",
    )
    return _YTM_CLIENT


def _album_title_key(text: str) -> str:
    """Ключ для сопоставления названий треков альбома (f**k ≈ fuck, $ ≈ s)."""
    t = (text or "").lower()
    # цензура Apple / iTunes: f**k, s**t и т.п.
    t = re.sub(r"f\*+c?k", "fuck", t)
    t = re.sub(r"s\*+t", "shit", t)
    t = re.sub(r"a\*+s\b", "ass", t)
    t = re.sub(r"b\*+ch", "bitch", t)
    t = re.sub(r"n\*+g+a?", "nigga", t)
    t = t.replace("*", "")
    t = t.replace("$", "s")
    t = _norm_match(t)
    return re.sub(r"[^a-z0-9а-яё]+", "", t, flags=re.IGNORECASE)


def _match_album_video_id(title: str, mapping: dict[str, str]) -> str:
    if not title or not mapping:
        return ""
    key = _album_title_key(title)
    if key and key in mapping:
        return mapping[key]
    # частичное: все символы title есть и длина близка
    best = ""
    best_score = 0.0
    for k, vid in mapping.items():
        if not k or not vid:
            continue
        if key and (key in k or k in key) and abs(len(k) - len(key)) <= 3:
            score = min(len(k), len(key)) / max(len(k), len(key))
            if score > best_score:
                best_score = score
                best = vid
        elif _title_core_match(k, title) or _title_core_match(title, k):
            return vid
    return best if best_score >= 0.75 else ""


async def _ytmusic_album_track_map(artist: str, album: str) -> dict[str, str]:
    """
    title_key → videoId для треков альбома на YouTube Music.
    Это главный якорь: альбом из Apple → те же треки с YTM, не «похожий» поиск.
    """
    artist = (artist or "").strip()
    album = (album or "").strip()
    if not artist or not album:
        return {}
    cache_key = (
        f"{(YTMUSIC_LOCATION or 'US').upper()}::"
        f"{artist.casefold()}::{album.casefold()}"
    )
    if cache_key in _YTM_ALBUM_CACHE:
        return _YTM_ALBUM_CACHE[cache_key]

    def _fetch() -> dict[str, str]:
        try:
            client = _ytmusic_client()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ytmusic init: %s", exc)
            return {}

        album_variants = _album_name_variants(album)
        queries: list[str] = []
        for alb in album_variants:
            queries.extend(
                [
                    f"{artist} {alb}",
                    f"{alb} {artist}",
                    alb,
                ]
            )
        # уникальные
        seen_q: set[str] = set()
        uniq_q: list[str] = []
        for q in queries:
            k = q.casefold()
            if k in seen_q:
                continue
            seen_q.add(k)
            uniq_q.append(q)

        browse_id = ""
        album_clean = album_variants[-1] if album_variants else album
        for q in uniq_q:
            try:
                rows = client.search(q, filter="albums", limit=10) or []
            except Exception as exc:  # noqa: BLE001
                logger.debug("ytmusic album search %r: %s", q, exc)
                continue
            best_bid = ""
            best_sc = -1.0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                name = (row.get("title") or "").strip()
                arts = ", ".join(
                    (a.get("name") or "").strip()
                    for a in (row.get("artists") or [])
                    if a.get("name")
                )
                if artist and not _artist_name_ok(artist, arts):
                    continue
                name_key = _album_title_key(name)
                ok_title = False
                for alb in album_variants:
                    alb_key = _album_title_key(alb)
                    if not alb_key:
                        continue
                    if name_key == alb_key or alb_key in name_key or name_key in alb_key:
                        ok_title = True
                        break
                    if _title_core_match(name, alb, artist=artist):
                        ok_title = True
                        break
                if not ok_title:
                    continue
                sc = 0.0
                for alb in album_variants:
                    sc = max(sc, _token_hit_ratio(name, alb) * 50)
                    if _album_title_key(name) == _album_title_key(alb):
                        sc += 45
                    elif _album_title_key(alb) in _album_title_key(name) or _album_title_key(
                        name
                    ) in _album_title_key(alb):
                        sc += 25
                bid = (row.get("browseId") or "").strip()
                if bid and sc > best_sc:
                    best_sc = sc
                    best_bid = bid
            if best_bid and best_sc >= 35:
                browse_id = best_bid
                break
        if not browse_id:
            return {}
        try:
            details = client.get_album(browse_id) or {}
        except Exception as exc:  # noqa: BLE001
            logger.debug("ytmusic get_album %s: %s", browse_id, exc)
            return {}
        out: dict[str, str] = {}
        for tr in details.get("tracks") or []:
            if not isinstance(tr, dict):
                continue
            name = (tr.get("title") or "").strip()
            vid = (tr.get("videoId") or "").strip()
            if not name or not vid:
                continue
            out[_album_title_key(name)] = vid
            # также нормализованное имя
            out[_norm_match(name)] = vid
        logger.info(
            "ytmusic album map %s — %s → %d tracks",
            artist,
            album,
            len(out) // 2,
        )
        return out

    mapping = await asyncio.to_thread(_fetch)
    if mapping:
        _YTM_ALBUM_CACHE[cache_key] = mapping
        loc = (YTMUSIC_LOCATION or "US").upper()
        # также кэш без (Deluxe) чтобы следующие запросы попадали
        for alb in _album_name_variants(album)[1:]:
            _YTM_ALBUM_CACHE[
                f"{loc}::{artist.casefold()}::{alb.casefold()}"
            ] = mapping
    return mapping


async def _ytmusic_album_video_for_track(
    artist: str, album: str, title: str
) -> str:
    mapping = await _ytmusic_album_track_map(artist, album)
    return _match_album_video_id(title, mapping)


async def _resolve_best_ytmusic(
    *,
    artist: str,
    title: str,
    expected: Optional[int] = None,
    album: str = "",
) -> str:
    """Официальный трек из YouTube Music → watch URL. Пусто если нет совпадения."""

    # сначала точный трек с альбома
    if album:
        vid = await _ytmusic_album_video_for_track(artist, album, title)
        if vid:
            logger.info(
                "ytmusic album-exact title=%r id=%s", title, vid
            )
            return f"https://www.youtube.com/watch?v={vid}"

    def _search() -> str:
        try:
            client = _ytmusic_client()
        except Exception as exc:  # noqa: BLE001
            logger.debug("ytmusic init: %s", exc)
            return ""

        queries: list[str] = []
        if artist and title:
            if album:
                for alb in _album_name_variants(album)[:2]:
                    queries.append(f"{artist} {title} {alb}")
            queries.extend(
                [
                    f"{artist} {title}",
                    f"{title} {artist}",
                    f'"{title}" {artist}',
                ]
            )
        elif title:
            queries.append(title)

        best_id = ""
        best_score = -999.0
        best_title = ""
        seen_q: set[str] = set()
        for q in queries:
            key = q.casefold()
            if not q or key in seen_q:
                continue
            seen_q.add(key)
            try:
                rows = client.search(q, filter="songs", limit=25) or []
            except Exception as exc:  # noqa: BLE001
                logger.debug("ytmusic search %r: %s", q, exc)
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                sc = _score_ytmusic_song(
                    row, artist=artist, title=title, expected=expected
                )
                if album:
                    alb_name = ((row.get("album") or {}) or {}).get("name") or ""
                    if alb_name:
                        for alb in _album_name_variants(album):
                            if _album_title_key(alb_name) == _album_title_key(alb) or (
                                _album_title_key(alb)
                                and _album_title_key(alb) in _album_title_key(alb_name)
                            ):
                                sc += 35
                                break
                if sc > best_score:
                    best_score = sc
                    best_id = (row.get("videoId") or "").strip()
                    best_title = (row.get("title") or "").strip()

        if best_id and best_score >= 70:
            logger.info(
                "ytmusic pick score=%.1f title=%r id=%s",
                best_score,
                best_title,
                best_id,
            )
            return f"https://www.youtube.com/watch?v={best_id}"
        logger.info(
            "ytmusic no match (best=%.1f %r) — fallback SC/YT",
            best_score,
            best_title,
        )
        return ""

    return await asyncio.to_thread(_search)


def _score_youtube_entry(
    entry: dict[str, Any],
    *,
    artist: str,
    title: str,
    expected: Optional[int] = None,
) -> float:
    et = (entry.get("title") or "").strip()
    up = (
        (entry.get("uploader") or "")
        or (entry.get("channel") or "")
        or (entry.get("uploader_id") or "")
    ).strip()
    dur = int(entry.get("duration") or 0)
    et_l = et.lower()
    up_l = up.lower()

    # мусор сразу в минус бесконечность
    if _JUNK_VIDEO_RE.search(et) and not _JUNK_VIDEO_RE.search(title or ""):
        return -999.0
    title_ok = bool(title) and _title_core_match(et, title, artist=artist)
    if title and not title_ok:
        return -999.0
    if dur and dur < _MIN_FULL_TRACK_SEC:
        return -999.0
    if dur and dur > _MAX_TRACK_SEC:
        return -200.0
    if expected and dur and not _is_full_track_duration(dur, expected=expected):
        return -999.0

    score = 0.0
    title_ratio = _token_hit_ratio(et, title)
    artist_in_title = _token_hit_ratio(et, artist)
    artist_in_up = _token_hit_ratio(up, artist)
    t_norm = _norm_match(title)
    et_norm = _norm_match(et)

    score += title_ratio * 45
    score += artist_in_title * 22
    score += artist_in_up * 28

    if t_norm and t_norm in et_norm:
        score += 25
    # короткий title в коллабе — не primary song name, но валиден
    if (
        title
        and len(t_norm.split()) == 1
        and len(t_norm) <= 5
        and _short_or_collab_title_match(et, title, artist=artist)
    ):
        score += 30
    if artist and title:
        compact = _norm_match(f"{artist} {title}")
        if et_norm.startswith(compact) or compact in et_norm[: len(compact) + 8]:
            score += 20

    if "topic" in up_l:
        score += 35
    if "official audio" in et_l or "official music video" in et_l:
        score += 28
    elif "official" in et_l:
        score += 14
    if artist and artist.lower() == up_l:
        score += 20

    if re.search(r"\b(remaster|cdq|hq audio)\b", et_l):
        score += 6

    if _BAD_VERSION_RE.search(et_l) and not _BAD_VERSION_RE.search(title or ""):
        if not re.search(r"\b(remaster|cdq|hq)\b", et_l):
            score -= 80

    if re.search(r"\blive\b|concert|rolling\s*loud|festival", et_l) and not re.search(
        r"\blive\b|concert", (title or "").lower()
    ):
        score -= 90

    if expected and dur:
        score += max(0, 40 - abs(dur - expected) * 1.5)
    elif dur and 90 <= dur <= 360:
        score += 15
    elif dur and dur > 480:
        score -= 20

    if title and title_ratio < 0.35 and t_norm not in et_norm:
        score -= 40

    return score


async def _resolve_best_youtube(
    *,
    artist: str,
    title: str,
    query: str,
    expected: Optional[int] = None,
    album: str = "",
) -> str:
    """
    Ищет среди нескольких результатов YouTube лучший ролик.
    Возвращает URL watch?v=… или "".
    """
    import yt_dlp  # type: ignore

    album = (album or "").strip()
    variants = []
    if artist and title:
        if album:
            variants.append(f"{artist} - {title} {album}")
            variants.append(f"{artist} {album} {title}")
        t_norm = _norm_match(title)
        if len(t_norm.split()) == 1 and len(t_norm) <= 5:
            variants.extend(
                [
                    f"{artist} & {title}",
                    f"{title} & {artist}",
                    f"{artist} {title} official",
                ]
            )
        variants.extend(
            [
                f'{artist} - {title} "Official Audio"',
                f"{artist} - {title} topic",
                f"{artist} - {title}",
                f'"{artist}" "{title}"',
                f"{artist} {title}",
                f"{title} {artist}",
            ]
        )
    variants.append(query)
    # уникальные с сохранением порядка
    seen: set[str] = set()
    searches: list[str] = []
    for v in variants:
        v = (v or "").strip()
        if v and v.lower() not in seen:
            seen.add(v.lower())
            searches.append(v)

    def _search() -> str:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": True,
            "http_headers": {"User-Agent": UA},
            "socket_timeout": 20,
        }
        proxy = (YTMUSIC_PROXY or "").strip()
        if proxy:
            opts["proxy"] = proxy
        best_id = ""
        best_score = -999.0
        best_title = ""
        with yt_dlp.YoutubeDL(opts) as ydl:
            for q in searches[:6]:
                try:
                    info = ydl.extract_info(f"ytsearch15:{q}", download=False)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("ytsearch %r: %s", q, exc)
                    continue
                for entry in (info or {}).get("entries") or []:
                    if not entry:
                        continue
                    vid = entry.get("id") or ""
                    if not vid:
                        continue
                    etitle = entry.get("title") or ""
                    if _JUNK_VIDEO_RE.search(etitle) and not _JUNK_VIDEO_RE.search(
                        title or ""
                    ):
                        continue
                    if _BAD_VERSION_RE.search(etitle) and not _BAD_VERSION_RE.search(
                        title or ""
                    ):
                        # remaster/cdq только если title-core уже ок (score ниже)
                        if not re.search(r"\b(remaster|cdq|hq)\b", etitle, re.I):
                            continue
                    sc = _score_youtube_entry(
                        entry, artist=artist, title=title, expected=expected
                    )
                    logger.debug(
                        "yt candidate %.1f %s | %s",
                        sc,
                        entry.get("title"),
                        entry.get("uploader"),
                    )
                    if sc > best_score:
                        best_score = sc
                        best_id = vid
                        best_title = entry.get("title") or ""
        if best_id and best_score >= 40:
            logger.info(
                "youtube pick score=%.1f title=%r id=%s",
                best_score,
                best_title,
                best_id,
            )
            return f"https://www.youtube.com/watch?v={best_id}"
        if best_id and best_score >= 25:
            logger.info(
                "youtube weak pick score=%.1f title=%r id=%s",
                best_score,
                best_title,
                best_id,
            )
            return f"https://www.youtube.com/watch?v={best_id}"
        logger.warning("no good youtube match (best=%.1f %r)", best_score, best_title)
        return ""

    return await asyncio.to_thread(_search)


def _node_bin() -> Optional[str]:
    return shutil.which("node") or shutil.which("nodejs")


# Telegram ~48 МБ ≈ ~25–30 мин MP3; длинные миксы не отправляются
_MAX_YT_DURATION_SEC = 30 * 60


def _ytdlp_auth_args() -> list[str]:
    """Cookies / JS runtime — без этого YouTube часто требует Sign in."""
    args: list[str] = []
    node = _node_bin()
    if node:
        args.extend(["--js-runtimes", f"node:{node}"])
        # EJS challenge solver (нужен для аудиоформатов на новых YouTube)
        args.extend(["--remote-components", "ejs:github"])
    if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).is_file():
        args.extend(["--cookies", YTDLP_COOKIES_FILE])
    elif YTDLP_COOKIES_FROM_BROWSER:
        args.extend(["--cookies-from-browser", YTDLP_COOKIES_FROM_BROWSER])
    return args


def _build_ytdlp_cmd(search: str, outtmpl: str) -> list[str]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-o",
        outtmpl,
        "--ignore-errors",
        "--max-downloads",
        "1",
        "--no-warnings",
        "--newline",
        "--progress",
        "--no-playlist",
    ]
    cmd.extend(_ytdlp_auth_args())
    proxy = (YTMUSIC_PROXY or "").strip()
    if proxy:
        cmd.extend(["--proxy", proxy])
    if _has_ffmpeg():
        cmd.extend(["-x", "--audio-format", "mp3", "--audio-quality", "0"])
    else:
        logger.warning("ffmpeg не найден — скачиваю без конвертации в mp3")
        cmd.extend(["-f", "bestaudio/best"])
    if "youtube.com" in search or "youtu.be" in search or search.startswith("ytsearch"):
        # cookies + EJS решают Sign in / signature; android часто стабильнее
        cmd.extend(["--extractor-args", "youtube:player_client=android,web"])
        # отсекаем превью/сниппеты и многочасовые миксы
        cmd.extend(
            [
                "--match-filter",
                f"duration >=? {_MIN_FULL_TRACK_SEC} & duration <=? {_MAX_YT_DURATION_SEC}",
            ]
        )
    cmd.append(search)
    return cmd


async def _download_ytdlp_async(
    query: str,
    tmp_dir: Path,
    *,
    source: str,
    timeout: float,
    set_pct: Callable[[int], Awaitable[None]],
) -> Optional[DownloadedAudio]:
    outtmpl = str(tmp_dir / "%(title).80B.%(ext)s")
    if source:
        search = f"{source}:{query}"
    else:
        search = query  # прямой URL
    cmd = _build_ytdlp_cmd(search, outtmpl)

    logger.info("yt-dlp async: %s", search)
    await set_pct(5)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_subprocess_env(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot start yt-dlp: %s", exc)
        return None

    last_pct = 5
    stderr_chunks: list[str] = []

    async def _read_stream(stream: asyncio.StreamReader, *, is_err: bool) -> None:
        nonlocal last_pct
        while True:
            line_b = await stream.readline()
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="ignore").strip()
            if not line:
                continue
            if is_err:
                stderr_chunks.append(line)
                logger.debug("yt-dlp: %s", line[:200])
            m = _PCT_RE.search(line)
            if m:
                try:
                    pct = int(float(m.group(1)))
                    mapped = min(90, max(5, pct))
                    if mapped >= last_pct + 2 or mapped >= 90:
                        last_pct = mapped
                        await set_pct(mapped)
                except ValueError:
                    pass
            elif "Destination" in line or "ExtractAudio" in line or "Deleting" in line:
                if last_pct < 95:
                    last_pct = 95
                    await set_pct(95)

    try:
        await asyncio.wait_for(
            asyncio.gather(
                _read_stream(proc.stdout, is_err=False),  # type: ignore[arg-type]
                _read_stream(proc.stderr, is_err=True),  # type: ignore[arg-type]
                proc.wait(),
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.error("yt-dlp timeout %ss — kill (%s)", timeout, source)
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001
            pass
        # файл мог успеть скачаться до kill
        audio = _pick_downloaded_file(tmp_dir)
        if audio is not None:
            await set_pct(97)
            try:
                return await asyncio.to_thread(_finalize_file, audio, query, tmp_dir)
            except DownloadError as exc:
                logger.warning("finalize after timeout rejected: %s", exc)
                return None
        if source.startswith("yt"):
            raise DownloadError(
                "⏰ Скачивание превысило лимит времени. Попробуйте ещё раз."
            )
        return None

    code = proc.returncode
    audio = _pick_downloaded_file(tmp_dir)
    if audio is not None and (code in _OK_CODES or code is None):
        await set_pct(97)
        try:
            return await asyncio.to_thread(_finalize_file, audio, query, tmp_dir)
        except DownloadError as exc:
            logger.warning("finalize rejected: %s", exc)
            try:
                audio.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    if audio is not None:
        # файл есть даже при странном коде выхода
        await set_pct(97)
        try:
            return await asyncio.to_thread(_finalize_file, audio, query, tmp_dir)
        except DownloadError as exc:
            logger.warning("finalize rejected: %s", exc)
            try:
                audio.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    tail = "\n".join(stderr_chunks[-8:])
    logger.warning("yt-dlp %s exit %s: %s", source, code, tail[-500:])
    joined = "\n".join(stderr_chunks)
    if "match-filter" in joined.lower() or "does not pass filter" in joined.lower():
        # короткое/длинное — просто пробуем следующий источник
        logger.info("yt-dlp match-filter skip (%s)", source)
        return None
    if "sign in to confirm" in joined.lower():
        if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).is_file():
            hint = "Cookies заданы, но YouTube всё равно блокирует — обновите cookies.txt."
        elif YTDLP_COOKIES_FROM_BROWSER:
            hint = (
                "YTDLP_COOKIES_FROM_BROWSER работает только на Mac с браузером. "
                "На сервере: экспортируйте cookies.txt и задайте "
                "YTDLP_COOKIES_FILE=/app/cookies.txt или YTDLP_COOKIES_B64."
            )
        else:
            hint = (
                "На сервере задайте YTDLP_COOKIES_B64 в env (base64 cookies.txt) "
                "или загрузите cookies.txt в /app/data/cookies.txt. "
                "Проверьте логи при старте: yt-dlp cookies=… "
                "На Mac: YTDLP_COOKIES_FROM_BROWSER=chrome."
            )
        raise DownloadError(f"YouTube требует вход (антибот). {hint}")
    return None


def _pick_downloaded_file(tmp_dir: Path) -> Optional[Path]:
    files = sorted(
        [
            f
            for f in tmp_dir.glob("*")
            if f.is_file() and f.suffix.lower() in {".mp3", ".m4a", ".webm", ".opus", ".ogg", ".wav", ".aac"}
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in files:
        if path.stat().st_size >= 1000:
            return path
    return None


def _finalize_file(path: Path, query: str, tmp_dir: Path) -> DownloadedAudio:
    if path.suffix.lower() != ".mp3" and _has_ffmpeg():
        converted = _try_to_mp3(path)
        if converted is not None:
            path = converted

    if path.stat().st_size > MAX_AUDIO_BYTES:
        compressed = _try_compress(path)
        if compressed is not None:
            path = compressed
        if path.stat().st_size > MAX_AUDIO_BYTES:
            raise DownloadError("Файл слишком большой для Telegram (>50 МБ).")

    stem = path.stem
    artist, title = "", stem
    if " - " in stem:
        artist, title = stem.split(" - ", 1)

    final = tmp_dir / f"{_safe_name(artist or 'track')} - {_safe_name(title)}{path.suffix or '.mp3'}"
    try:
        if path != final:
            path.replace(final)
            path = final
    except OSError:
        pass

    data = path.read_bytes()
    if not data:
        raise DownloadError("Скачанный файл пустой.")

    duration = _probe_duration_sec(path)
    # крошечный файл ≈ превью / обрыв
    if duration is not None and duration < _MIN_FULL_TRACK_SEC:
        raise DownloadError(
            f"Скачалось слишком коротко ({duration}с) — похоже на превью, не трек."
        )
    if len(data) < 350_000 and (duration is None or duration < 90):
        raise DownloadError("Файл слишком маленький — похоже на превью, не полный трек.")

    logger.info(
        "Download OK: %s (%d bytes, %ss)",
        path.name,
        len(data),
        duration or "?",
    )
    return DownloadedAudio(
        path=path,
        title=title or query,
        artist=artist,
        duration=duration,
        data=data,
    )


async def _vevioz_fallback_async(
    query: str, tmp_dir: Path
) -> Optional[DownloadedAudio]:
    """Fallback: лучший YouTube id → api.vevioz.com → aiohttp download."""
    video_id = ""
    title = query
    artist = ""
    try:
        url = await _resolve_best_youtube(artist="", title=query, query=query)
        if url and "v=" in url:
            video_id = url.split("v=", 1)[1].split("&", 1)[0]
            title = query
    except Exception as exc:  # noqa: BLE001
        logger.warning("vevioz resolve: %s", exc)
        return None

    if not video_id:
        return None

    session = await get_session()
    for try_url in (
        f"https://api.vevioz.com/download/{video_id}/mp3",
        f"https://api.vevioz.com/api/button/mp3/{video_id}",
    ):
        try:
            async with session.get(
                try_url,
                headers={"User-Agent": UA, "Accept": "*/*"},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as resp:
                if resp.status != 200:
                    continue
                ctype = (resp.headers.get("Content-Type") or "").lower()
                data = await resp.read()
            if "text/html" in ctype or len(data) < 50_000:
                continue
            out = tmp_dir / f"{_safe_name(artist)} - {_safe_name(title)}.mp3"
            out.write_bytes(data)
            if out.stat().st_size > MAX_AUDIO_BYTES:
                compressed = await asyncio.to_thread(_try_compress, out)
                if compressed:
                    out = compressed
            if out.stat().st_size > MAX_AUDIO_BYTES:
                return None
            logger.info("vevioz OK: %s (%d)", out.name, out.stat().st_size)
            return DownloadedAudio(
                path=out,
                title=title,
                artist=artist,
                data=out.read_bytes(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("vevioz %s: %s", try_url, exc)
    return None


async def _itunes_preview_async(
    query: str,
    tmp_dir: Path,
    *,
    artist: str = "",
    title: str = "",
) -> Optional[DownloadedAudio]:
    """Последний шанс: 30-сек превью iTunes — только при совпадении artist/title."""
    session = await get_session()
    url = (
        "https://itunes.apple.com/search"
        f"?term={quote_plus(query)}&media=music&entity=song&limit=8"
        "&country=ru"
    )
    try:
        async with session.get(
            url,
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                return None
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.warning("itunes search: %s", exc)
        return None

    results = payload.get("results") or []
    item = None
    for row in results:
        row_title = (row.get("trackName") or "").strip()
        row_artist = (row.get("artistName") or "").strip()
        if title and _token_hit_ratio(row_title, title) < 0.6:
            continue
        if artist and _token_hit_ratio(row_artist, artist) < 0.5:
            continue
        if row.get("previewUrl"):
            item = row
            break
    if not item:
        return None

    preview = (item.get("previewUrl") or "").strip()
    title_out = (item.get("trackName") or title or query).strip()
    artist_out = (item.get("artistName") or artist).strip()
    try:
        async with session.get(
            preview,
            headers={"User-Agent": UA},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001
        logger.warning("itunes preview download: %s", exc)
        return None

    if len(data) < 1000:
        return None

    raw = tmp_dir / f"{_safe_name(artist_out)} - {_safe_name(title_out)}.m4a"
    raw.write_bytes(data)
    path = raw
    if _has_ffmpeg():
        converted = await asyncio.to_thread(_try_to_mp3, raw)
        if converted is not None:
            path = converted

    logger.info("itunes preview OK: %s (%d)", path.name, path.stat().st_size)
    return DownloadedAudio(
        path=path,
        title=f"{title_out} (превью 30с)",
        artist=artist_out,
        duration=30,
        data=path.read_bytes(),
    )


def _try_to_mp3(path: Path) -> Optional[Path]:
    ff = _ffmpeg_bin()
    if ff:
        out = path.with_suffix(".mp3")
        try:
            import subprocess

            r = subprocess.run(
                [
                    ff,
                    "-y",
                    "-i",
                    str(path),
                    "-vn",
                    "-acodec",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(out),
                ],
                capture_output=True,
                timeout=120,
                check=False,
            )
            if r.returncode == 0 and out.exists() and out.stat().st_size > 1000:
                return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("ffmpeg to_mp3: %s", exc)

    try:
        from pydub import AudioSegment  # type: ignore
    except ImportError:
        return None
    try:
        audio = AudioSegment.from_file(path)
        out = path.with_suffix(".mp3")
        audio.export(out, format="mp3", bitrate="192k")
        if out.exists() and out.stat().st_size > 1000:
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("to_mp3: %s", exc)
    return None


def _try_compress(path: Path) -> Optional[Path]:
    try:
        from pydub import AudioSegment  # type: ignore
    except ImportError:
        return None
    try:
        audio = AudioSegment.from_file(path)
        out = path.with_suffix(".128.mp3")
        audio.export(out, format="mp3", bitrate="128k")
        if out.exists() and out.stat().st_size < path.stat().st_size:
            return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("compress: %s", exc)
    return None


def _pack_zips(
    files: list[DownloadedAudio],
    *,
    artist: str,
    album: str,
) -> list[Path]:
    if not files:
        return []
    base_dir = files[0].path.parent
    prefix = f"{_safe_name(artist)} - {_safe_name(album or 'album')}"
    out = base_dir / f"{prefix}.zip"
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for audio in files:
            zf.write(audio.path, arcname=audio.path.name)
    if out.stat().st_size > MAX_AUDIO_BYTES:
        try:
            out.unlink(missing_ok=True)
        except OSError:
            pass
        return []
    return [out]


def cleanup_download(path: Path) -> None:
    try:
        parent = path.parent
        if path.exists():
            path.unlink()
        if parent.exists() and parent.name.startswith("tgmusic_"):
            for f in parent.glob("*"):
                try:
                    f.unlink()
                except OSError:
                    pass
            parent.rmdir()
    except OSError as exc:
        logger.debug("cleanup: %s", exc)


def cleanup_album_files(
    zip_paths: Sequence[Path],
    audios: Optional[Sequence[DownloadedAudio]] = None,
) -> None:
    for z in zip_paths:
        try:
            parent = z.parent
            if z.exists():
                z.unlink()
            if parent.exists() and parent.name.startswith("tgmusic_"):
                for f in parent.glob("*"):
                    try:
                        f.unlink()
                    except OSError:
                        pass
                try:
                    parent.rmdir()
                except OSError:
                    pass
        except OSError as exc:
            logger.debug("cleanup zip: %s", exc)
    if audios:
        for a in audios:
            cleanup_download(a.path)
