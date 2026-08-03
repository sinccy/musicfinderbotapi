"""Inline- и reply-клавиатуры: меню, пагинация, платформы, топы."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from config import ALBUMS_PER_PAGE, CHART_PER_PAGE
from i18n import DEFAULT_LANG, normalize_lang, t

# Тексты кнопок reply-клавиатуры (для фильтров в bot.py)
BTN_START = "🏠 Главное меню"
BTN_SETTINGS = "⚙️ Настройки"
BTN_PLAYLISTS = "📂 Плейлисты"
BTN_HELP = "❓ Справка"
BTN_RECENT = "📂 Недавно скачанные"
BTN_MY_PLAYLISTS = "🎛 Мои плейлисты"
BTN_RECOMMEND = "✨ Рекомендации"

BTN_START_EN = "🏠 Main menu"
BTN_SETTINGS_EN = "⚙️ Settings"
BTN_HELP_EN = "❓ Help"
BTN_RECOMMEND_EN = "✨ Recommendations"
BTN_REFERRAL = "🎁 Рефералка"
BTN_REFERRAL_EN = "🎁 Referral"


def get_main_reply_keyboard(lang: str = DEFAULT_LANG) -> ReplyKeyboardMarkup:
    """Постоянная нижняя клавиатура."""
    lang_n = normalize_lang(lang)
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=t("btn_start", lang_n)),
                KeyboardButton(text=t("btn_settings", lang_n)),
            ],
            [
                KeyboardButton(text=t("btn_recommend", lang_n)),
                KeyboardButton(text=t("btn_referral", lang_n)),
            ],
            [
                KeyboardButton(text=t("btn_help", lang_n)),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=t("reply_placeholder", lang_n),
    )


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🇷🇺 Русский",
                    callback_data="lang:ru",
                ),
                InlineKeyboardButton(
                    text="🇬🇧 English",
                    callback_data="lang:en",
                ),
            ],
        ]
    )


def query_type_kb(query_key: str) -> InlineKeyboardMarkup:
    """Выбор: исполнитель / альбом / трек на YouTube."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👤 Исполнитель",
                    callback_data=f"qt:artist:{query_key}"[:64],
                ),
                InlineKeyboardButton(
                    text="💿 Альбом",
                    callback_data=f"qt:album:{query_key}"[:64],
                ),
            ],
            [
                InlineKeyboardButton(
                    text="🔍 И то, и другое",
                    callback_data=f"qt:both:{query_key}"[:64],
                )
            ],
            [
                InlineKeyboardButton(
                    text="▶ Трек на YouTube",
                    callback_data=f"qt:yt:{query_key}"[:64],
                )
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu")],
        ]
    )


def youtube_results_kb(
    hits: Sequence,
    *,
    page: int,
    total_pages: int,
    session_key: str,
    per_page: int = ALBUMS_PER_PAGE,
) -> InlineKeyboardMarkup:
    """
    ys:<session>:<idx> — скачать
    yp:<session>:<page> — страница
    """
    total = len(hits)
    total_pages = pages_count(total, per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = list(hits)[start : start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    for i, hit in enumerate(chunk):
        idx = start + i
        label = getattr(hit, "button_label", None) or str(hit)
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⬇ {label}"[:64],
                    callback_data=f"ys:{session_key}:{idx}"[:64],
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"yp:{session_key}:{page - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Вперед",
                callback_data=f"yp:{session_key}:{page + 1}"[:64],
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:menu"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_kb(
    *, quality: str, country: str = "", lang: str = DEFAULT_LANG
) -> InlineKeyboardMarkup:
    del country  # регион убран из UI
    lang_n = normalize_lang(lang)
    q_mark_192 = "✓ " if quality == "192" else ""
    q_mark_128 = "✓ " if quality == "128" else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{q_mark_192}{t('set_q_192', lang_n)}",
                    callback_data="set:q:192",
                ),
                InlineKeyboardButton(
                    text=f"{q_mark_128}{t('set_q_128', lang_n)}",
                    callback_data="set:q:128",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=t("set_language", lang_n),
                    callback_data="lang:pick",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("set_referral", lang_n),
                    callback_data="ref:home",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_menu", lang_n),
                    callback_data="mode:menu",
                )
            ],
        ]
    )


def referral_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang_n = normalize_lang(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_ref_lb", lang_n),
                    callback_data="ref:lb",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_menu", lang_n),
                    callback_data="mode:menu",
                )
            ],
        ]
    )


def referral_lb_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang_n = normalize_lang(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("btn_ref_back", lang_n),
                    callback_data="ref:home",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_menu", lang_n),
                    callback_data="mode:menu",
                )
            ],
        ]
    )


def main_menu_kb(lang: str = DEFAULT_LANG) -> InlineKeyboardMarkup:
    lang_n = normalize_lang(lang)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("menu_text_search", lang_n),
                    callback_data="mode:text",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("menu_link_search", lang_n),
                    callback_data="mode:link",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("menu_cover_search", lang_n),
                    callback_data="mode:cover",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("menu_tops", lang_n),
                    callback_data="mode:tops",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("menu_releases", lang_n),
                    callback_data="mode:newreleases",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_recommend", lang_n),
                    callback_data="pl:reco",
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("btn_referral", lang_n),
                    callback_data="ref:home",
                )
            ],
        ]
    )


def back_to_menu_kb(*, back_callback: str = "") -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if back_callback:
        rows.append(
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback[:64])]
        )
    rows.append(
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="mode:menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def genius_countries_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🌍 Global", callback_data="gen:global")],
        [
            InlineKeyboardButton(text="🇷🇺 Russia", callback_data="gen:ru"),
            InlineKeyboardButton(text="🇺🇸 USA", callback_data="gen:us"),
        ],
        [
            InlineKeyboardButton(text="🇬🇧 UK", callback_data="gen:gb"),
            InlineKeyboardButton(text="🇩🇪 Germany", callback_data="gen:de"),
        ],
        [
            InlineKeyboardButton(text="🇫🇷 France", callback_data="gen:fr"),
        ],
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:menu"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def country_kb(current: str, codes: Sequence[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for code in codes:
        mark = "✓ " if code == current else ""
        row.append(
            InlineKeyboardButton(
                text=f"{mark}{code.upper()}",
                callback_data=f"ct:{code}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:menu"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def albums_page_kb(
    albums: Sequence,
    *,
    page: int,
    total_pages: int,
    session_key: str,
    kind: str = "alb",  # alb | sng
    has_albums: bool = False,
    has_singles: bool = False,
    show_disc_search: bool = False,
    back_callback: str = "mode:menu",
) -> InlineKeyboardMarkup:
    """
    albums/singles — текущая страница.
    album: a:i:<id> / a:m:<mbid>
    single: a:t:<trackId>
    пагинация: ap:<kind>:<session_key>:<page>
    """
    rows: list[list[InlineKeyboardButton]] = []
    for album in albums:
        # TrackCandidate → a:t: ; AlbumCandidate (в т.ч. сингл-релиз) → a:i:/a:s:/a:m:
        tid = str(getattr(album, "track_id", "") or "").strip()
        sid = str(getattr(album, "source_id", "") or "").strip()
        # сингл-трек без collection id
        if tid and not sid:
            label = getattr(album, "button_label", str(album))
            if len(label) > 64:
                label = label[:61] + "…"
            cb = f"a:t:{tid}"
            if len(cb.encode("utf-8")) > 64:
                continue
            rows.append([InlineKeyboardButton(text=label, callback_data=cb)])
            continue

        if not sid:
            # без id кнопка бесполезна — альбом «не откроется»
            continue

        source = getattr(album, "source", "itunes")
        if source == "itunes":
            prefix = "a:i:"
        elif source == "spotify":
            prefix = "a:s:"
        else:
            prefix = "a:m:"
        label = getattr(album, "button_label", None) or (
            f"{getattr(album, 'collection_name', '')} – "
            f"{getattr(album, 'artist_name', '')}"
        )
        if len(label) > 64:
            label = label[:61] + "…"
        cb = f"{prefix}{sid}"
        if len(cb.encode("utf-8")) > 64:
            continue
        rows.append([InlineKeyboardButton(text=label, callback_data=cb)])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"ap:{kind}:{session_key}:{page - 1}",
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Вперед",
                callback_data=f"ap:{kind}:{session_key}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)

    if has_albums and has_singles:
        alb_mark = "· " if kind == "alb" else ""
        sng_mark = "· " if kind == "sng" else ""
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{alb_mark}🎵 Альбомы",
                    callback_data=f"ap:alb:{session_key}:0",
                ),
                InlineKeyboardButton(
                    text=f"{sng_mark}🎶 Синглы",
                    callback_data=f"ap:sng:{session_key}:0",
                ),
            ]
        )

    if show_disc_search:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔍 Найти в дискографии",
                    callback_data=f"af:{session_key}"[:64],
                )
            ]
        )

    back_cb = (back_callback or "mode:menu")[:64]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def chart_page_kb(
    items: Sequence,
    *,
    page: int,
    total_pages: int,
    country: str,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for item in items:
        title = getattr(item, "title", "") or getattr(item, "name", "")
        pos = getattr(item, "position", "?")
        # gd:<country>:<index_global>
        idx = getattr(item, "index", getattr(item, "position", 1) - 1)
        short = (title or "трек")[:40]
        label = f"{pos}. Подробнее · {short}"
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"gd:{country}:{idx}",
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"gp:{country}:{page - 1}",
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="Вперёд ➡️",
                callback_data=f"gp:{country}:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def platform_links_kb(
    *,
    youtube: str = "",
    spotify: str = "",
    apple: str = "",
    yandex: str = "",
    soundcloud: str = "",
    preview: str = "",
    download_session: str = "",
    download_locked: bool = False,
    youtube_search_query: str = "",
    back_callback: str = "",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    link_row: list[InlineKeyboardButton] = []

    def add(text: str, url: str) -> None:
        nonlocal link_row
        if not url:
            return
        link_row.append(InlineKeyboardButton(text=text, url=url))
        if len(link_row) == 2:
            rows.append(link_row)
            link_row = []

    add("▶ YouTube", youtube)
    add("🎵 Spotify", spotify)
    add("🍏 Apple", apple)
    add("🟡 Яндекс", yandex)
    add("☁ SoundCloud", soundcloud)
    if link_row:
        rows.append(link_row)
        link_row = []
    # превью отдельной строкой — чтобы не терялось среди платформ
    if preview and preview.startswith("http"):
        rows.append(
            [InlineKeyboardButton(text="🎧 Превью 30 сек", url=preview)]
        )

    if download_locked:
        from utils import youtube_search_url

        q = (youtube_search_query or "").strip() or "music"
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔍 Найти на YouTube и пришлите ссылку",
                    url=youtube_search_url(q),
                )
            ]
        )
    elif download_session:
        # dl:<session> → экран выбора треков (edit того же сообщения)
        rows.append(
            [
                InlineKeyboardButton(
                    text="⬇ Скачать MP3",
                    callback_data=f"dl:{download_session}"[:64],
                )
            ]
        )
    nav_row: list[InlineKeyboardButton] = []
    if back_callback:
        nav_row.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback[:64])
        )
    nav_row.append(InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"))
    rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_track_selection_keyboard(
    tracks: Sequence,
    *,
    page: int,
    session_key: str,
    per_page: int = ALBUMS_PER_PAGE,
    unavailable: Optional[set[int]] = None,
) -> InlineKeyboardMarkup:
    """
    Клавиатура выбора трека для скачивания.
    dt:<session>:<idx> — один трек
    du:<session>:<idx> — трек без свободной версии (подсказка YouTube)
    dz:<session> — ZIP всего альбома
    dp:<session>:<page> — пагинация
    """
    unavailable = unavailable or set()
    total = len(tracks)
    total_pages = pages_count(total, per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = list(tracks)[start : start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    for i, track in enumerate(chunk):
        idx = start + i
        name = getattr(track, "name", None) or getattr(track, "track_name", None)
        if isinstance(track, dict):
            name = track.get("name") or track.get("track_name") or name
        name = (name or f"Трек {idx + 1}").strip()
        if idx in unavailable:
            label = f"⛔ {name}"
            cb = f"du:{session_key}:{idx}"
        else:
            label = f"🎵 {name}"
            cb = f"dt:{session_key}:{idx}"
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=cb[:64],
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"dp:{session_key}:{page - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Вперед",
                callback_data=f"dp:{session_key}:{page + 1}"[:64],
            )
        )
    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                text="📦 Скачать весь альбом",
                callback_data=f"dz:{session_key}"[:64],
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"bk:dl:{session_key}"[:64],
            ),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def unavailable_free_kb(artist: str, title: str) -> InlineKeyboardMarkup:
    """Кнопка поиска на YouTube + меню."""
    from utils import youtube_search_url

    q = f"{(artist or '').strip()} {(title or '').strip()}".strip() or "music"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Найти на YouTube",
                    url=youtube_search_url(q),
                )
            ],
            [
                InlineKeyboardButton(
                    text="🏠 Меню",
                    callback_data="mode:menu",
                )
            ],
        ]
    )


def artists_kb(artists: Iterable[tuple[str, str]]) -> InlineKeyboardMarkup:
    """artists: (label, callback_data)."""
    rows = [
        [InlineKeyboardButton(text=label[:64], callback_data=cb)]
        for label, cb in artists
    ]
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:menu"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def playlists_menu_kb() -> InlineKeyboardMarkup:
    """Устарело: раньше хаб плейлистов, теперь только рекомендации."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=BTN_RECOMMEND,
                    callback_data="pl:reco",
                )
            ],
            [InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu")],
        ]
    )


def user_playlists_kb(playlists: Sequence) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text="➕ Создать плейлист",
                callback_data="pl:new",
            )
        ]
    ]
    for p in playlists:
        pid = getattr(p, "id", 0)
        name = getattr(p, "name", "") or "Плейлист"
        cnt = int(getattr(p, "track_count", 0) or 0)
        label = f"📁 {name} ({cnt})"
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"pl:open:{pid}"[:64],
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pl:rmpl:{pid}"[:64],
                ),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text=BTN_RECOMMEND, callback_data="pl:reco"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def user_playlist_tracks_kb(
    tracks: Sequence,
    *,
    playlist_id: int,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in tracks:
        tid = getattr(t, "id", 0)
        name = getattr(t, "track_name", "") or "Трек"
        artist = getattr(t, "artist", "") or ""
        label = f"{name} – {artist}".strip(" –")
        if len(label) > 48:
            label = label[:45] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶ {label}",
                    callback_data=f"pl:uplay:{tid}"[:64],
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pl:udel:{playlist_id}:{tid}"[:64],
                ),
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"pl:open:{playlist_id}:{page - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"pl:open:{playlist_id}:{page + 1}"[:64],
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="➕ Из скачанных",
                callback_data=f"pl:addpick:{playlist_id}:0"[:64],
            )
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="🎛 Мои", callback_data="pl:mine"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pick_recent_for_playlist_kb(
    tracks: Sequence,
    *,
    playlist_id: int,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """Выбор скачанного трека → добавить в плейлист."""
    rows: list[list[InlineKeyboardButton]] = []
    for t in tracks:
        tid = getattr(t, "id", 0)
        name = getattr(t, "track_name", "") or "Трек"
        artist = getattr(t, "artist", "") or ""
        label = f"➕ {name} – {artist}".strip(" –+")
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"pl:add:{playlist_id}:{tid}"[:64],
                )
            ]
        )
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️",
                callback_data=f"pl:addpick:{playlist_id}:{page - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️",
                callback_data=f"pl:addpick:{playlist_id}:{page + 1}"[:64],
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(
                text="⬅️ К плейлисту",
                callback_data=f"pl:open:{playlist_id}:0"[:64],
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recent_playlist_kb(
    tracks: Sequence,
    *,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    """
    pl:play:<id> — переслать по file_id
    pl:del:<id> — удалить
    pl:to:<id> — добавить в мой плейлист
    pl:recent:<page> — страница
    """
    rows: list[list[InlineKeyboardButton]] = []
    for t in tracks:
        tid = getattr(t, "id", 0)
        name = getattr(t, "track_name", "") or "Трек"
        artist = getattr(t, "artist", "") or ""
        label = f"{name} – {artist}".strip(" –")
        if len(label) > 40:
            label = label[:37] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"▶ {label}",
                    callback_data=f"pl:play:{tid}"[:64],
                ),
                InlineKeyboardButton(
                    text="📁",
                    callback_data=f"pl:to:{tid}"[:64],
                ),
                InlineKeyboardButton(
                    text="🗑",
                    callback_data=f"pl:del:{tid}"[:64],
                ),
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"pl:recent:{page - 1}",
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Вперед",
                callback_data=f"pl:recent:{page + 1}",
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text=BTN_RECOMMEND, callback_data="pl:reco"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choose_playlist_kb(
    playlists: Sequence, *, recent_id: int
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for p in playlists:
        pid = getattr(p, "id", 0)
        name = getattr(p, "name", "") or "Плейлист"
        label = f"📁 {name}"
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=label,
                    callback_data=f"pl:put:{pid}:{recent_id}"[:64],
                )
            ]
        )
    if not playlists:
        rows.append(
            [
                InlineKeyboardButton(
                    text="➕ Создать плейлист",
                    callback_data="pl:new",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="pl:recent:0")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def recommendations_kb(items: Sequence[tuple[str, str]]) -> InlineKeyboardMarkup:
    """items: (label, callback_data)."""
    rows = [
        [InlineKeyboardButton(text=label[:64], callback_data=cb[:64])]
        for label, cb in items
    ]
    rows.append(
        [InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lyrics_page_kb(
    hits: Sequence,
    *,
    page: int,
    total_pages: int,
    session_key: str,
    per_page: int = ALBUMS_PER_PAGE,
) -> InlineKeyboardMarkup:
    """
    Результаты поиска по тексту песни.
    ls:<session>:<idx> — выбрать
    lp:<session>:<page> — страница
    """
    total = len(hits)
    total_pages = pages_count(total, per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    chunk = list(hits)[start : start + per_page]

    rows: list[list[InlineKeyboardButton]] = []
    for i, hit in enumerate(chunk):
        idx = start + i
        label = getattr(hit, "button_label", None) or str(hit)
        if len(label) > 64:
            label = label[:61] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🎵 {label}"[:64],
                    callback_data=f"ls:{session_key}:{idx}"[:64],
                )
            ]
        )

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton(
                text="⬅️ Назад",
                callback_data=f"lp:{session_key}:{page - 1}"[:64],
            )
        )
    nav.append(
        InlineKeyboardButton(
            text=f"{page + 1}/{max(total_pages, 1)}",
            callback_data="noop",
        )
    )
    if page + 1 < total_pages:
        nav.append(
            InlineKeyboardButton(
                text="➡️ Вперед",
                callback_data=f"lp:{session_key}:{page + 1}"[:64],
            )
        )
    if nav:
        rows.append(nav)
    rows.append(
        [
            InlineKeyboardButton(text="⬅️ Назад", callback_data="mode:menu"),
            InlineKeyboardButton(text="🏠 Меню", callback_data="mode:menu"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pages_count(total: int, per_page: int) -> int:
    if total <= 0:
        return 1
    return (total + per_page - 1) // per_page
