"""
Telegram Music Bot — полное меню: текст, обложка, мелодия, Genius-топы, скачивание.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote_plus

import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.token import validate_token

import config
from cache import close_session
from charts import (
    ChartsError,
    ChartSong,
    fetch_new_releases,
    format_chart_text,
    get_genius_charts,
)
from config import (
    ALBUMS_PER_PAGE,
    CHART_PER_PAGE,
    DEFAULT_COUNTRY,
    OCR_LANGUAGE,
    OCR_SPACE_API_KEY,
    PHOTO_SEARCH_TIMEOUT,
    TELEGRAM_PROXY,
    require_core_env,
)
from download import (
    AUDIO_CAPTION,
    CLOSED_DOWNLOAD_NOTICE,
    DownloadError,
    YoutubeHit,
    cleanup_album_files,
    cleanup_download,
    download_album_as_zip,
    download_single_track,
    download_track,
    parse_artist_title_from_yt,
    probe_album_free_download,
    probe_single_track_free_download,
    search_youtube_tracks,
    show_track_selection,
)
from keyboards import (
    BTN_HELP,
    BTN_RECOMMEND,
    BTN_SETTINGS,
    BTN_START,
    albums_page_kb,
    artists_kb,
    back_to_menu_kb,
    chart_page_kb,
    choose_playlist_kb,
    country_kb,
    get_main_reply_keyboard,
    lyrics_page_kb,
    main_menu_kb,
    pages_count,
    pick_recent_for_playlist_kb,
    platform_links_kb,
    unavailable_free_kb,
    query_type_kb,
    recent_playlist_kb,
    recommendations_kb,
    settings_kb,
    user_playlist_tracks_kb,
    user_playlists_kb,
    youtube_results_kb,
)
from links import (
    build_platform_links,
    follow_redirects,
    resolve_any_url_meta,
    soundcloud_search_tracks,
    soundcloud_url_meta,
    spotify_lookup,
    spotify_url_meta,
)
from lyrics import (
    LyricHit,
    LyricsError,
    resolve_genius_url,
    search_by_lyrics,
)
from music import (
    AlbumCandidate,
    MusicAPIError,
    TrackCandidate,
    albums_by_artist_id,
    artist_button_label,
    enrich_artists,
    filter_artist_own_releases,
    filter_discography,
    find_album_by_cover,
    find_album_via_artist_discography,
    format_album_caption,
    format_album_header,
    format_tracklist,
    lookup_album_details,
    lookup_itunes_id,
    lookup_itunes_song,
    pick_dominant_artist,
    search_artists,
    search_text_split,
    split_albums_singles,
)
from ocr import OCRError, clean_ocr_text, recognize_cover
from playlists import (
    add_recent_to_user_playlist,
    create_user_playlist,
    delete_from_playlist,
    delete_user_playlist,
    get_playlist_track,
    get_user_playlist,
    get_user_playlist_track,
    init_playlist_db,
    list_recent,
    list_user_playlist_tracks,
    list_user_playlists,
    recommendation_seeds,
    record_search,
    remove_from_user_playlist,
    save_to_playlist,
)
from states import SearchMode
from utils import (
    YOUTUBE_RE,
    classify_query,
    country_code,
    escape_html,
    extract_first_url,
    format_error,
    parse_music_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def safe_callback_answer(
    callback: CallbackQuery,
    text: str = "",
    **kwargs: Any,
) -> None:
    try:
        await callback.answer(text, **kwargs)
    except TelegramBadRequest as exc:
        if "query is too old" in str(exc).lower():
            logger.debug("callback answer skipped: %s", exc)
        else:
            raise

# --- In-memory caches (режим — в FSM) ---
_user_country: dict[int, str] = {}
_user_quality: dict[int, str] = {}  # "192" | "128"
# session_key -> {"albums": [...], "singles": [...]}
_album_sessions: dict[str, dict[str, list]] = {}
_chart_cache_mem: dict[str, list[ChartSong]] = {}
# session_key -> {artist, album, tracks, view_text, links, back_callback}
_download_sessions: dict[str, dict[str, Any]] = {}
# короткий ключ → текст запроса (для выбора artist/album)
_pending_queries: dict[str, str] = {}
# session → список LyricHit
_lyrics_sessions: dict[str, list[LyricHit]] = {}
# session → список YoutubeHit (поиск вне каталогов)
_youtube_sessions: dict[str, dict[str, Any]] = {}
# uid -> последний список поиска (для кнопки «Назад» с карточки альбома)
_user_list_nav: dict[int, dict[str, Any]] = {}

COUNTRY_CHOICES = ("ru", "us", "gb", "de", "fr", "jp", "br", "ua", "kz")

WELCOME = (
    "🎧 <b>PROJECT COVER</b>\n\n"
    "Просто пришлите в чат:\n"
    "• <b>название</b> артиста, альбома или трека\n"
    "• <b>ссылку</b> (Apple, Spotify, YouTube, Genius…)\n\n"
    "Кнопки меню — по желанию, искать можно сразу без них.\n"
    "<i>🖼 Поиск по обложке — пока в разработке.</i>"
)


def uid_of(message: Message | CallbackQuery) -> int:
    user = message.from_user
    return user.id if user else 0


def remember_list_nav(
    uid: int, *, kind: str, session_key: str, page: int = 0
) -> None:
    if uid:
        _user_list_nav[uid] = {
            "kind": kind,
            "session_key": session_key,
            "page": page,
        }


def list_back_callback(uid: int) -> str:
    nav = _user_list_nav.get(uid) or {}
    key = nav.get("session_key") or ""
    kind = nav.get("kind") or "alb"
    page = int(nav.get("page") or 0)
    if key and key in _album_sessions:
        return f"ap:{kind}:{key}:{page}"
    return "mode:menu"


async def ui_edit(
    message: Message,
    text: str,
    *,
    reply_markup: Any = None,
    disable_web_page_preview: bool = True,
) -> bool:
    """Редактирует текущее сообщение (текст или caption у фото)."""
    try:
        if message.photo:
            # Caption ограничен 1024 — длинные экраны лучше новым сообщением
            if len(text) > 1024:
                return False
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(
                text,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("ui_edit failed: %s", exc)
        return False


async def ui_show(
    message: Message,
    text: str,
    *,
    reply_markup: Any = None,
    edit: bool = True,
    disable_web_page_preview: bool = True,
) -> None:
    """Меняет экран на месте; если edit невозможен — шлёт новое сообщение."""
    if edit and await ui_edit(
        message,
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
    ):
        return
    # фото→длинный текст: удаляем карточку с обложкой, чтобы не плодить сообщения
    if edit and message.photo:
        try:
            await message.delete()
        except Exception:  # noqa: BLE001
            pass
    await message.answer(
        text,
        reply_markup=reply_markup,
        disable_web_page_preview=disable_web_page_preview,
    )


async def set_mode_fsm(state: FSMContext, mode: str) -> None:
    mapping = {
        "menu": SearchMode.menu,
        "text": SearchMode.text,
        "cover": SearchMode.cover,
        "tops": SearchMode.tops,
        "artist_filter": SearchMode.artist_filter,
        "link": SearchMode.link,
    }
    await state.set_state(mapping.get(mode, SearchMode.menu))


def get_country(uid: int, override: Optional[str] = None) -> str:
    if override:
        return country_code(override, DEFAULT_COUNTRY)
    return _user_country.get(uid, DEFAULT_COUNTRY)


def _menu_text(uid: int = 0) -> str:
    del uid  # регион больше не показываем в меню
    return WELCOME


def store_search_session(
    albums: list,
    singles: Optional[list] = None,
    *,
    artist_id: str = "",
    artist_name: str = "",
    all_releases: Optional[list] = None,
    back_callback: str = "mode:menu",
) -> str:
    key = uuid.uuid4().hex[:10]
    _album_sessions[key] = {
        "albums": albums,
        "singles": singles or [],
        "artist_id": artist_id,
        "artist_name": artist_name,
        "all_releases": all_releases or [],
        "back_callback": back_callback or "mode:menu",
    }
    return key


def store_albums(uid: int, albums: list[AlbumCandidate]) -> str:
    return store_search_session(albums, [])


def store_download_session(
    *,
    artist: str,
    album: str,
    tracks: list[dict[str, Any]],
    view_text: str = "",
    links: Optional[dict[str, str]] = None,
    back_callback: str = "",
    free_download: bool = True,
) -> str:
    """Сохраняет треклист и снимок экрана альбома для «Назад»."""
    key = uuid.uuid4().hex[:8]
    _download_sessions[key] = {
        "artist": artist,
        "album": album,
        "tracks": tracks,
        "view_text": view_text,
        "links": links or {},
        "back_callback": back_callback,
        "free_download": free_download,
        "unavailable_idx": set(),
    }
    if len(_download_sessions) > 400:
        for k in list(_download_sessions.keys())[:80]:
            _download_sessions.pop(k, None)
    return key


def _download_select_text(sess: dict[str, Any]) -> str:
    tracks = sess.get("tracks") or []
    return (
        f"⬇ <b>Выберите трек</b>\n"
        f"💿 {escape_html(sess.get('album') or '')}\n"
        f"👤 {escape_html(sess.get('artist') or '')}\n"
        f"Треков: <b>{len(tracks)}</b>"
    )


def _album_platform_kb(sess: dict[str, Any], dl_key: str) -> Any:
    links = sess.get("links") or {}
    free = bool(sess.get("free_download", True))
    artist = sess.get("artist") or ""
    album = sess.get("album") or ""
    return platform_links_kb(
        youtube=links.get("youtube", ""),
        spotify=links.get("spotify", ""),
        apple=links.get("apple", ""),
        yandex=links.get("yandex", ""),
        soundcloud=links.get("soundcloud", ""),
        preview=links.get("preview", ""),
        download_session=dl_key if free else "",
        download_locked=not free,
        youtube_search_query=f"{artist} {album}".strip(),
        back_callback=sess.get("back_callback") or "",
    )


def _format_download_error(exc: DownloadError) -> tuple[str, Any]:
    """Текст + клавиатура для ошибки скачивания."""
    if getattr(exc, "unavailable_free", False):
        art = exc.artist or ""
        tit = exc.title or ""
        body = (
            f"{CLOSED_DOWNLOAD_NOTICE}\n\n"
            f"<code>{escape_html(art)} — {escape_html(tit)}</code>"
        )
        return body, unavailable_free_kb(art, tit)
    return format_error(str(exc)), None


def _tracks_from_album_details(details: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in getattr(details, "tracks", None) or []:
        name = getattr(t, "track_name", "") or ""
        if not name:
            continue
        dur_ms = int(getattr(t, "duration_ms", 0) or 0)
        out.append(
            {
                "name": name,
                "artist": getattr(t, "artist_name", "") or details.artist_name,
                "number": getattr(t, "track_number", 0) or 0,
                "duration": (dur_ms // 1000) if dur_ms > 0 else 0,
            }
        )
    if not out:
        out.append(
            {
                "name": details.collection_name,
                "artist": details.artist_name,
                "number": 1,
                "duration": 0,
            }
        )
    return out


# ---------- commands ----------


async def reset_to_main_menu(message: Message, state: FSMContext) -> None:
    """Сброс любого незавершённого сценария → главное меню (reply + inline)."""
    uid = uid_of(message)
    await state.clear()
    await set_mode_fsm(state, "text")
    await message.answer(
        _menu_text(uid),
        reply_markup=get_main_reply_keyboard(),
    )
    await message.answer(
        "Пришлите название или ссылку — или выберите режим:",
        reply_markup=main_menu_kb(),
    )


async def cmd_start(message: Message, state: FSMContext) -> None:
    await reset_to_main_menu(message, state)


async def cmd_help(message: Message) -> None:
    await message.answer(
        "📖 <b>Справка</b>\n\n"
        "/start — главное меню\n"
        "/top — топ чарт Genius\n"
        "/newreleases — недельные релизы\n\n"
        "Режимы: название, ссылка, топ, релизы.\n"
        "🖼 Поиск по обложке — пока в разработке.\n"
        "После альбома — ссылки на платформы и ⬇ MP3 "
        "(выбор трека или ZIP).\n"
        "✨ Рекомендации — подборки по недавно скачанным трекам.\n\n"
        "Нижние кнопки: меню, настройки, рекомендации, справка.",
        reply_markup=main_menu_kb(),
    )


async def cmd_settings(message: Message) -> None:
    uid = uid_of(message)
    quality = _user_quality.get(uid, "192")
    await message.answer(
        "⚙️ <b>Настройки</b>\n"
        f"Качество MP3: <code>{quality} kbps</code>",
        reply_markup=get_main_reply_keyboard(),
    )
    await message.answer(
        "Выберите параметр:",
        reply_markup=settings_kb(quality=quality),
    )


async def cmd_playlists(message: Message) -> None:
    """Совместимость: старая кнопка «Плейлисты» → рекомендации."""
    uid = uid_of(message)
    await message.answer(
        "✨ <b>Рекомендации</b>",
        reply_markup=get_main_reply_keyboard(),
    )
    await show_recommendations(
        message, uid, country=get_country(uid)
    )


async def show_my_playlists(
    target: Message, user_id: int, *, edit: bool = False
) -> None:
    playlists = await list_user_playlists(user_id)
    text = (
        "🎛 <b>Мои плейлисты</b>\n\n"
        "Создайте плейлист и добавляйте треки из «Недавно скачанные» "
        "(кнопка 📁 у трека) или «➕ Из скачанных» внутри плейлиста."
    )
    if playlists:
        text += f"\n\nВсего: <b>{len(playlists)}</b>"
    kb = user_playlists_kb(playlists)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_user_playlist(
    target: Message,
    user_id: int,
    playlist_id: int,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    pl = await get_user_playlist(user_id, playlist_id)
    if not pl:
        text = format_error("Плейлист не найден.")
        kb = user_playlists_kb(await list_user_playlists(user_id))
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
        return
    per_page = 10
    tracks, total = await list_user_playlist_tracks(
        user_id, playlist_id, page=page, per_page=per_page
    )
    total_pages = pages_count(total, per_page)
    page = max(0, min(page, total_pages - 1))
    if not tracks:
        text = (
            f"📁 <b>{escape_html(pl.name)}</b>\n\n"
            "Пока пусто. Нажмите «➕ Из скачанных» или добавьте трек "
            "из «Недавно скачанные» кнопкой 📁."
        )
    else:
        lines = [
            f"📁 <b>{escape_html(pl.name)}</b> · {total} трек(ов)",
            f"Стр. {page + 1}/{total_pages}",
            "",
        ]
        for i, t in enumerate(tracks, start=page * per_page + 1):
            lines.append(
                f"<b>{i}.</b> {escape_html(t.track_name)} — "
                f"<i>{escape_html(t.artist)}</i>"
            )
        text = "\n".join(lines)
    kb = user_playlist_tracks_kb(
        tracks, playlist_id=playlist_id, page=page, total_pages=total_pages
    )
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_recommendations(
    target: Message, user_id: int, *, country: str, edit: bool = False
) -> None:
    artists, titles = await recommendation_seeds(user_id)
    if not artists and not titles:
        text = (
            "✨ <b>Рекомендации</b>\n\n"
            "Пока мало данных. Поищите альбомы/треки или скачайте MP3 — "
            "тогда появятся подборки по вашим вкусам."
        )
        kb = back_to_menu_kb()
        if edit:
            await target.edit_text(text, reply_markup=kb)
        else:
            await target.answer(text, reply_markup=kb)
        return

    lines = ["✨ <b>Рекомендации для вас</b>", ""]
    buttons: list[tuple[str, str]] = []

    def _store_q(q: str) -> str:
        key = uuid.uuid4().hex[:8]
        _pending_queries[key] = q
        if len(_pending_queries) > 400:
            for k in list(_pending_queries.keys())[:80]:
                _pending_queries.pop(k, None)
        return key

    # артисты из истории
    for art in artists[:5]:
        lines.append(f"🎤 Ещё от <b>{escape_html(art)}</b>")
        key = _store_q(art)
        buttons.append((f"🎤 {art}"[:64], f"pl:rart:{key}"))

    # похожие поиски по недавним трекам
    for art, tit in titles[:4]:
        q = f"{art} {tit}".strip()
        label = f"🔎 {tit}" if tit else f"🔎 {art}"
        lines.append(f"• похожее на «{escape_html(tit or art)}»")
        key = _store_q(q)
        buttons.append((label[:64], f"pl:rq:{key}"))

    # свежие релизы / топ как доп. точка входа
    buttons.append(("📊 Топ чарт", "mode:tops"))
    buttons.append(("🗓 Новые релизы", "mode:newreleases"))

    text = "\n".join(lines)
    kb = recommendations_kb(buttons)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def show_recent_playlist(
    target: Message, user_id: int, *, page: int = 0, edit: bool = False
) -> None:
    per_page = 10
    tracks, total = await list_recent(user_id, page=page, per_page=per_page)
    total_pages = pages_count(total, per_page)
    page = max(0, min(page, total_pages - 1))
    if not tracks:
        text = (
            "📂 <b>Недавно скачанные</b>\n\n"
            "Пока пусто. Скачайте трек кнопкой ⬇ Скачать MP3 — "
            "он появится в истории загрузок."
        )
        kb = back_to_menu_kb()
    else:
        lines = [
            f"📂 <b>Недавно скачанные</b> · {total} трек(ов)",
            f"Стр. {page + 1}/{total_pages}",
            "▶ слушать · 📁 в плейлист · 🗑 удалить",
            "",
        ]
        for i, t in enumerate(tracks, start=page * per_page + 1):
            lines.append(
                f"<b>{i}.</b> {escape_html(t.track_name)} — "
                f"<i>{escape_html(t.artist)}</i>"
            )
        text = "\n".join(lines)
        kb = recent_playlist_kb(tracks, page=page, total_pages=total_pages)
    if edit:
        await target.edit_text(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


async def cmd_country(message: Message, command: CommandObject) -> None:
    uid = uid_of(message)
    args = (command.args or "").strip().lower()
    if args:
        code = country_code(args, "")
        if len(code) != 2:
            await message.answer(format_error("Код страны: ru, us, gb…"))
            return
        _user_country[uid] = code
        await message.answer(
            f"🌍 Регион: <code>{code.upper()}</code>",
            reply_markup=main_menu_kb(),
        )
        return
    await message.answer(
        f"Текущий регион: <code>{get_country(uid).upper()}</code>",
        reply_markup=country_kb(get_country(uid), COUNTRY_CHOICES),
    )


async def cmd_top(message: Message, command: CommandObject, state: FSMContext) -> None:
    await set_mode_fsm(state, "tops")
    args = (command.args or "").strip().lower()
    country = args if args in {"global", "ru", "us", "gb", "de", "fr"} else "global"
    status = await message.answer("📊 Загружаю топ чарт…")
    await _show_genius(status, country)


async def cmd_newreleases(message: Message, command: CommandObject) -> None:
    uid = uid_of(message)
    args = (command.args or "").split()
    country = get_country(uid, args[0] if args else None)
    status = await message.answer(
        f"🗓 Загружаю недельные релизы (<code>{country.upper()}</code>)…"
    )
    await _show_new_releases(status, country)


# ---------- album presentation ----------


async def _present_albums_page(
    message: Message,
    albums: list[AlbumCandidate],
    *,
    page: int = 0,
    session_key: Optional[str] = None,
    uid: int = 0,
    header: str = "",
    edit: bool = False,
    kind: str = "alb",
    singles: Optional[list[TrackCandidate]] = None,
) -> None:
    """Один экран списка. Альбомы/синглы переключаются вкладками, без второго сообщения."""
    if singles is not None and session_key is None:
        if not albums and not singles:
            await ui_show(
                message,
                format_error("Ничего не найдено."),
                reply_markup=back_to_menu_kb(),
                edit=edit,
            )
            return
        key = store_search_session(albums, singles)
        if len(albums) == 1 and not singles:
            await _send_album_details(
                message, albums[0], country=get_country(uid), uid=uid
            )
            return
        show_kind = "alb" if albums else "sng"
        show_items: list = albums if show_kind == "alb" else singles  # type: ignore[assignment]
        await _render_list_page(
            message,
            show_items,
            page=0,
            session_key=key,
            header=header
            or ("🎵 <b>Альбомы</b>" if show_kind == "alb" else "🎶 <b>Синглы</b>"),
            edit=edit,
            kind=show_kind,
            uid=uid,
        )
        return

    items: list = albums
    if kind == "sng" and session_key and session_key in _album_sessions:
        items = _album_sessions[session_key].get("singles") or []
    elif kind == "alb" and session_key and session_key in _album_sessions:
        items = _album_sessions[session_key].get("albums") or albums

    if not items:
        await ui_show(
            message,
            format_error("Ничего не найдено."),
            reply_markup=back_to_menu_kb(),
            edit=edit,
        )
        return

    if kind == "alb" and len(items) == 1 and page == 0 and not edit and not session_key:
        await _send_album_details(
            message, items[0], country=get_country(uid), uid=uid
        )
        return

    key = session_key or store_search_session(
        items if kind == "alb" else [],
        items if kind == "sng" else [],
    )
    await _render_list_page(
        message,
        items,
        page=page,
        session_key=key,
        header=header
        or ("🎵 <b>Альбомы</b>" if kind == "alb" else "🎶 <b>Синглы</b>"),
        edit=edit,
        kind=kind,
        uid=uid,
    )


async def _render_list_page(
    message: Message,
    items: list,
    *,
    page: int,
    session_key: str,
    header: str,
    edit: bool,
    kind: str,
    uid: int = 0,
) -> None:
    total_pages = pages_count(len(items), ALBUMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * ALBUMS_PER_PAGE
    chunk = items[start : start + ALBUMS_PER_PAGE]
    sess = _album_sessions.get(session_key) or {}
    has_albums = bool(sess.get("albums"))
    has_singles = bool(sess.get("singles"))

    lines = [header, f"Всего: <b>{len(items)}</b> · стр. {page + 1}/{total_pages}"]
    lines.append("<i>Сортировка: сначала новые</i>\n")
    for i, a in enumerate(chunk, start=start + 1):
        # TrackCandidate имеет track_name; AlbumCandidate — collection_name
        track_name = getattr(a, "track_name", "") or ""
        collection = getattr(a, "collection_name", "") or ""
        artist = getattr(a, "artist_name", "") or ""
        release_date = getattr(a, "release_date", "") or ""
        release_year = getattr(a, "release_year", "") or ""
        if kind == "sng" and track_name and not getattr(a, "source_id", ""):
            year = release_date[:4] if release_date else ""
            y = f" ({year})" if year else ""
            alb = f" · {escape_html(collection)}" if collection else ""
            lines.append(
                f"<b>{i}.</b> {escape_html(track_name)}{escape_html(y)} — "
                f"<i>{escape_html(artist)}</i>{alb}"
            )
        else:
            title = collection or track_name
            year = release_year or (release_date[:4] if release_date else "")
            y = f" ({year})" if year else ""
            lines.append(
                f"<b>{i}.</b> {escape_html(title)}{escape_html(y)} — "
                f"<i>{escape_html(artist)}</i>"
            )
    text = "\n".join(lines)
    list_back = sess.get("back_callback") or "mode:menu"
    kb = albums_page_kb(
        chunk,
        page=page,
        total_pages=total_pages,
        session_key=session_key,
        kind=kind,
        has_albums=has_albums,
        has_singles=has_singles,
        show_disc_search=bool(sess.get("artist_id") or sess.get("all_releases")),
        back_callback=list_back,
    )
    remember_list_nav(uid or uid_of(message), kind=kind, session_key=session_key, page=page)
    await ui_show(
        message,
        text,
        reply_markup=kb,
        edit=edit,
        disable_web_page_preview=True,
    )


async def _ensure_itunes_artist(
    artist: dict[str, Any], *, country: str
) -> Optional[dict[str, Any]]:
    """Гарантирует numeric iTunes artist_id (Spotify id → поиск в iTunes)."""
    from parser import artist_names_match

    aid = str(artist.get("artist_id") or "")
    if aid.isdigit():
        return artist
    name = (artist.get("artist_name") or "").strip()
    if not name:
        return None
    try:
        hits = await search_artists(
            name, country=country, limit=5, allow_spotify=False
        )
    except MusicAPIError:
        hits = []
    for h in hits:
        if str(h.get("artist_id") or "").isdigit() and artist_names_match(
            name, h.get("artist_name") or ""
        ):
            return h
    for h in hits:
        if str(h.get("artist_id") or "").isdigit():
            return h
    return None


async def _show_artist_discography(
    message: Message,
    *,
    artist_id: str,
    artist_name: str,
    country: str,
    uid: int,
    edit: bool = True,
    filter_query: str = "",
) -> None:
    """Один экран дискографии: вкладки альбомы/синглы + опциональный фильтр."""
    # склеиваем RU + US — иначе новые зарубежные альбомы часто пустые в RU
    releases_a = await albums_by_artist_id(artist_id, country=country, limit=50)
    releases_b: list[AlbumCandidate] = []
    if country.lower() != "us":
        releases_b = await albums_by_artist_id(
            artist_id, country="us", limit=50
        )
    seen_ids: set[str] = set()
    releases: list[AlbumCandidate] = []
    for pool in (releases_a, releases_b):
        for a in pool:
            sid = str(a.source_id or "")
            key = sid or f"{a.collection_name.casefold()}|{a.artist_name.casefold()}"
            if key in seen_ids:
                continue
            seen_ids.add(key)
            releases.append(a)
    releases.sort(key=lambda a: a.sort_date_key, reverse=True)
    releases = filter_artist_own_releases(releases, artist_name)
    if filter_query:
        releases = filter_discography(releases, filter_query)
    if not releases:
        await ui_show(
            message,
            format_error(
                f"У «{escape_html(artist_name)}» ничего не найдено"
                + (f" по «{escape_html(filter_query)}»" if filter_query else "")
                + "."
            ),
            reply_markup=back_to_menu_kb(),
            edit=edit,
        )
        return

    albums, singles = split_albums_singles(releases)
    # если фильтр оставил 1 релиз — сразу карточка
    if filter_query and len(releases) == 1:
        await _send_album_details(
            message, releases[0], country=country, uid=uid
        )
        return

    key = store_search_session(
        albums,
        singles,
        artist_id=artist_id,
        artist_name=artist_name,
        all_releases=releases,
    )
    header = f"💿 <b>{escape_html(artist_name)}</b>"
    if filter_query:
        header += f" · «{escape_html(filter_query[:40])}»"
    show_kind = "alb" if albums else "sng"
    items = albums if show_kind == "alb" else singles
    await _render_list_page(
        message,
        items,
        page=0,
        session_key=key,
        header=header,
        edit=edit,
        kind=show_kind,
        uid=uid,
    )


def _find_album_in_sessions(source: str, source_id: str) -> Optional[AlbumCandidate]:
    sid = (source_id or "").strip()
    if not sid:
        return None
    for sess in _album_sessions.values():
        pools = (
            sess.get("albums") or [],
            sess.get("singles") or [],
            sess.get("all_releases") or [],
        )
        for pool in pools:
            for item in pool:
                if not isinstance(item, AlbumCandidate):
                    continue
                if str(item.source_id or "") != sid:
                    continue
                if item.source and item.source != source:
                    continue
                return item
    return None


async def _send_album_details(
    message: Message,
    album: AlbumCandidate,
    *,
    country: str,
    uid: int = 0,
    edit: bool = False,
) -> None:
    """Одно сообщение: шапка + треклист + кнопки. Без отдельного треклиста."""
    if not (album.source_id or "").strip():
        raise MusicAPIError("Нет ID альбома — повторите поиск.")

    details = None
    last_err: Optional[Exception] = None
    for cc in (country, "us", "ru"):
        if not cc:
            continue
        try:
            details = await lookup_album_details(
                album.source, album.source_id, country=cc
            )
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            logger.warning(
                "lookup_album_details %s/%s country=%s: %s",
                album.source,
                album.source_id,
                cc,
                exc,
            )
    if details is None:
        raise MusicAPIError(
            str(last_err) if last_err else "Альбом не найден в iTunes."
        )

    # превью: альбом → первый трек с previewUrl
    preview = (details.preview_url or "").strip()
    if not preview.startswith("http"):
        for tr in details.tracks or []:
            p = (getattr(tr, "preview_url", None) or "").strip()
            if p.startswith("http"):
                preview = p
                break

    try:
        links = await asyncio.wait_for(
            build_platform_links(
                artist=details.artist_name,
                title=details.collection_name,
                apple_url=details.collection_view_url,
                preview_url=preview,
                entity="album",
            ),
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("platform links: %s", exc)
        links = type(
            "L",
            (),
            {
                "youtube": "",
                "spotify": "",
                "apple_music": details.collection_view_url or "",
                "yandex": "",
                "soundcloud": "",
                "preview_url": details.preview_url or "",
            },
        )()

    back_cb = list_back_callback(uid or uid_of(message))
    tracks = _tracks_from_album_details(details)
    free_ok = True
    try:
        free_ok = await asyncio.wait_for(
            probe_album_free_download(
                details.artist_name, details.collection_name, tracks
            ),
            timeout=25,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe album free download: %s", exc)
        free_ok = True  # при сбое проверки не блокируем скачивание

    view_text = format_album_caption(details)
    if not free_ok:
        view_text = (
            f"{CLOSED_DOWNLOAD_NOTICE}\n\n{format_album_caption(details)}"
        )
    link_map = {
        "youtube": getattr(links, "youtube", "") or "",
        "spotify": getattr(links, "spotify", "") or "",
        "apple": getattr(links, "apple_music", "") or "",
        "yandex": getattr(links, "yandex", "") or "",
        "soundcloud": getattr(links, "soundcloud", "") or "",
        "preview": getattr(links, "preview_url", "") or "",
    }
    dl_key = store_download_session(
        artist=details.artist_name,
        album=details.collection_name,
        tracks=tracks,
        view_text=view_text,
        links=link_map,
        back_callback=back_cb,
        free_download=free_ok,
    )
    kb = platform_links_kb(
        youtube=link_map["youtube"],
        spotify=link_map["spotify"],
        apple=link_map["apple"],
        yandex=link_map["yandex"],
        soundcloud=link_map["soundcloud"],
        preview=link_map["preview"],
        download_session=dl_key if free_ok else "",
        download_locked=not free_ok,
        youtube_search_query=(
            f"{details.artist_name} {details.collection_name}".strip()
        ),
        back_callback=back_cb,
    )

    if uid:
        await record_search(
            uid,
            artist=details.artist_name,
            title=details.collection_name,
            kind="album",
        )

    def _photo_caption(full: str) -> str:
        """Caption к фото ≤ 1024 символов."""
        if len(full) <= 1024:
            return full
        header = format_album_header(details)
        room = 1024 - len(header) - 2
        if room < 80:
            return header[:1020] + "…"
        tl = format_tracklist(details, limit=max(80, room - 10))
        cap = header + "\n\n" + tl
        return cap if len(cap) <= 1024 else cap[:1020] + "…"

    async def _deliver(text: str) -> None:
        cover = (details.artwork_url or "").strip()
        if cover:
            caption = _photo_caption(text)
            media = InputMediaPhoto(media=cover, caption=caption)
            if edit:
                try:
                    await message.edit_media(media=media, reply_markup=kb)
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("edit_media cover: %s", exc)
                    # текст→фото иногда проще заменить новым сообщением
                    try:
                        await message.delete()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await message.answer_photo(
                            photo=cover, caption=caption, reply_markup=kb
                        )
                        return
                    except Exception as exc2:  # noqa: BLE001
                        logger.warning("answer_photo after delete: %s", exc2)
            else:
                try:
                    await message.answer_photo(
                        photo=cover, caption=caption, reply_markup=kb
                    )
                    return
                except Exception as exc:  # noqa: BLE001
                    logger.warning("cover: %s", exc)

        if edit:
            await ui_show(
                message,
                text,
                reply_markup=kb,
                edit=True,
                disable_web_page_preview=True,
            )
            return
        await message.answer(
            text, reply_markup=kb, disable_web_page_preview=True
        )

    try:
        await _deliver(view_text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("album deliver failed, plain fallback: %s", exc)
        plain = (
            f"🎧 <b>{escape_html(details.collection_name)}</b>\n"
            f"👤 {escape_html(details.artist_name)}\n"
            f"Треков: <b>{len(details.tracks)}</b>\n\n"
            + format_tracklist(details, limit=2500)
        )
        await _deliver(plain)


async def _send_track_result(
    message: Message,
    *,
    artist: str,
    title: str,
    album: str = "",
    cover: str = "",
    apple_url: str = "",
    country: str = "ru",
    edit: bool = False,
    duration_sec: int = 0,
) -> None:
    links = await build_platform_links(
        artist=artist,
        title=title,
        apple_url=apple_url,
        entity="track",
    )
    text = (
        f"🎵 <b>{escape_html(title)}</b>\n"
        f"👤 {escape_html(artist)}\n"
    )
    if album:
        text += f"💿 {escape_html(album)}\n"
    back_cb = list_back_callback(uid_of(message))
    link_map = {
        "youtube": links.youtube or "",
        "spotify": links.spotify or "",
        "apple": (links.apple_music or apple_url) or "",
        "yandex": links.yandex or "",
        "soundcloud": links.soundcloud or "",
        "preview": links.preview_url or "",
    }
    track_row: dict[str, Any] = {"name": title, "artist": artist, "number": 1}
    if duration_sec and duration_sec > 0:
        track_row["duration"] = int(duration_sec)

    free_ok = True
    try:
        free_ok = await asyncio.wait_for(
            probe_single_track_free_download(
                artist,
                title,
                album=album,
                expected=duration_sec or None,
            ),
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe track free download: %s", exc)
        free_ok = True

    if not free_ok:
        text = f"{CLOSED_DOWNLOAD_NOTICE}\n\n{text}"

    dl_key = store_download_session(
        artist=artist,
        album=album or title,
        tracks=[track_row],
        view_text=text,
        links=link_map,
        back_callback=back_cb,
        free_download=free_ok,
    )
    kb = platform_links_kb(
        youtube=link_map["youtube"],
        spotify=link_map["spotify"],
        apple=link_map["apple"],
        yandex=link_map["yandex"],
        soundcloud=link_map["soundcloud"],
        preview=link_map["preview"],
        download_session=dl_key if free_ok else "",
        download_locked=not free_ok,
        youtube_search_query=f"{artist} {title}".strip(),
        back_callback=back_cb,
    )

    await record_search(
        uid_of(message), artist=artist, title=title, kind="track"
    )

    if cover and not edit and len(text) <= 1024:
        try:
            await message.answer_photo(photo=cover, caption=text, reply_markup=kb)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("track cover: %s", exc)

    await ui_show(message, text, reply_markup=kb, edit=edit)


# ---------- search handlers ----------


async def _artists_picker_kb(artists: list[dict], *, country: str) -> Any:
    if artists and all("catalog_score" in a for a in artists):
        enriched = sorted(
            artists,
            key=lambda r: (
                -int(r.get("catalog_score") or 0),
                r.get("artist_name") or "",
            ),
        )[:8]
    else:
        enriched = await enrich_artists(artists[:8], country=country)
    # слабые дубликаты (0–1 релиз) прячем, если есть сильный каталог
    strong = [a for a in enriched if int(a.get("album_count") or 0) >= 3]
    if len(strong) >= 1 and len(enriched) > 3:
        show = strong[:5]
    else:
        show = enriched[:5]
    return artists_kb(
        [(artist_button_label(a), f"ar:{a['artist_id']}") for a in show]
    )


async def _auto_or_pick_artist(
    status: Message,
    message: Message,
    artists: list[dict],
    *,
    country: str,
    uid: int,
    query: str = "",
) -> bool:
    """
    Один доминирующий артист → дискография.
    Несколько ≈ равных → пикер.
    Возвращает True, если ответ уже отправлен.
    """
    if not artists:
        return False
    enriched = await enrich_artists(artists[:8], country=country)
    winner = pick_dominant_artist(enriched, query=query)
    chosen = winner or (enriched[0] if len(enriched) == 1 else None)
    if chosen is not None and (
        winner is not None or len(enriched) == 1
    ):
        resolved = await _ensure_itunes_artist(chosen, country=country)
        if not resolved:
            return False
        await status.delete()
        await _show_artist_discography(
            message,
            artist_id=resolved["artist_id"],
            artist_name=resolved["artist_name"],
            country=country,
            uid=uid,
            edit=False,
        )
        return True
    await status.edit_text(
        "Найдено несколько артистов. Выберите (по свежему релизу):",
        reply_markup=await _artists_picker_kb(enriched, country=country),
    )
    return True


def _token_in_blob(token: str, blob: str) -> bool:
    """Короткие буквенные токены (≤2) — целое слово; иначе подстрока.

    «b»/«ye» не должны матчиться внутри Kalbim; «#1» остаётся подстрокой.
    """
    import re

    t = (token or "").lower().strip()
    b = (blob or "").lower()
    if not t or not b:
        return False
    # только чистые буквы/цифры короткой длины → word-boundary
    if len(t) <= 2 and re.fullmatch(r"[\w]+", t, flags=re.UNICODE):
        return bool(re.search(rf"(?i)\b{re.escape(t)}\b", b))
    return t in b


def _catalog_covers_query(query: str, split: Any) -> bool:
    """True, если каталог попал в запрос (артист + большинство слов названия)."""
    from parser import artist_names_match, expand_query_aliases

    raw_tokens = [t for t in (query or "").lower().split() if t]
    # токены исходного + транслита («ог»+«буда» и «og»+«buda»)
    token_sets = [raw_tokens]
    for v in expand_query_aliases(query)[:4]:
        toks = [t for t in v.lower().split() if t]
        if toks and toks not in token_sets:
            token_sets.append(toks)
    if not raw_tokens:
        return bool(getattr(split, "albums", None) or getattr(split, "singles", None))
    albums = getattr(split, "albums", None) or []
    singles = getattr(split, "singles", None) or []
    if not albums and not singles:
        return False

    def _covers(blob: str, tokens: list[str]) -> bool:
        if len(tokens) == 1:
            return _token_in_blob(tokens[0], blob)
        hits = sum(1 for t in tokens if _token_in_blob(t, blob))
        return hits == len(tokens) or (hits >= 2 and hits / len(tokens) >= 0.7)

    for a in albums:
        blob = f"{a.artist_name} {a.collection_name}"
        if artist_names_match(query, a.artist_name):
            return True
        for tokens in token_sets:
            if _covers(blob, tokens):
                return True
    for s in singles:
        blob = f"{s.artist_name} {s.track_name} {s.collection_name}"
        if artist_names_match(query, s.artist_name):
            return True
        for tokens in token_sets:
            if _covers(blob, tokens):
                return True
    return False


async def _present_youtube_page(
    message: Message,
    hits: list[YoutubeHit],
    *,
    query: str = "",
    page: int = 0,
    session_key: Optional[str] = None,
    edit: bool = False,
) -> None:
    key = session_key or uuid.uuid4().hex[:10]
    _youtube_sessions[key] = {"hits": hits, "query": query}
    if len(_youtube_sessions) > 200:
        for k in list(_youtube_sessions.keys())[:40]:
            _youtube_sessions.pop(k, None)

    total_pages = pages_count(len(hits), ALBUMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    text = (
        f"▶ <b>YouTube</b>"
        + (f" · «{escape_html(query[:40])}»" if query else "")
        + f"\nНайдено: <b>{len(hits)}</b> · стр. {page + 1}/{total_pages}\n"
        f"<i>Треки вне Apple/Spotify — выберите для скачивания.</i>"
    )
    kb = youtube_results_kb(
        hits, page=page, total_pages=total_pages, session_key=key
    )
    await ui_show(message, text, reply_markup=kb, edit=edit)


async def _search_youtube_fallback(
    status: Message, query: str, *, edit: bool = True
) -> bool:
    await ui_edit(
        status,
        f"▶ В каталогах пусто. Ищу на YouTube…\n«{escape_html(query[:80])}»",
    )
    try:
        hits = await search_youtube_tracks(query, limit=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("youtube fallback: %s", exc)
        hits = []
    if not hits:
        return False
    await _present_youtube_page(status, hits, query=query, edit=edit)
    return True


async def handle_text_search(
    message: Message,
    text: str,
    *,
    mode: Optional[str] = None,
) -> None:
    uid = uid_of(message)
    country = get_country(uid)
    text = (text or "").strip()
    if not text:
        return

    # ссылка в любом виде текста → единый resolve
    url = extract_first_url(text)
    if url:
        status = await message.answer("🔗 Разбираю ссылку…")
        try:
            await asyncio.wait_for(
                _handle_url(message, url, status, country=country),
                timeout=55,
            )
        except asyncio.TimeoutError:
            await status.edit_text(
                format_error(
                    "⏰ Разбор ссылки занял слишком долго.\n"
                    "Попробуйте ещё раз или пришлите название трека текстом."
                ),
                reply_markup=main_menu_kb(),
            )
        return

    kind = mode or classify_query(text)
    logger.info("classify_query(%r) → %s", text, kind)

    # явный поиск на YouTube (кнопка «Трек на YouTube» или mode=yt)
    if mode == "yt":
        status = await message.answer(
            f"▶ Ищу на YouTube…\n«{escape_html(text[:80])}»"
        )
        if await _search_youtube_fallback(status, text, edit=True):
            return
        await status.edit_text(
            format_error(
                f"На YouTube ничего не найдено для "
                f"<code>{escape_html(text[:200])}</code>."
            ),
            reply_markup=main_menu_kb(),
        )
        return

    # 2 слова вроде «ye nhh» — не спрашиваем artist/album, ищем сразу
    # (иначе легко уйти в чужой альбом «ye»)
    if kind == "ambiguous" and mode is None:
        kind = "both"
        mode = "both"

    status = await message.answer(
        f"🔍 Ищу «{escape_html(text[:80])}» в <code>{country.upper()}</code>…"
    )

    artists: list[dict] = []
    split = None
    try:
        # Сначала точное имя артиста (в т.ч. «huzzy b») — ДО combo/discog,
        # иначе «huzzy»+«b» ловит чужой сингл с буквой b внутри названия.
        from parser import artist_names_match, expand_query_aliases

        try:
            artists = await search_artists(text, country=country, limit=8)
        except MusicAPIError as exc:
            logger.warning("search_artists soft-fail: %s", exc)
            artists = []
        # og buda ↔ ог буда ↔ OG Buda
        exact_artists = [
            a
            for a in artists
            if artist_names_match(text, a["artist_name"])
        ]

        if exact_artists:
            if await _auto_or_pick_artist(
                status,
                message,
                exact_artists,
                country=country,
                uid=uid,
                query=text,
            ):
                return

        # 1–3 слова и первый результат — явный артист по транслиту
        if (
            artists
            and len(text.split()) <= 3
            and artist_names_match(text, artists[0]["artist_name"])
        ):
            if await _auto_or_pick_artist(
                status,
                message,
                artists[:5],
                country=country,
                uid=uid,
                query=text,
            ):
                return

        if kind == "artist" and len(artists) > 1:
            # только с iTunes id в кнопках
            pick: list[dict] = []
            for a in artists:
                r = await _ensure_itunes_artist(a, country=country)
                if r:
                    pick.append(r)
            if pick and await _auto_or_pick_artist(
                status,
                message,
                pick,
                country=country,
                uid=uid,
                query=text,
            ):
                return
        # combo / album: артист→дискография или трек (Flying Bird и т.п.)
        if kind in {"combo", "album", "both"} and len(text.split()) >= 2:
            via = await find_album_via_artist_discography(
                text, country=country, limit=10
            )
            if via and _catalog_covers_query(text, via):
                if via.exact and len(via.albums) == 1 and not via.singles:
                    await status.delete()
                    await _send_album_details(
                        message, via.albums[0], country=country, uid=uid
                    )
                    return
                if via.exact and not via.albums and len(via.singles) == 1:
                    s0 = via.singles[0]
                    await status.delete()
                    await _send_track_result(
                        message,
                        artist=s0.artist_name,
                        title=s0.track_name,
                        album=s0.collection_name,
                        cover=s0.artwork_url,
                        apple_url=s0.track_view_url,
                        country=country,
                    )
                    return
                await _present_albums_page(
                    status,
                    via.albums,
                    uid=uid,
                    header=(
                        (via.note + "\n" if via.note else "")
                        + f"🎵 <b>Результаты</b> · «{escape_html(text[:40])}»"
                    ),
                    singles=via.singles,
                    edit=True,
                )
                return

        # «артист трек» в режиме artist → ищем как трек, не дискографию наугад
        if kind == "artist" and len(text.split()) >= 2 and not exact_artists:
            kind = "both"

        search_mode = kind if kind in {"artist", "album", "combo", "both"} else "both"
        queries = expand_query_aliases(text) or [text]
        split = None
        for qtry in queries[:3]:
            split = await asyncio.wait_for(
                search_text_split(
                    qtry, limit=30, country=country, mode=search_mode
                ),
                timeout=PHOTO_SEARCH_TIMEOUT,
            )
            if _catalog_covers_query(qtry, split) or _catalog_covers_query(text, split):
                break
    except asyncio.TimeoutError:
        await status.edit_text(
            format_error("⏰ Таймаут. Сервер не отвечает. Попробуйте снова.")
        )
        return
    except MusicAPIError as exc:
        logger.warning("text search catalog: %s", exc)
        if await _search_youtube_fallback(status, text, edit=True):
            return
        await status.edit_text(
            format_error(
                f"{exc}\nПробую позже или введите ссылку / другое написание."
            ),
            reply_markup=main_menu_kb(),
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("text search: %s", exc)
        await status.edit_text(format_error("❌ Ошибка поиска. Попробуйте ещё раз."))
        return

    if split is None:
        split = type("S", (), {"albums": [], "singles": [], "exact": False, "note": ""})()

    # каталог не покрыл запрос (пример: ye nhh) → только YouTube, без мусора
    weak_catalog = not _catalog_covers_query(text, split)

    if weak_catalog or (not split.albums and not split.singles):
        if await _search_youtube_fallback(status, text, edit=True):
            return
        await status.edit_text(
            format_error(
                f"Ничего не найдено для <code>{escape_html(text[:200])}</code>.\n"
                "Нет в каталогах и на YouTube. Попробуйте ссылку или другое написание."
            ),
            reply_markup=main_menu_kb(),
        )
        return

    # один точный альбом → сразу карточка
    if split.exact and len(split.albums) == 1 and not split.singles:
        await status.delete()
        await _send_album_details(
            message, split.albums[0], country=country, uid=uid
        )
        return

    # один точный трек (сингл в каталоге) → карточка трека
    if split.exact and not split.albums and len(split.singles) == 1:
        s0 = split.singles[0]
        await status.delete()
        await _send_track_result(
            message,
            artist=s0.artist_name,
            title=s0.track_name,
            album=s0.collection_name,
            cover=s0.artwork_url,
            apple_url=s0.track_view_url,
            country=country,
        )
        return

    note = ""
    if not split.exact and split.note:
        note = f"{split.note}\n"
    header = f"{note}🎵 <b>Результаты</b> · «{escape_html(text[:40])}»"
    await _present_albums_page(
        status,
        split.albums[:3] if not split.exact else split.albums,
        uid=uid,
        header=header,
        singles=split.singles[:3] if not split.exact else split.singles,
        edit=True,
    )


async def _present_lyrics_page(
    message: Message,
    hits: list[LyricHit],
    *,
    query: str = "",
    page: int = 0,
    session_key: Optional[str] = None,
    edit: bool = False,
) -> None:
    key = session_key or uuid.uuid4().hex[:10]
    _lyrics_sessions[key] = hits
    if len(_lyrics_sessions) > 200:
        for k in list(_lyrics_sessions.keys())[:40]:
            _lyrics_sessions.pop(k, None)

    total_pages = pages_count(len(hits), ALBUMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    text = (
        f"🔤 <b>По тексту песни</b>"
        + (f" · «{escape_html(query[:40])}»" if query else "")
        + f"\nНайдено: <b>{len(hits)}</b> · стр. {page + 1}/{total_pages}"
    )
    kb = lyrics_page_kb(
        hits, page=page, total_pages=total_pages, session_key=key
    )
    await ui_show(message, text, reply_markup=kb, edit=edit)


async def handle_cover_search(message: Message, bot: Bot) -> None:
    """Один точный альбом по обложке (интернет + OCR + сверка artwork)."""
    uid = uid_of(message)
    country = get_country(uid)
    status = await message.answer("поиск по обложке")

    try:
        if message.photo:
            file = await bot.get_file(message.photo[-1].file_id)
        elif message.document and (message.document.mime_type or "").startswith(
            "image/"
        ):
            file = await bot.get_file(message.document.file_id)
        else:
            await status.edit_text(format_error("Пришлите изображение обложки."))
            return
        buf = await bot.download_file(file.file_path)
        image_bytes = buf.read()
    except Exception as exc:  # noqa: BLE001
        logger.exception("photo: %s", exc)
        await status.edit_text(format_error("Не удалось скачать фото."))
        return

    from cover_search import find_albums_from_cover_image
    from image_prep import prepare_cover_image

    image_bytes = await asyncio.to_thread(prepare_cover_image, image_bytes)

    try:
        result = await asyncio.wait_for(
            find_albums_from_cover_image(
                image_bytes,
                country=country,
                limit=1,
                ocr_api_key=OCR_SPACE_API_KEY,
                ocr_language=OCR_LANGUAGE,
            ),
            timeout=PHOTO_SEARCH_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("cover search: %s", exc)
        await status.edit_text(format_error("Ошибка поиска по обложке."))
        return

    album = result.best
    if album is None:
        await status.edit_text(
            format_error(
                "Не удалось точно определить альбом по обложке.\n"
                "Попробуйте другое фото или введите название вручную."
            ),
            reply_markup=main_menu_kb(),
        )
        return

    await status.delete()
    await _send_album_details(message, album, country=country, uid=uid)


async def _resolve_query_to_card(
    message: Message,
    status: Message,
    query: str,
    *,
    country: str,
    uid: int,
    prefer: str = "album",
    artist: str = "",
    title: str = "",
    cover: str = "",
) -> bool:
    """Общий финал: query → альбом/трек карточка. True если показали результат."""
    q = (query or "").strip()
    if not q and artist and title:
        q = f"{artist} {title}".strip()
    if not q:
        return False

    try:
        via = await find_album_via_artist_discography(q, country=country, limit=8)
    except MusicAPIError as exc:
        logger.warning("resolve via discog: %s", exc)
        via = None
    if via and via.albums:
        if len(via.albums) == 1 or via.exact:
            await status.delete()
            await _send_album_details(
                message, via.albums[0], country=country, uid=uid
            )
            return True
        await _present_albums_page(
            status,
            via.albums,
            uid=uid,
            header=f"🔗 <b>По ссылке</b> · «{escape_html(q[:40])}»",
            singles=via.singles,
            edit=True,
        )
        return True
    if via and via.singles:
        s0 = via.singles[0]
        await status.delete()
        await _send_track_result(
            message,
            artist=s0.artist_name or artist,
            title=s0.track_name or title,
            album=s0.collection_name,
            cover=s0.artwork_url or cover,
            apple_url=s0.track_view_url,
            country=country,
        )
        return True

    try:
        split = await search_text_split(q, limit=15, country=country, mode="both")
    except MusicAPIError as exc:
        logger.warning("resolve search_text_split: %s", exc)
        split = type("S", (), {"albums": [], "singles": [], "exact": False, "note": ""})()
    if split.albums:
        if len(split.albums) == 1:
            await status.delete()
            await _send_album_details(
                message, split.albums[0], country=country, uid=uid
            )
            return True
        await _present_albums_page(
            status,
            split.albums,
            uid=uid,
            header=f"🔗 <b>По ссылке</b>",
            singles=split.singles,
            edit=True,
        )
        return True
    if split.singles:
        s0 = split.singles[0]
        await status.delete()
        await _send_track_result(
            message,
            artist=s0.artist_name or artist,
            title=s0.track_name or title,
            album=s0.collection_name,
            cover=s0.artwork_url or cover,
            apple_url=s0.track_view_url,
            country=country,
        )
        return True

    if prefer == "track" or (artist and title):
        await status.delete()
        await _send_track_result(
            message,
            artist=artist or q,
            title=title or q,
            cover=cover,
            country=country,
        )
        return True
    return False


async def _handle_url(
    message: Message, url: str, status: Message, *, country: str
) -> None:
    uid = uid_of(message)
    # short-links → полный URL, затем повторный parse
    resolved_url = await follow_redirects(url)
    parsed = parse_music_url(resolved_url) or parse_music_url(url)
    if parsed and parsed.platform == "unknown":
        # ещё раз после redirect (spotify.link и т.п.)
        parsed = parse_music_url(resolved_url)
    if not parsed or parsed.platform == "unknown":
        meta = await resolve_any_url_meta(resolved_url or url)
        if meta and meta.query:
            ok = await _resolve_query_to_card(
                message,
                status,
                meta.query,
                country=country,
                uid=uid,
                prefer=meta.prefer,
                artist=meta.artist,
                title=meta.title,
                cover=meta.cover,
            )
            if ok:
                return
            await status.delete()
            await _send_track_result(
                message,
                artist=meta.artist or meta.query,
                title=meta.title or meta.query,
                cover=meta.cover,
                country=country,
            )
            return
        await status.edit_text(
            format_error(
                "Не распознал ссылку. Поддержка: Apple, Spotify, "
                "Яндекс, YouTube, Genius, SoundCloud."
            )
        )
        return
    url = resolved_url or url
    try:
        # --- Apple Music / iTunes ---
        if parsed.platform == "itunes":
            if parsed.entity_id:
                # ссылка на конкретный трек (?i= /song/)
                if parsed.entity == "track":
                    song = await lookup_itunes_song(
                        parsed.entity_id, country=country
                    )
                    if song:
                        await status.delete()
                        await _send_track_result(
                            message,
                            artist=song.artist_name,
                            title=song.track_name,
                            album=song.collection_name,
                            cover=song.artwork_url,
                            apple_url=song.track_view_url,
                            country=country,
                            duration_sec=(song.duration_ms // 1000)
                            if song.duration_ms
                            else 0,
                        )
                        return
                albums = await lookup_itunes_id(
                    parsed.entity_id, country=country
                )
                if albums:
                    if len(albums) == 1:
                        await status.delete()
                        await _send_album_details(
                            message, albums[0], country=country, uid=uid
                        )
                    else:
                        await _present_albums_page(
                            status,
                            albums,
                            uid=uid,
                            header="🔗 Apple Music",
                            edit=True,
                        )
                    return
            # нет numeric id — пробуем вытащить из slug и найти текстом
            slug = re.sub(
                r"https?://[^/]+/|/id\d+.*$",
                " ",
                parsed.original,
                flags=re.I,
            )
            slug = re.sub(r"[/\-_?=&]+", " ", slug).strip()
            if slug:
                ok = await _resolve_query_to_card(
                    message,
                    status,
                    slug,
                    country=country,
                    uid=uid,
                    prefer="album" if parsed.entity != "track" else "track",
                )
                if ok:
                    return
            await status.edit_text(
                format_error("Не удалось открыть ссылку Apple Music.")
            )
            return

        # --- SoundCloud ---
        if parsed.platform == "soundcloud":
            meta = await soundcloud_url_meta(url)
            queries: list[str] = []
            if meta:
                if meta.query:
                    queries.append(meta.query)
                if meta.artist and meta.title:
                    queries.append(f"{meta.artist} {meta.title}")
                    queries.append(meta.title)
            slug = (parsed.entity_id or "").replace("-", " ").replace("/", " ")
            if slug and slug not in queries:
                queries.append(slug)
            for qtry in queries:
                try:
                    ok = await asyncio.wait_for(
                        _resolve_query_to_card(
                            message,
                            status,
                            qtry,
                            country=country,
                            uid=uid,
                            prefer="track",
                            artist=(meta.artist if meta else ""),
                            title=(meta.title if meta else ""),
                            cover=(meta.cover if meta else ""),
                        ),
                        timeout=30,
                    )
                except (asyncio.TimeoutError, MusicAPIError):
                    ok = False
                if ok:
                    return
            if meta and (meta.title or meta.query):
                await status.delete()
                await _send_track_result(
                    message,
                    artist=meta.artist or "SoundCloud",
                    title=meta.title or meta.query,
                    cover=meta.cover,
                    country=country,
                )
                return
            # артист / пустой oEmbed → поиск треков на SoundCloud
            sc_hits = await soundcloud_search_tracks(
                slug or (meta.query if meta else "") or url, limit=5
            )
            if sc_hits:
                h0 = sc_hits[0]
                ok = await _resolve_query_to_card(
                    message,
                    status,
                    h0["query"],
                    country=country,
                    uid=uid,
                    prefer="track",
                    artist=h0.get("artist", ""),
                    title=h0.get("title", ""),
                )
                if ok:
                    return
                await status.delete()
                await _send_track_result(
                    message,
                    artist=h0.get("artist") or "SoundCloud",
                    title=h0.get("title") or h0["query"],
                    country=country,
                )
                return
            if slug and await _search_youtube_fallback(status, slug, edit=True):
                return
            await status.edit_text(
                format_error("Не удалось открыть ссылку SoundCloud.")
            )
            return

        # --- Spotify ---
        if parsed.platform == "spotify":
            if parsed.entity in {"album", "track"} and parsed.entity_id:
                data = await spotify_lookup(parsed.entity, parsed.entity_id)
                if data and parsed.entity == "album":
                    cand = AlbumCandidate(
                        source="spotify",
                        source_id=parsed.entity_id,
                        artist_name=(
                            (data.get("artists") or [{}])[0].get("name") or ""
                        ),
                        collection_name=data.get("name") or "",
                        artwork_url=(
                            ((data.get("images") or [{}])[0]).get("url") or ""
                        ),
                        track_count=int(data.get("total_tracks") or 0),
                        release_date=(data.get("release_date") or "")[:10],
                        collection_view_url=(
                            (data.get("external_urls") or {}).get("spotify")
                            or ""
                        ),
                    )
                    ok = await _resolve_query_to_card(
                        message,
                        status,
                        f"{cand.artist_name} {cand.collection_name}",
                        country=country,
                        uid=uid,
                        prefer="album",
                        artist=cand.artist_name,
                        title=cand.collection_name,
                        cover=cand.artwork_url,
                    )
                    if ok:
                        return
                    await status.delete()
                    await _send_album_details(
                        message, cand, country=country, uid=uid
                    )
                    return
                if data and parsed.entity == "track":
                    artists = data.get("artists") or []
                    art = (artists[0].get("name") if artists else "") or ""
                    title = data.get("name") or ""
                    alb = ((data.get("album") or {}).get("name")) or ""
                    cover = ""
                    imgs = (data.get("album") or {}).get("images") or []
                    if imgs:
                        cover = imgs[0].get("url") or ""
                    ok = await _resolve_query_to_card(
                        message,
                        status,
                        f"{art} {title}",
                        country=country,
                        uid=uid,
                        prefer="track",
                        artist=art,
                        title=title,
                        cover=cover,
                    )
                    if ok:
                        return
                    await status.delete()
                    await _send_track_result(
                        message,
                        artist=art,
                        title=title,
                        album=alb,
                        cover=cover,
                        country=country,
                    )
                    return
            # нет API-ключей / lookup пуст → oEmbed
            meta = await spotify_url_meta(url)
            if meta and meta.query:
                ok = await _resolve_query_to_card(
                    message,
                    status,
                    meta.query,
                    country=country,
                    uid=uid,
                    prefer=meta.prefer,
                    artist=meta.artist,
                    title=meta.title,
                    cover=meta.cover,
                )
                if ok:
                    return
                await status.delete()
                await _send_track_result(
                    message,
                    artist=meta.artist or "Spotify",
                    title=meta.title or meta.query,
                    cover=meta.cover,
                    country=country,
                )
                return
            if parsed.entity == "artist" and parsed.entity_id:
                await status.edit_text(
                    format_error(
                        "Ссылка на артиста Spotify: введите имя текстом "
                        "или откройте альбом/трек."
                    )
                )
                return
            await status.edit_text(
                format_error(
                    "Не удалось открыть Spotify. "
                    "Пришлите название или проверьте ссылку."
                )
            )
            return

        # --- Genius ---
        if parsed.platform == "genius":
            resolved = await resolve_genius_url(parsed)
            if not resolved:
                await status.edit_text(
                    format_error("Не удалось разобрать ссылку Genius.")
                )
                return
            if resolved.kind == "artist":
                arts = await search_artists(
                    resolved.artist, country=country, limit=5
                )
                if arts and await _auto_or_pick_artist(
                    status,
                    message,
                    arts,
                    country=country,
                    uid=uid,
                    query=resolved.artist,
                ):
                    return
            prefer = "track" if resolved.kind == "track" else "album"
            ok = await _resolve_query_to_card(
                message,
                status,
                resolved.query,
                country=country,
                uid=uid,
                prefer=prefer,
                artist=resolved.artist,
                title=resolved.title or resolved.album,
                cover=resolved.cover_url,
            )
            if ok:
                return
            await status.edit_text(
                format_error(
                    f"Genius: нашёл «{escape_html(resolved.query)}», "
                    "но в каталоге совпадений нет."
                )
            )
            return

        # --- Yandex ---
        if parsed.platform == "yandex":
            meta = await _yandex_resolve(parsed)
            if meta:
                ok = await _resolve_query_to_card(
                    message,
                    status,
                    meta["query"],
                    country=country,
                    uid=uid,
                    prefer=meta.get("prefer", "album"),
                    artist=meta.get("artist", ""),
                    title=meta.get("title", ""),
                )
                if ok:
                    return
                if meta.get("artist") and meta.get("title"):
                    await status.delete()
                    await _send_track_result(
                        message,
                        artist=meta["artist"],
                        title=meta["title"],
                        country=country,
                    )
                    return
            await status.edit_text(
                format_error(
                    "Не удалось открыть Яндекс.Музыку. "
                    "Попробуйте название или YANDEX_MUSIC_TOKEN."
                )
            )
            return

        # --- YouTube ---
        if parsed.platform == "youtube":
            from utils import normalize_youtube_watch_url

            vid = (parsed.entity_id or "").strip()
            if not vid:
                await status.edit_text(
                    format_error(
                        "Не вижу video id в ссылке YouTube.\n"
                        "Нужен вид: https://www.youtube.com/watch?v=…"
                    )
                )
                return
            watch = normalize_youtube_watch_url(vid)
            try:
                meta = await asyncio.wait_for(
                    _youtube_meta(watch),
                    timeout=22,
                )
            except asyncio.TimeoutError:
                meta = {}
            full_title = (meta.get("title") or "").strip() or f"YouTube {vid}"
            art, tit = parse_artist_title_from_yt(
                full_title, (meta.get("artist") or "")
            )
            # Прямая ссылка = ЭТОТ ролик. Не ищем «похожий» в Apple —
            # иначе xaviersobased / underground уезжает в чужой трек.
            await status.delete()
            hit = YoutubeHit(
                video_id=vid,
                title=full_title,
                uploader=art or (meta.get("artist") or ""),
                duration=int(meta.get("duration") or 0),
                url=watch,
            )
            await message.answer(
                f"▶ <b>{escape_html(tit or full_title)}</b>\n"
                f"👤 {escape_html(art or hit.uploader or 'YouTube')}\n\n"
                f"<i>Скачаю именно этот ролик (не поиск по каталогам).</i>"
            )
            await _present_youtube_page(
                message,
                [hit],
                query=f"{art} {tit}".strip() or full_title,
                edit=False,
            )
            return

        await status.edit_text(format_error("Этот тип ссылки пока не поддерживается."))
    except Exception as exc:  # noqa: BLE001
        logger.exception("url handle: %s", exc)
        try:
            await status.edit_text(format_error(str(exc)))
        except Exception:  # noqa: BLE001
            pass

async def _yandex_resolve(parsed: Any) -> Optional[dict[str, str]]:
    """Достаёт artist/title из Яндекс.Музыки (token опционален)."""
    try:
        from yandex_music import Client

        from config import YANDEX_MUSIC_TOKEN

        client = (
            Client(YANDEX_MUSIC_TOKEN).init()
            if YANDEX_MUSIC_TOKEN
            else Client().init()
        )
        if parsed.entity == "album" and parsed.entity_id:
            alb = client.albums_with_tracks(int(parsed.entity_id))
            if isinstance(alb, list):
                alb = alb[0] if alb else None
            if not alb:
                return None
            artists = ", ".join(a.name for a in (alb.artists or []) if a.name)
            title = alb.title or ""
            return {
                "artist": artists,
                "title": title,
                "query": f"{artists} {title}".strip(),
                "prefer": "album",
            }
        if parsed.entity == "track" and parsed.entity_id:
            tracks = client.tracks([parsed.entity_id])
            tr = tracks[0] if tracks else None
            if not tr:
                return None
            artists = ", ".join(a.name for a in (tr.artists or []) if a.name)
            title = tr.title or ""
            return {
                "artist": artists,
                "title": title,
                "query": f"{artists} {title}".strip(),
                "prefer": "track",
            }
        if parsed.entity == "artist" and parsed.entity_id:
            arts = client.artists([int(parsed.entity_id)])
            ar = arts[0] if arts else None
            if not ar:
                return None
            name = ar.name or ""
            return {
                "artist": name,
                "title": "",
                "query": name,
                "prefer": "album",
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("yandex resolve: %s", exc)
    return None


async def _youtube_meta(video_id_or_url: str) -> dict[str, Any]:
    """Достаёт title/artist с YouTube: oEmbed → noembed → yt-dlp (с таймаутом)."""
    import shutil
    from pathlib import Path

    from cache import get_session
    from config import YTDLP_COOKIES_FILE, YTDLP_COOKIES_FROM_BROWSER

    raw = (video_id_or_url or "").strip()
    if not raw:
        return {}
    from utils import normalize_youtube_watch_url

    url = normalize_youtube_watch_url(raw)

    async def _from_oembed(oembed_url: str) -> dict[str, Any]:
        session = await get_session()
        async with session.get(
            oembed_url,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                    "Version/17.5 Safari/605.1.15"
                ),
                "Accept": "application/json",
            },
        ) as resp:
            if resp.status != 200:
                return {}
            data = await resp.json(content_type=None)
        title = (data.get("title") or "").strip()
        author = (data.get("author_name") or "").strip()
        if not title:
            return {}
        art, tit = parse_artist_title_from_yt(title, author)
        # если артист = канал-загрузчик, а в title уже есть имя — предпочесть title
        if art and author and art.casefold() == author.casefold():
            art2, tit2 = parse_artist_title_from_yt(title, "")
            if art2 and tit2 and art2.casefold() != author.casefold():
                art, tit = art2, tit2
        query = f"{art} {tit}".strip() if art and tit else title
        return {
            "title": title,
            "track": tit or title,
            "artist": art or author,
            "query": query,
            "url": url,
            "duration": 0,
        }

    # 1) YouTube oEmbed
    try:
        meta = await _from_oembed(
            "https://www.youtube.com/oembed"
            f"?url={quote_plus(url)}&format=json"
        )
        if meta:
            logger.info(
                "youtube oEmbed: %r / %r", meta.get("artist"), meta.get("track")
            )
            return meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("youtube oEmbed: %s", exc)

    # 2) noembed fallback
    try:
        meta = await _from_oembed(
            "https://noembed.com/embed?url=" + quote_plus(url)
        )
        if meta:
            logger.info(
                "youtube noembed: %r / %r", meta.get("artist"), meta.get("track")
            )
            return meta
    except Exception as exc:  # noqa: BLE001
        logger.warning("youtube noembed: %s", exc)

    # 3) yt-dlp — только с жёстким таймаутом; cookiesfrombrowser часто зависает на Mac
    def _extract() -> dict[str, Any]:
        try:
            import yt_dlp

            opts: dict[str, Any] = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "socket_timeout": 12,
                "extractor_args": {
                    "youtube": {"player_client": ["android", "web"]}
                },
                "remote_components": ["ejs:github"],
            }
            node = shutil.which("node")
            if node:
                opts["js_runtimes"] = {"node": {"path": node}}
            # для метаданных cookies из браузера НЕ трогаем — зависает
            if YTDLP_COOKIES_FILE and Path(YTDLP_COOKIES_FILE).is_file():
                opts["cookiefile"] = YTDLP_COOKIES_FILE
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False) or {}
            title = (info.get("track") or info.get("title") or "").strip()
            artist = (
                info.get("artist")
                or info.get("uploader")
                or info.get("channel")
                or ""
            ).strip()
            if artist.endswith(" - Topic"):
                artist = artist[: -len(" - Topic")].strip()
            art, tit = parse_artist_title_from_yt(title, artist)
            query = f"{art} {tit}".strip() if art and tit else title
            return {
                "title": title,
                "track": tit or title,
                "artist": art or artist,
                "query": query,
                "url": url,
                "duration": int(info.get("duration") or 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("yt-dlp title: %s", exc)
            return {}

    try:
        return await asyncio.wait_for(asyncio.to_thread(_extract), timeout=18)
    except asyncio.TimeoutError:
        logger.warning("yt-dlp title timeout for %s", url)
        _ = YTDLP_COOKIES_FROM_BROWSER  # referenced for config awareness
        return {}

async def _show_genius(target: Message, country: str = "global") -> None:
    country = (country or "global").lower()
    await ui_edit(target, "📊 Загружаю топ чарт…")
    try:
        songs, source = await get_genius_charts(country, limit=20)
        _chart_cache_mem[country] = songs
        page = 0
        total_pages = pages_count(len(songs), CHART_PER_PAGE)
        chunk = songs[0:CHART_PER_PAGE]
        note = ""
        if "Apple" in source or "fallback" in source.lower() or "недоступен" in source:
            note = (
                "\n\n⚠️ Не удалось загрузить чарты Genius. Использую Apple Music."
            )
        text = format_chart_text(
            chunk,
            title="📊 Топ чарт",
            country=country,
            source=source,
            page=1,
            total=len(songs),
        )
        await ui_show(
            target,
            text + note,
            reply_markup=chart_page_kb(
                chunk, page=page, total_pages=total_pages, country=country
            ),
            edit=True,
            disable_web_page_preview=True,
        )
    except ChartsError as exc:
        await ui_show(
            target,
            format_error(str(exc)),
            reply_markup=main_menu_kb(),
            edit=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("genius: %s", exc)
        await ui_show(
            target,
            format_error("❌ Не удалось загрузить топ."),
            reply_markup=main_menu_kb(),
            edit=True,
        )


async def _show_new_releases(target: Message, country: str) -> None:
    cc = (country or "ru").lower()
    await ui_edit(
        target, f"🗓 Загружаю недельные релизы (<code>{cc.upper()}</code>)…"
    )
    try:
        items, source = await fetch_new_releases(cc)
        lines = [
            f"<b>🗓 Недельные релизы</b> · <code>{cc.upper()}</code>",
            f"<i>{escape_html(source)}</i>",
            "",
        ]
        for it in items[:25]:
            name = escape_html(it.name)
            if it.url:
                name = f'<a href="{escape_html(it.url)}">{name}</a>'
            lines.append(
                f"<b>{it.position}.</b> {name} — "
                f"<i>{escape_html(it.artist)}</i> "
                f"<code>{escape_html(it.release_date)}</code>"
            )
        await ui_show(
            target,
            "\n".join(lines),
            reply_markup=back_to_menu_kb(),
            edit=True,
            disable_web_page_preview=True,
        )
    except ChartsError as exc:
        await ui_show(
            target,
            format_error(str(exc)),
            reply_markup=main_menu_kb(),
            edit=True,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("newreleases: %s", exc)
        await ui_show(
            target,
            format_error("❌ Не удалось загрузить релизы."),
            reply_markup=main_menu_kb(),
            edit=True,
        )


# ---------- message router ----------


def _is_reply_nav(text: str) -> bool:
    return text in {
        BTN_START,
        "🏠 Start",
        "Start",
        BTN_SETTINGS,
        "⚙️ Settings",
        "Settings",
        BTN_RECOMMEND,
        "Рекомендации",
        "✨ Рекомендации",
        "📂 Плейлисты",
        "📂 Playlists",
        "Playlists",
        "🎛 Мои плейлисты",
        "Мои плейлисты",
        "📂 Недавно скачанные",
        "Недавно скачанные",
        BTN_HELP,
        "❓ Help",
        "Help",
    }


async def on_message(message: Message, bot: Bot, state: FSMContext) -> None:
    current = await state.get_state()
    text = (message.text or "").strip() if message.text else ""

    # Нижнее меню всегда важнее текущего сценария (обложка / фильтр / поиск)
    if text and _is_reply_nav(text):
        if text in {BTN_START, "🏠 Start", "Start"}:
            await reset_to_main_menu(message, state)
            return
        await state.clear()
        await set_mode_fsm(state, "menu")
        if text in {BTN_SETTINGS, "⚙️ Settings", "Settings"}:
            await cmd_settings(message)
            return
        if text in {
            BTN_RECOMMEND,
            "Рекомендации",
            "✨ Рекомендации",
            "📂 Плейлисты",
            "📂 Playlists",
            "Playlists",
            "🎛 Мои плейлисты",
            "Мои плейлисты",
            "📂 Недавно скачанные",
            "Недавно скачанные",
        }:
            # старые кнопки плейлистов/недавних → рекомендации
            await message.answer(
                "✨ Рекомендации",
                reply_markup=get_main_reply_keyboard(),
            )
            await show_recommendations(
                message,
                uid_of(message),
                country=get_country(uid_of(message)),
            )
            return
        if text in {BTN_HELP, "❓ Help", "Help"}:
            await cmd_help(message)
            return

    if message.photo or (
        message.document
        and (message.document.mime_type or "").startswith("image/")
    ):
        await message.answer(
            "🖼 <b>Поиск по обложке</b> пока в разработке.\n"
            "Пришлите <b>название</b> или <b>ссылку</b> — так поиск работает сейчас.",
            reply_markup=main_menu_kb(),
        )
        return

    if message.voice or message.audio or (
        message.document
        and (
            (message.document.mime_type or "").startswith("audio/")
            or (message.document.file_name or "").lower().endswith(
                (".mp3", ".m4a", ".ogg", ".flac", ".wav")
            )
        )
    ):
        await message.answer(
            "Эта функция временно недоступна.\n"
            "Используйте поиск по названию или ссылке.",
            reply_markup=main_menu_kb(),
        )
        return

    if message.text and not message.text.startswith("/"):
        # создание плейлиста — ждём имя
        if current == SearchMode.playlist_name.state:
            name = text.strip()
            if len(name) < 1:
                await message.answer("Название слишком короткое. Попробуйте ещё раз.")
                return
            pl = await create_user_playlist(uid_of(message), name)
            await state.clear()
            await set_mode_fsm(state, "text")
            if not pl:
                await message.answer(
                    format_error(
                        "Не удалось создать (лимит плейлистов или пустое имя)."
                    ),
                    reply_markup=back_to_menu_kb(),
                )
                return
            await message.answer(
                f"✅ Плейлист «{escape_html(pl.name)}» создан.",
                reply_markup=get_main_reply_keyboard(),
            )
            await show_user_playlist(message, uid_of(message), pl.id, page=0)
            return

        # Ссылка или обычный текст — сразу поиск, без обязательных кнопок меню.
        # Исключение: режим фильтра дискографии (короткий запрос внутри артиста),
        # но ссылка / явный новый запрос всё равно идут в общий поиск.
        url = extract_first_url(text)
        if current == SearchMode.artist_filter.state and not url:
            data = await state.get_data()
            sess_key = data.get("disc_session") or ""
            sess = _album_sessions.get(sess_key) or {}
            artist_id = sess.get("artist_id") or ""
            artist_name = sess.get("artist_name") or "Артист"
            if artist_id:
                await set_mode_fsm(state, "text")
                status = await message.answer(
                    f"🔍 Ищу «{escape_html(text[:60])}» у "
                    f"<b>{escape_html(artist_name)}</b>…"
                )
                await _show_artist_discography(
                    status,
                    artist_id=artist_id,
                    artist_name=artist_name,
                    country=get_country(uid_of(message)),
                    uid=uid_of(message),
                    edit=True,
                    filter_query=text,
                )
                return

        await set_mode_fsm(state, "text")
        await handle_text_search(message, text)
        return

    await message.answer(
        "Пришлите название или ссылку — или выберите режим:",
        reply_markup=main_menu_kb(),
    )


# ---------- callbacks ----------


async def on_callback(callback: CallbackQuery, bot: Bot, state: FSMContext) -> None:
    data = (callback.data or "").strip()
    uid = uid_of(callback)
    country = get_country(uid)
    msg = callback.message

    if data == "noop":
        await callback.answer()
        return

    # modes (FSM) — меняем то же сообщение, не плодим новые
    if data.startswith("mode:"):
        mode = data.split(":", 1)[1]
        if not msg:
            await callback.answer()
            return
        if mode == "tops":
            await callback.answer("Загружаю…")
            await set_mode_fsm(state, mode)
            await _show_genius(msg, "global")
            return
        if mode == "newreleases":
            await callback.answer("Загружаю…")
            await set_mode_fsm(state, mode)
            await _show_new_releases(msg, country)
            return
        await callback.answer()
        if mode == "menu":
            await state.clear()
            await set_mode_fsm(state, "text")
            await msg.answer(
                _menu_text(uid),
                reply_markup=get_main_reply_keyboard(),
            )
            await ui_show(
                msg,
                "Пришлите название или ссылку — или выберите режим:",
                reply_markup=main_menu_kb(),
                edit=True,
            )
            return
        await set_mode_fsm(state, mode)
        if mode == "text":
            await ui_show(
                msg,
                "🔍 <b>Поиск по названию</b>\n"
                "Введите артиста, альбом или трек "
                "<i>(можно сразу, без этой кнопки)</i>:",
                reply_markup=back_to_menu_kb(),
                edit=True,
            )
        elif mode == "cover":
            await ui_show(
                msg,
                "🖼 <b>Поиск по обложке</b>\n\n"
                "⏳ Пока в разработке.\n"
                "Сейчас ищите по <b>названию</b> или <b>ссылке</b>.",
                reply_markup=back_to_menu_kb(),
                edit=True,
            )
        elif mode == "link":
            await ui_show(
                msg,
                "🔗 <b>Поиск по ссылке</b>\n\n"
                "Пришлите ссылку на трек, альбом или артиста:\n"
                "• Apple Music / iTunes\n"
                "• Spotify\n"
                "• YouTube\n"
                "• Genius\n"
                "• Яндекс.Музыка\n\n"
                "<i>Ссылку можно кидать сразу в чат — кнопка не обязательна.</i>\n\n"
                "<i>Пример:</i>\n"
                "<code>https://genius.com/albums/…</code>\n"
                "<code>https://open.spotify.com/track/…</code>\n"
                "<code>https://youtu.be/…</code>",
                reply_markup=back_to_menu_kb(),
                edit=True,
            )
        elif mode == "melody":
            await ui_show(
                msg,
                "Эта функция временно недоступна.",
                reply_markup=main_menu_kb(),
                edit=True,
            )
        return

    # genius country (legacy callbacks / deep links)
    if data.startswith("gen:"):
        cc = data.split(":", 1)[1]
        await callback.answer("Загружаю…")
        if msg:
            await _show_genius(msg, cc)
        return

    # lyrics pagination / select
    if data.startswith("lp:"):
        parts = data.split(":")
        await callback.answer()
        if len(parts) >= 3 and msg:
            key, page_s = parts[1], parts[2]
            try:
                page = int(page_s)
            except ValueError:
                page = 0
            hits = _lyrics_sessions.get(key) or []
            if not hits:
                await callback.answer("Список устарел", show_alert=True)
                return
            await _present_lyrics_page(
                msg, hits, page=page, session_key=key, edit=True
            )
        return

    if data.startswith("ls:"):
        parts = data.split(":")
        if len(parts) < 3 or not msg:
            await callback.answer()
            return
        key, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            await callback.answer()
            return
        hits = _lyrics_sessions.get(key) or []
        if idx < 0 or idx >= len(hits):
            await callback.answer("Трек не найден", show_alert=True)
            return
        hit = hits[idx]
        await callback.answer("Ищу…")
        await _send_track_result(
            msg,
            artist=hit.artist,
            title=hit.title,
            album=hit.album,
            cover=hit.cover_url,
            country=country,
        )
        return

    # рекомендации (старые кнопки плейлистов ведут сюда)
    if data in {"pl:menu", "pl:mine", "pl:reco", "pl:new"}:
        await callback.answer("Считаю…" if data == "pl:reco" else "")
        if msg:
            try:
                await show_recommendations(
                    msg, uid, country=country, edit=True
                )
            except Exception:  # noqa: BLE001
                await show_recommendations(
                    msg, uid, country=country, edit=False
                )
        return

    if data.startswith("pl:open:"):
        await callback.answer()
        parts = data.split(":")
        try:
            pid = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
        except (ValueError, IndexError):
            await callback.answer("Ошибка", show_alert=True)
            return
        if msg:
            try:
                await show_user_playlist(
                    msg, uid, pid, page=page, edit=True
                )
            except Exception:  # noqa: BLE001
                await show_user_playlist(
                    msg, uid, pid, page=page, edit=False
                )
        return

    if data.startswith("pl:rmpl:"):
        try:
            pid = int(data.split(":")[-1])
        except ValueError:
            await callback.answer()
            return
        ok = await delete_user_playlist(uid, pid)
        await callback.answer("Удалён" if ok else "Не найден")
        if msg:
            try:
                await show_my_playlists(msg, uid, edit=True)
            except Exception:  # noqa: BLE001
                await show_my_playlists(msg, uid, edit=False)
        return

    if data.startswith("pl:addpick:"):
        await callback.answer()
        parts = data.split(":")
        try:
            pid = int(parts[2])
            page = int(parts[3]) if len(parts) > 3 else 0
        except (ValueError, IndexError):
            return
        per_page = 8
        tracks, total = await list_recent(uid, page=page, per_page=per_page)
        total_pages = pages_count(total, per_page)
        if not tracks:
            await callback.answer(
                "Сначала скачайте трек (⬇ Скачать MP3)", show_alert=True
            )
            return
        if msg:
            await ui_show(
                msg,
                f"➕ Выберите трек из скачанных → в плейлист\n"
                f"Стр. {page + 1}/{max(total_pages, 1)}",
                reply_markup=pick_recent_for_playlist_kb(
                    tracks,
                    playlist_id=pid,
                    page=page,
                    total_pages=total_pages,
                ),
                edit=True,
            )
        return

    if data.startswith("pl:add:"):
        parts = data.split(":")
        try:
            pid = int(parts[2])
            rid = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer()
            return
        ok = await add_recent_to_user_playlist(uid, pid, rid)
        await callback.answer("Добавлено ✓" if ok else "Уже есть / ошибка")
        if msg and ok:
            try:
                await show_user_playlist(msg, uid, pid, page=0, edit=True)
            except Exception:  # noqa: BLE001
                await show_user_playlist(msg, uid, pid, page=0, edit=False)
        return

    if data.startswith("pl:to:"):
        try:
            rid = int(data.split(":")[-1])
        except ValueError:
            await callback.answer()
            return
        playlists = await list_user_playlists(uid)
        await callback.answer()
        if msg:
            await ui_show(
                msg,
                "📁 Куда добавить трек?",
                reply_markup=choose_playlist_kb(playlists, recent_id=rid),
                edit=True,
            )
        return

    if data.startswith("pl:put:"):
        parts = data.split(":")
        try:
            pid = int(parts[2])
            rid = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer()
            return
        ok = await add_recent_to_user_playlist(uid, pid, rid)
        await callback.answer("Добавлено ✓" if ok else "Уже есть / ошибка")
        if msg:
            try:
                await show_user_playlist(msg, uid, pid, page=0, edit=True)
            except Exception:  # noqa: BLE001
                await show_user_playlist(msg, uid, pid, page=0, edit=False)
        return

    if data.startswith("pl:uplay:"):
        try:
            tid = int(data.split(":")[-1])
        except ValueError:
            await callback.answer()
            return
        item = await get_user_playlist_track(uid, tid)
        if not item or not item.file_id:
            await callback.answer("Трек не найден", show_alert=True)
            return
        await callback.answer("▶")
        if msg:
            try:
                await msg.answer_audio(
                    audio=item.file_id,
                    title=item.track_name[:64],
                    performer=item.artist[:64],
                    caption=AUDIO_CAPTION,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("uplay failed: %s", exc)
                await msg.answer(
                    format_error("Не удалось воспроизвести — скачайте снова.")
                )
        return

    if data.startswith("pl:udel:"):
        parts = data.split(":")
        try:
            pid = int(parts[2])
            tid = int(parts[3])
        except (ValueError, IndexError):
            await callback.answer()
            return
        ok = await remove_from_user_playlist(uid, pid, tid)
        await callback.answer("Удалено" if ok else "Не найдено")
        if msg and ok:
            try:
                await show_user_playlist(msg, uid, pid, page=0, edit=True)
            except Exception:  # noqa: BLE001
                await show_user_playlist(msg, uid, pid, page=0, edit=False)
        return

    if data.startswith("pl:rart:"):
        key = data.split(":", 2)[-1].strip()
        art = _pending_queries.pop(key, "") or key
        await callback.answer("Ищу…")
        if msg and art:
            await handle_text_search(msg, art, mode="artist")
        return

    if data.startswith("pl:rq:"):
        key = data.split(":", 2)[-1].strip()
        q = _pending_queries.pop(key, "") or key
        await callback.answer("Ищу…")
        if msg and q:
            await handle_text_search(msg, q, mode="both")
        return

    if data.startswith("pl:recent:"):
        await callback.answer()
        if msg:
            try:
                await show_recommendations(
                    msg, uid, country=country, edit=True
                )
            except Exception:  # noqa: BLE001
                await show_recommendations(
                    msg, uid, country=country, edit=False
                )
        return

    if data.startswith("pl:play:"):
        try:
            tid = int(data.split(":")[-1])
        except ValueError:
            await callback.answer()
            return
        item = await get_playlist_track(uid, tid)
        if not item or not item.file_id:
            await callback.answer("Трек не найден", show_alert=True)
            return
        await callback.answer("▶")
        if msg:
            try:
                await msg.answer_audio(
                    audio=item.file_id,
                    title=item.track_name[:64],
                    performer=item.artist[:64],
                    caption=AUDIO_CAPTION,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("replay file_id failed: %s", exc)
                await msg.answer(
                    format_error(
                        "Не удалось воспроизвести. Возможно, файл устарел — "
                        "скачайте трек заново."
                    )
                )
        return

    if data.startswith("pl:del:"):
        try:
            tid = int(data.split(":")[-1])
        except ValueError:
            await callback.answer()
            return
        ok = await delete_from_playlist(uid, tid)
        await callback.answer("Удалено" if ok else "Не найдено")
        if msg and ok:
            try:
                await show_recent_playlist(msg, uid, page=0, edit=True)
            except Exception:  # noqa: BLE001
                await show_recent_playlist(msg, uid, page=0, edit=False)
        return

    # выбор типа запроса: исполнитель / альбом
    if data.startswith("qt:"):
        parts = data.split(":")
        await callback.answer()
        if len(parts) >= 3 and msg:
            qmode, qkey = parts[1], parts[2]
            query = _pending_queries.pop(qkey, "")
            if not query:
                await msg.answer(
                    format_error("Запрос устарел. Введите название снова."),
                    reply_markup=main_menu_kb(),
                )
                return
            if qmode == "yt":
                await handle_text_search(msg, query, mode="yt")
                return
            if qmode not in {"artist", "album", "both", "combo"}:
                qmode = "both"
            await handle_text_search(msg, query, mode=qmode)
        return

    # YouTube results pagination / download
    if data.startswith("yp:"):
        parts = data.split(":")
        if len(parts) >= 3 and msg:
            key, page_s = parts[1], parts[2]
            try:
                page = int(page_s)
            except ValueError:
                page = 0
            sess = _youtube_sessions.get(key) or {}
            hits = sess.get("hits") or []
            if not hits:
                await callback.answer("Список устарел", show_alert=True)
                return
            await callback.answer()
            await _present_youtube_page(
                msg,
                hits,
                query=sess.get("query") or "",
                page=page,
                session_key=key,
                edit=True,
            )
        else:
            await callback.answer()
        return

    if data.startswith("ys:"):
        parts = data.split(":")
        if len(parts) < 3 or not msg:
            await callback.answer()
            return
        key, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            await callback.answer()
            return
        sess = _youtube_sessions.get(key) or {}
        hits = sess.get("hits") or []
        if idx < 0 or idx >= len(hits):
            await callback.answer("Видео не найдено", show_alert=True)
            return
        hit: YoutubeHit = hits[idx]
        # Telegram ~48 МБ — многочасовые миксы не проходят
        title_l = (hit.title or "").lower()
        if hit.duration and hit.duration > 30 * 60:
            await callback.answer(
                "Ролик длиннее 30 мин — Telegram не примет (~48 МБ)",
                show_alert=True,
            )
            return
        if re.search(r"\d+\s*(час|hour)", title_l):
            await callback.answer(
                "Похоже на многочасовой микс — Telegram не примет файл",
                show_alert=True,
            )
            return
        await callback.answer("Идёт загрузка…")
        art, tit = parse_artist_title_from_yt(hit.title, hit.uploader)
        status = await msg.answer(
            f"⏳ Загрузка с YouTube: 0%\n"
            f"<code>{escape_html(hit.title[:80])}</code>"
        )
        path = None
        try:
            last_edit = {"pct": -1}

            async def _progress(pct: int) -> None:
                if pct == last_edit["pct"]:
                    return
                if pct not in {0, 100} and pct - last_edit["pct"] < 5:
                    return
                last_edit["pct"] = pct
                try:
                    await status.edit_text(
                        f"⏳ Загрузка с YouTube: {pct}%\n"
                        f"<code>{escape_html(hit.title[:80])}</code>"
                    )
                except Exception:  # noqa: BLE001
                    pass

            # только этот video id — без повторного поиска по Apple/имени
            result = await download_track(
                hit.watch_url,
                progress_cb=_progress,
                artist="",
                title="",
            )
            path = result.path
            payload = result.payload()
            if not payload:
                raise DownloadError("Файл пустой после скачивания.")
            audio = BufferedInputFile(
                payload,
                filename=f"{(result.artist or art or 'track')} - "
                f"{(result.title or tit or 'audio')}.mp3",
            )
            sent = await msg.answer_audio(
                audio=audio,
                title=(result.title or tit or hit.title)[:64],
                performer=(result.artist or art or hit.uploader)[:64],
                duration=result.duration or hit.duration or None,
                caption=AUDIO_CAPTION,
            )
            if sent.audio and sent.audio.file_id:
                await save_to_playlist(
                    uid,
                    result.title or tit or hit.title,
                    result.artist or art or hit.uploader,
                    sent.audio.file_id,
                )
            await status.edit_text("✅ Готово! Трек добавлен в «Недавно скачанные».")
        except DownloadError as exc:
            await status.edit_text(format_error(str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("youtube download: %s", exc)
            await status.edit_text(format_error("Не удалось скачать с YouTube."))
        finally:
            if path is not None:
                cleanup_download(path)
        return

    # настройки качества
    if data.startswith("set:q:"):
        quality = data.split(":")[-1]
        if quality in {"128", "192"}:
            _user_quality[uid] = quality
            await callback.answer(f"Качество {quality} kbps")
            if msg:
                await ui_show(
                    msg,
                    "⚙️ <b>Настройки</b>\n"
                    f"Качество MP3: <code>{quality} kbps</code>",
                    reply_markup=settings_kb(quality=quality),
                    edit=True,
                )
        else:
            await callback.answer()
        return

    # genius pagination
    if data.startswith("gp:"):
        parts = data.split(":")
        if len(parts) >= 3 and msg:
            cc, page_s = parts[1], parts[2]
            try:
                page = int(page_s)
            except ValueError:
                page = 0
            songs = _chart_cache_mem.get(cc) or []
            if not songs:
                await callback.answer("Список устарел, откройте топ заново", show_alert=True)
                return
            total_pages = pages_count(len(songs), CHART_PER_PAGE)
            page = max(0, min(page, total_pages - 1))
            chunk = songs[page * CHART_PER_PAGE : (page + 1) * CHART_PER_PAGE]
            text = format_chart_text(
                chunk,
                title="📊 Топ Genius",
                country=cc,
                page=page + 1,
                total=len(songs),
            )
            await msg.edit_text(
                text,
                reply_markup=chart_page_kb(
                    chunk, page=page, total_pages=total_pages, country=cc
                ),
                disable_web_page_preview=True,
            )
        await callback.answer()
        return

    # genius song details
    if data.startswith("gd:"):
        parts = data.split(":")
        if len(parts) >= 3 and msg:
            cc, idx_s = parts[1], parts[2]
            try:
                idx = int(idx_s)
            except ValueError:
                await callback.answer()
                return
            songs = _chart_cache_mem.get(cc) or []
            if idx < 0 or idx >= len(songs):
                await callback.answer("Нет данных", show_alert=True)
                return
            song = songs[idx]
            await callback.answer("Ищу…")
            await _send_track_result(
                msg,
                artist=song.artist,
                title=song.title,
                cover=song.cover_url,
                country=country,
            )
        return

    # album/single pagination / назад с карточки: ap:<kind>:<session_key>:<page>
    if data.startswith("ap:"):
        parts = data.split(":")
        await safe_callback_answer(callback)
        if len(parts) >= 4 and msg:
            kind, key, page_s = parts[1], parts[2], parts[3]
            try:
                page = int(page_s)
            except ValueError:
                page = 0
            sess = _album_sessions.get(key) or {}
            if not sess:
                await ui_show(
                    msg,
                    format_error("Список устарел — повторите поиск."),
                    reply_markup=main_menu_kb(),
                    edit=True,
                )
                return
            items = sess.get("albums" if kind == "alb" else "singles") or []
            artist_name = sess.get("artist_name") or ""
            header = (
                f"💿 <b>{escape_html(artist_name)}</b>"
                if artist_name
                else ("🎵 <b>Альбомы</b>" if kind == "alb" else "🎶 <b>Синглы</b>")
            )
            await _present_albums_page(
                msg,
                items if kind == "alb" else [],
                page=page,
                session_key=key,
                uid=uid,
                header=header,
                edit=True,
                kind=kind,
            )
        elif len(parts) >= 3 and msg:
            # старый формат ap:key:page
            key, page_s = parts[1], parts[2]
            try:
                page = int(page_s)
            except ValueError:
                page = 0
            sess = _album_sessions.get(key) or {}
            albums = sess.get("albums") or []
            await _present_albums_page(
                msg,
                albums,
                page=page,
                session_key=key,
                uid=uid,
                edit=True,
                kind="alb",
            )
        return

    # поиск внутри дискографии
    if data.startswith("af:"):
        key = data[3:]
        sess = _album_sessions.get(key) or {}
        if not sess.get("artist_id"):
            await callback.answer("Сессия устарела", show_alert=True)
            return
        await callback.answer()
        await state.update_data(disc_session=key)
        await set_mode_fsm(state, "artist_filter")
        name = sess.get("artist_name") or "артиста"
        if msg:
            await ui_show(
                msg,
                f"🔍 <b>Поиск в дискографии</b>\n"
                f"👤 {escape_html(name)}\n\n"
                f"Введите название альбома или часть названия:",
                reply_markup=back_to_menu_kb(back_callback=f"ar:{sess['artist_id']}"),
                edit=True,
            )
        return

    # artist selected
    if data.startswith("ar:"):
        artist_id = data[3:]
        await callback.answer()
        if msg:
            try:
                # имя из сессий / кнопок — уточним из lookup
                artist_name = "Артист"
                for sess in _album_sessions.values():
                    if sess.get("artist_id") == artist_id and sess.get("artist_name"):
                        artist_name = sess["artist_name"]
                        break
                # быстрый lookup имени
                try:
                    payload_albums = await albums_by_artist_id(
                        artist_id, country=country, limit=1
                    )
                    if payload_albums:
                        artist_name = payload_albums[0].artist_name or artist_name
                except Exception:  # noqa: BLE001
                    pass
                await _show_artist_discography(
                    msg,
                    artist_id=artist_id,
                    artist_name=artist_name,
                    country=country,
                    uid=uid,
                    edit=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("artist albums: %s", exc)
                await ui_show(
                    msg,
                    format_error("Не удалось загрузить дискографию."),
                    reply_markup=back_to_menu_kb(),
                    edit=True,
                )
        return

    # single track chosen
    if data.startswith("a:t:"):
        track_id = data[4:]
        await callback.answer("Загружаю…")
        if msg and track_id:
            try:
                # найти трек в сессиях
                track: Optional[TrackCandidate] = None
                for sess in _album_sessions.values():
                    for t in sess.get("singles") or []:
                        if t.track_id == track_id:
                            track = t
                            break
                    if track:
                        break
                if track:
                    await _send_track_result(
                        msg,
                        artist=track.artist_name,
                        title=track.track_name,
                        album=track.collection_name,
                        cover=track.artwork_url,
                        apple_url=track.track_view_url,
                        country=country,
                        duration_sec=(track.duration_ms // 1000)
                        if getattr(track, "duration_ms", 0)
                        else 0,
                    )
                else:
                    # lookup через iTunes track id
                    song = await lookup_itunes_song(track_id, country=country)
                    if song:
                        await _send_track_result(
                            msg,
                            artist=song.artist_name,
                            title=song.track_name,
                            album=song.collection_name,
                            cover=song.artwork_url,
                            apple_url=song.track_view_url,
                            country=country,
                            duration_sec=(song.duration_ms // 1000)
                            if song.duration_ms
                            else 0,
                        )
                    else:
                        await msg.answer(
                            format_error(
                                "Трек не найден в сессии. Повторите поиск."
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception("track: %s", exc)
                await msg.answer(format_error("❌ Не удалось загрузить трек."))
        return

    # album chosen
    if (
        data.startswith("a:i:")
        or data.startswith("a:m:")
        or data.startswith("a:s:")
    ):
        if data.startswith("a:i:"):
            source, source_id = "itunes", data[4:]
        elif data.startswith("a:s:"):
            source, source_id = "spotify", data[4:]
        else:
            source, source_id = "musicbrainz", data[4:]
        source_id = (source_id or "").strip()
        if not source_id:
            await callback.answer("Нет ID альбома, повторите поиск", show_alert=True)
            return
        await callback.answer("Загружаю…")
        if msg:
            try:
                await ui_edit(msg, "⏳ Загружаю альбом…")
                cand = _find_album_in_sessions(source, source_id) or AlbumCandidate(
                    source=source,  # type: ignore[arg-type]
                    source_id=source_id,
                    artist_name="",
                    collection_name="",
                )
                await _send_album_details(
                    msg, cand, country=country, uid=uid, edit=True
                )
            except MusicAPIError as exc:
                await ui_show(
                    msg,
                    format_error(str(exc)),
                    reply_markup=back_to_menu_kb(
                        back_callback=list_back_callback(uid)
                    ),
                    edit=True,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("album: %s", exc)
                await ui_show(
                    msg,
                    format_error("❌ Не удалось загрузить альбом."),
                    reply_markup=back_to_menu_kb(
                        back_callback=list_back_callback(uid)
                    ),
                    edit=True,
                )
        return

    # download: выбор треков — редактируем карточку альбома
    if data.startswith("dl:"):
        key = data[3:]
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer(
                "Сессия устарела, откройте альбом снова", show_alert=True
            )
            return
        if not sess.get("free_download", True):
            await callback.answer(
                "Альбом недоступен для свободной загрузки",
                show_alert=True,
            )
            return
        await callback.answer()
        if not msg:
            return
        tracks = sess.get("tracks") or []
        unavail = set(sess.get("unavailable_idx") or [])
        await ui_show(
            msg,
            _download_select_text(sess),
            reply_markup=show_track_selection(
                key, tracks, page=0, unavailable=unavail
            ),
            edit=True,
        )
        return

    # назад с экрана выбора трека → карточка альбома
    if data.startswith("bk:dl:"):
        key = data[6:]
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer("Сессия устарела", show_alert=True)
            return
        await callback.answer()
        if not msg:
            return
        view = sess.get("view_text") or (
            f"🎧 <b>{escape_html(sess.get('album') or '')}</b>\n"
            f"👤 {escape_html(sess.get('artist') or '')}"
        )
        await ui_show(
            msg,
            view,
            reply_markup=_album_platform_kb(sess, key),
            edit=True,
        )
        return

    # пагинация списка треков для скачивания
    if data.startswith("dp:"):
        parts = data.split(":")
        if len(parts) < 3 or not msg:
            await callback.answer()
            return
        key, page_s = parts[1], parts[2]
        try:
            page = int(page_s)
        except ValueError:
            page = 0
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer("Сессия устарела", show_alert=True)
            return
        await callback.answer()
        tracks = sess.get("tracks") or []
        unavail = set(sess.get("unavailable_idx") or [])
        await ui_show(
            msg,
            _download_select_text(sess),
            reply_markup=show_track_selection(
                key, tracks, page=page, unavailable=unavail
            ),
            edit=True,
        )
        return

    # скачать один трек
    if data.startswith("dt:"):
        parts = data.split(":")
        if len(parts) < 3 or not msg:
            await callback.answer()
            return
        key, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            await callback.answer()
            return
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer(
                "Сессия устарела, откройте альбом снова", show_alert=True
            )
            return
        tracks = sess.get("tracks") or []
        if idx < 0 or idx >= len(tracks):
            await callback.answer("Трек не найден", show_alert=True)
            return
        track = tracks[idx]
        title = track.get("name") or ""
        artist = track.get("artist") or sess.get("artist") or ""
        await callback.answer("Идёт загрузка…")
        status = await msg.answer(
            f"⏳ Загрузка: 0%\n"
            f"<code>{escape_html(artist)} — {escape_html(title)}</code>"
        )
        path = None
        try:
            last_edit = {"pct": -1}

            async def _progress(pct: int) -> None:
                if pct == last_edit["pct"]:
                    return
                if pct not in {0, 100} and pct - last_edit["pct"] < 5:
                    return
                last_edit["pct"] = pct
                try:
                    await status.edit_text(
                        f"⏳ Загрузка: {pct}%\n"
                        f"<code>{escape_html(artist)} — {escape_html(title)}</code>"
                    )
                except Exception:  # noqa: BLE001
                    pass

            result = await download_single_track(
                artist,
                title,
                progress_cb=_progress,
                expected_duration=(
                    int(track.get("duration") or 0) or None
                ),
                album=(sess.get("album") or ""),
                strict=True,
            )
            path = result.path
            payload = result.payload()
            if not payload:
                raise DownloadError("Файл пустой после скачивания.")
            audio = BufferedInputFile(
                payload,
                filename=(
                    path.name
                    if path and path.suffix
                    else f"{(result.title or title)}.mp3"
                ),
            )
            sent = await msg.answer_audio(
                audio=audio,
                title=(result.title or title)[:64],
                performer=(result.artist or artist)[:64],
                duration=result.duration,
                caption=AUDIO_CAPTION,
            )
            if sent.audio and sent.audio.file_id:
                await save_to_playlist(
                    uid,
                    result.title or title,
                    result.artist or artist,
                    sent.audio.file_id,
                )
            await status.edit_text("✅ Готово! Трек добавлен в «Недавно скачанные».")
        except DownloadError as exc:
            body, kb_err = _format_download_error(exc)
            if getattr(exc, "unavailable_free", False):
                # пометить трек в сессии, если есть
                bad = sess.setdefault("unavailable_idx", set())
                bad.add(idx)
                sess["unavailable_idx"] = bad
            try:
                await status.edit_text(body, reply_markup=kb_err, disable_web_page_preview=True)
            except Exception:  # noqa: BLE001
                await status.edit_text(format_error(str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("download track: %s", exc)
            await status.edit_text(
                format_error(
                    "Не удалось отправить файл. Установите ffmpeg для конвертации в MP3."
                )
            )
        finally:
            if path is not None:
                cleanup_download(path)
        return

    # трек помечен как без свободной загрузки
    if data.startswith("du:"):
        parts = data.split(":")
        if len(parts) < 3 or not msg:
            await callback.answer()
            return
        key, idx_s = parts[1], parts[2]
        try:
            idx = int(idx_s)
        except ValueError:
            await callback.answer()
            return
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer("Сессия устарела", show_alert=True)
            return
        tracks = sess.get("tracks") or []
        if idx < 0 or idx >= len(tracks):
            await callback.answer()
            return
        track = tracks[idx]
        title = track.get("name") or ""
        artist = track.get("artist") or sess.get("artist") or ""
        await callback.answer()
        await msg.answer(
            f"{CLOSED_DOWNLOAD_NOTICE}\n\n"
            f"<code>{escape_html(artist)} — {escape_html(title)}</code>",
            reply_markup=unavailable_free_kb(artist, title),
            disable_web_page_preview=True,
        )
        return

    # скачать весь альбом ZIP
    if data.startswith("dz:"):
        key = data[3:]
        sess = _download_sessions.get(key)
        if not sess:
            await callback.answer(
                "Сессия устарела, откройте альбом снова", show_alert=True
            )
            return
        await callback.answer("Упаковываю…")
        if not msg:
            return
        tracks = sess.get("tracks") or []
        artist = sess.get("artist") or ""
        album = sess.get("album") or ""
        status = await msg.answer(
            "📦 Упаковываю альбом в ZIP... Это может занять до 1 минуты.\n"
            f"<code>{escape_html(artist)} — {escape_html(album)}</code>"
        )
        zip_paths: list = []
        audios = None
        try:
            result = await download_album_as_zip(
                tracks, artist=artist, album=album
            )
            zip_paths = result.zip_paths
            audios = result.individual_files
            if result.too_large and audios:
                await status.edit_text(
                    "Архив слишком большой. Скачайте треки по отдельности."
                )
                # показать снова выбор треков
                key2 = store_download_session(
                    artist=artist, album=album, tracks=list(tracks)
                )
                await msg.answer(
                    "⬇ Выберите трек:",
                    reply_markup=show_track_selection(key2, tracks, page=0),
                )
                return
            if result.send_individually and audios:
                await status.edit_text(
                    "⚠️ ZIP слишком большой. Отправляю треки по одному…"
                )
                for audio_item in audios:
                    try:
                        doc = BufferedInputFile(
                            audio_item.payload(),
                            filename=audio_item.path.name,
                        )
                        sent = await msg.answer_audio(
                            audio=doc,
                            title=audio_item.title[:64],
                            performer=(audio_item.artist or artist)[:64],
                            duration=audio_item.duration,
                            caption=AUDIO_CAPTION,
                        )
                        if sent.audio and sent.audio.file_id:
                            await save_to_playlist(
                                uid,
                                audio_item.title,
                                audio_item.artist or artist,
                                sent.audio.file_id,
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("send individual: %s", exc)
                await status.edit_text("✅ Треки отправлены по одному.")
            else:
                if len(zip_paths) > 1:
                    await status.edit_text(
                        f"📦 Альбом разбит на {len(zip_paths)} ZIP "
                        f"(лимит Telegram 50 МБ)…"
                    )
                for i, zp in enumerate(zip_paths, start=1):
                    doc = BufferedInputFile(
                        zp.read_bytes(),
                        filename=zp.name,
                    )
                    caption = (
                        f"📦 {escape_html(album)} "
                        f"({i}/{len(zip_paths)})"
                        if len(zip_paths) > 1
                        else f"📦 {escape_html(album)}"
                    )
                    await msg.answer_document(document=doc, caption=caption)
                await status.edit_text("✅ ZIP готов!")
        except DownloadError as exc:
            await status.edit_text(format_error(str(exc)))
        except Exception as exc:  # noqa: BLE001
            logger.exception("download zip: %s", exc)
            await status.edit_text(
                format_error("❌ Не удалось упаковать альбом. Попробуйте треки по одному.")
            )
        finally:
            cleanup_album_files(zip_paths, audios)
        return

    # country (legacy-кнопки) — регион убран из UI
    if data == "ct:menu" or data.startswith("ct:"):
        await callback.answer("Регион больше не выбирается")
        if msg:
            quality = _user_quality.get(uid, "192")
            await ui_show(
                msg,
                "⚙️ <b>Настройки</b>\n"
                f"Качество MP3: <code>{quality} kbps</code>",
                reply_markup=settings_kb(quality=quality),
                edit=True,
            )
        return

    await callback.answer()


# ---------- main ----------


async def _check_telegram(bot: Bot) -> None:
    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=25)
    except asyncio.TimeoutError as exc:
        raise SystemExit(
            "\nТаймаут api.telegram.org — включите VPN или TELEGRAM_PROXY\n"
        ) from exc
    logger.info("Telegram OK: @%s", me.username)


async def main() -> None:
    from bootstrap import ensure_system_deps

    ensure_system_deps()
    require_core_env()
    validate_token(config.BOT_TOKEN)
    init_playlist_db()
    import shutil

    from config import YTMUSIC_PROXY, refresh_ytdlp_cookies, ytdlp_cookies_status

    refresh_ytdlp_cookies()
    ck = ytdlp_cookies_status()
    from download import _cookies_look_logged_in

    cookie_login = (
        "yes"
        if ck["path"] and _cookies_look_logged_in(str(ck["path"]))
        else "no"
    )
    logger.info(
        "yt-dlp cookies=%s source=%s size=%s logged_in=%s b64_env=%s node=%s ffmpeg=%s proxy=%s",
        ck["path"] or "none",
        ck["source"],
        ck["size"] or "-",
        cookie_login,
        "yes" if ck["b64_env_set"] else "no",
        shutil.which("node") or shutil.which("nodejs") or "none",
        shutil.which("ffmpeg") or "none",
        "yes" if YTMUSIC_PROXY else "no",
    )
    if ck["path"] and cookie_login == "no":
        logger.warning(
            "YouTube cookies без LOGIN_INFO/SID — экспорт неполный. "
            "На Mac: yt-dlp --cookies-from-browser chrome --cookies cookies.txt "
            "--skip-download https://www.youtube.com && base64 -i cookies.txt | pbcopy"
        )
    if ck["b64_env_set"] and not ck["path"]:
        logger.warning(
            "YTDLP_COOKIES_B64 задан, но cookies не созданы: %s",
            ck["error"] or "unknown",
        )
    elif not ck["path"]:
        logger.warning(
            "YouTube cookies не заданы — скачивание может падать с антиботом"
        )

    session = None
    if TELEGRAM_PROXY:
        session = AiohttpSession(proxy=TELEGRAM_PROXY)

    bot = Bot(
        token=config.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await _check_telegram(bot)
    # hosting / второй инстанс часто включает webhook → polling перестаёт получать апдейты
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("Webhook cleared — using long polling")
    except Exception as exc:  # noqa: BLE001
        logger.warning("delete_webhook failed: %s", exc)

    dp = Dispatcher(storage=MemoryStorage())
    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_country, Command("country"))
    dp.message.register(cmd_top, Command("top"))
    dp.message.register(cmd_newreleases, Command("newreleases"))
    dp.callback_query.register(on_callback)
    dp.message.register(on_message)

    logger.info("Bot started. default_country=%s FSM=MemoryStorage", DEFAULT_COUNTRY)

    # если другой инстанс/хостинг ставит webhook — сбрасываем и продолжаем polling
    async def _keep_polling_exclusive() -> None:
        from aiogram.exceptions import TelegramConflictError

        while True:
            try:
                await asyncio.sleep(8)
                info = await bot.get_webhook_info()
                if info.url:
                    logger.warning(
                        "Обнаружен webhook %s — удаляю (мешает локальному polling). "
                        "Остановите второй инстанс/хостинг с этим BOT_TOKEN.",
                        info.url,
                    )
                    await bot.delete_webhook(drop_pending_updates=False)
            except TelegramConflictError:
                try:
                    await bot.delete_webhook(drop_pending_updates=False)
                except Exception:  # noqa: BLE001
                    pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("webhook watchdog: %s", exc)

    watchdog = asyncio.create_task(_keep_polling_exclusive())
    try:
        await dp.start_polling(bot, handle_signals=True)
    finally:
        watchdog.cancel()
        await close_session()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as exc:
        if str(exc):
            print(exc)
        logger.info("Stopped.")
