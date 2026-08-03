"""Простая локализация UI (ru / en) и определение CIS по language_code Telegram."""

from __future__ import annotations

from typing import Optional

SUPPORTED_LANGS = ("ru", "en")
DEFAULT_LANG = "ru"

# Языки Telegram, типичные для СНГ / постсоветского региона → русский без выбора
CIS_LANGUAGE_CODES = frozenset(
    {
        "ru",  # Россия
        "uk",  # Украина
        "be",  # Беларусь
        "kk",  # Казахстан
        "uz",  # Узбекистан
        "ky",  # Кыргызстан
        "tg",  # Таджикистан
        "tk",  # Туркменистан
        "az",  # Азербайджан
        "hy",  # Армения
        "ka",  # Грузия
        "mo",  # Молдова (редко)
        "ro",  # часто у пользователей из Молдовы
    }
)

_STRINGS: dict[str, dict[str, str]] = {
    "welcome": {
        "ru": (
            "🎧 <b>PROJECT COVER</b>\n\n"
            "Просто пришлите в чат:\n"
            "• <b>название</b> артиста, альбома или трека\n"
            "• <b>ссылку</b> (Apple, Spotify, YouTube, Genius…)\n\n"
            "Кнопки меню — по желанию, искать можно сразу без них.\n"
            "<i>🖼 Поиск по обложке — пока в разработке.</i>"
        ),
        "en": (
            "🎧 <b>PROJECT COVER</b>\n\n"
            "Just send in the chat:\n"
            "• an artist, album, or <b>track name</b>\n"
            "• a <b>link</b> (Apple, Spotify, YouTube, Genius…)\n\n"
            "Menu buttons are optional — you can search right away.\n"
            "<i>🖼 Cover search is under development.</i>"
        ),
    },
    "menu_prompt": {
        "ru": "Пришлите название или ссылку — или выберите режим:",
        "en": "Send a title or link — or choose a mode:",
    },
    "choose_language": {
        "ru": (
            "🌐 <b>Choose your language / Выберите язык</b>\n\n"
            "This choice will be saved for your account."
        ),
        "en": (
            "🌐 <b>Choose your language / Выберите язык</b>\n\n"
            "This choice will be saved for your account."
        ),
    },
    "lang_saved": {
        "ru": "✅ Язык сохранён: русский",
        "en": "✅ Language saved: English",
    },
    "btn_start": {"ru": "🏠 Главное меню", "en": "🏠 Main menu"},
    "btn_settings": {"ru": "⚙️ Настройки", "en": "⚙️ Settings"},
    "btn_recommend": {"ru": "✨ Рекомендации", "en": "✨ Recommendations"},
    "btn_help": {"ru": "❓ Справка", "en": "❓ Help"},
    "reply_placeholder": {
        "ru": "Поиск: артист, альбом или трек…",
        "en": "Search: artist, album or track…",
    },
    "menu_text_search": {
        "ru": "🔍 Поиск по названию",
        "en": "🔍 Search by title",
    },
    "menu_link_search": {
        "ru": "🔗 Поиск по ссылке",
        "en": "🔗 Search by link",
    },
    "menu_cover_search": {
        "ru": "🖼 Поиск по обложке (в разработке)",
        "en": "🖼 Cover search (coming soon)",
    },
    "menu_tops": {
        "ru": "📊 Топ чарт (Genius)",
        "en": "📊 Top chart (Genius)",
    },
    "menu_releases": {
        "ru": "🗓 Недельные релизы",
        "en": "🗓 Weekly releases",
    },
    "help": {
        "ru": (
            "📖 <b>Справка</b>\n\n"
            "/start — главное меню\n"
            "/top — топ чарт Genius\n"
            "/newreleases — недельные релизы\n\n"
            "Режимы: название, ссылка, топ, релизы.\n"
            "🖼 Поиск по обложке — пока в разработке.\n"
            "После альбома — ссылки на платформы и ⬇ MP3 "
            "(выбор трека или ZIP).\n"
            "✨ Рекомендации — подборки по недавно скачанным трекам.\n\n"
            "Нижние кнопки: меню, настройки, рекомендации, справка."
        ),
        "en": (
            "📖 <b>Help</b>\n\n"
            "/start — main menu\n"
            "/top — Genius top chart\n"
            "/newreleases — weekly releases\n\n"
            "Modes: title, link, top, releases.\n"
            "🖼 Cover search is under development.\n"
            "After an album — platform links and ⬇ MP3 "
            "(pick a track or ZIP).\n"
            "✨ Recommendations — based on your recent downloads.\n\n"
            "Bottom buttons: menu, settings, recommendations, help."
        ),
    },
    "settings_title": {
        "ru": "⚙️ <b>Настройки</b>\nКачество MP3: <code>{quality} kbps</code>\nЯзык: <code>{lang}</code>",
        "en": "⚙️ <b>Settings</b>\nMP3 quality: <code>{quality} kbps</code>\nLanguage: <code>{lang}</code>",
    },
    "settings_choose": {
        "ru": "Выберите параметр:",
        "en": "Choose an option:",
    },
    "set_q_192": {"ru": "Качество 192 kbps", "en": "Quality 192 kbps"},
    "set_q_128": {"ru": "Качество 128 kbps", "en": "Quality 128 kbps"},
    "set_language": {"ru": "🌐 Язык / Language", "en": "🌐 Language"},
    "btn_menu": {"ru": "🏠 Меню", "en": "🏠 Menu"},
    "btn_main_menu": {"ru": "🏠 Главное меню", "en": "🏠 Main menu"},
    "btn_back": {"ru": "⬅️ Назад", "en": "⬅️ Back"},
    "mode_text": {
        "ru": (
            "🔍 <b>Поиск по названию</b>\n"
            "Введите артиста, альбом или трек "
            "<i>(можно сразу, без этой кнопки)</i>:"
        ),
        "en": (
            "🔍 <b>Search by title</b>\n"
            "Enter an artist, album, or track "
            "<i>(you can send it anytime, without this button)</i>:"
        ),
    },
    "mode_cover": {
        "ru": (
            "🖼 <b>Поиск по обложке</b>\n\n"
            "⏳ Пока в разработке.\n"
            "Сейчас ищите по <b>названию</b> или <b>ссылке</b>."
        ),
        "en": (
            "🖼 <b>Cover search</b>\n\n"
            "⏳ Coming soon.\n"
            "For now, search by <b>title</b> or <b>link</b>."
        ),
    },
    "mode_link": {
        "ru": (
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
            "<code>https://youtu.be/…</code>"
        ),
        "en": (
            "🔗 <b>Search by link</b>\n\n"
            "Send a link to a track, album, or artist:\n"
            "• Apple Music / iTunes\n"
            "• Spotify\n"
            "• YouTube\n"
            "• Genius\n"
            "• Yandex Music\n\n"
            "<i>You can paste a link anytime — the button is optional.</i>\n\n"
            "<i>Examples:</i>\n"
            "<code>https://genius.com/albums/…</code>\n"
            "<code>https://open.spotify.com/track/…</code>\n"
            "<code>https://youtu.be/…</code>"
        ),
    },
    "recommend_title": {"ru": "✨ <b>Рекомендации</b>", "en": "✨ <b>Recommendations</b>"},
    "lang_label_ru": {"ru": "Русский", "en": "Russian"},
    "lang_label_en": {"ru": "English", "en": "English"},
    "set_referral": {"ru": "🎁 Рефералка / сезон", "en": "🎁 Referral season"},
    "btn_referral": {"ru": "🎁 Рефералка", "en": "🎁 Referral"},
    "ref_title": {
        "ru": (
            "🎁 <b>{season}</b>\n"
            "📅 {dates}\n"
            "Приз сезона: <b>NFT-подарок Telegram</b> топ-{winners} пригласившим.\n\n"
            "Ваша ссылка:\n<code>{link}</code>\n\n"
            "📊 Ваши рефералы:\n"
            "• засчитано: <b>{qualified}</b>\n"
            "• в процессе: <b>{pending}</b>\n\n"
            "<b>Как засчитывается друг</b>\n"
            "1) перешёл по вашей ссылке\n"
            "2) реально пользуется ботом (≥{min_actions} действий: поиск/скачивание)\n"
            "3) активность растянута минимум на <b>{min_days} дн.</b> "
            "(одноразовые и мёртвые аккаунты не считаются)\n\n"
            "В конце сезона список победителей зафиксируем и отправим NFT вручную."
        ),
        "en": (
            "🎁 <b>{season}</b>\n"
            "📅 {dates}\n"
            "Season prize: a <b>Telegram NFT gift</b> for the top-{winners} inviters.\n\n"
            "Your link:\n<code>{link}</code>\n\n"
            "📊 Your referrals:\n"
            "• qualified: <b>{qualified}</b>\n"
            "• pending: <b>{pending}</b>\n\n"
            "<b>How a friend counts</b>\n"
            "1) opens your link\n"
            "2) actually uses the bot (≥{min_actions} actions: search/download)\n"
            "3) activity spans at least <b>{min_days} days</b> "
            "(one-shot / dead accounts don't count)\n\n"
            "At season end we freeze winners and send NFT gifts manually."
        ),
    },
    "ref_no_season": {
        "ru": "🎁 Сейчас нет активного реферального сезона. Следите за анонсом!",
        "en": "🎁 No active referral season right now. Stay tuned!",
    },
    "ref_leaderboard": {
        "ru": "🏆 <b>Лидерборд сезона</b>\n\n{rows}",
        "en": "🏆 <b>Season leaderboard</b>\n\n{rows}",
    },
    "ref_lb_empty": {
        "ru": "Пока пусто — будьте первым!",
        "en": "Empty for now — be the first!",
    },
    "ref_attached": {
        "ru": "✅ Вы привязаны к пригласившему. Реферал засчитается после реальной активности в боте.",
        "en": "✅ Linked to your inviter. The referral counts after real activity in the bot.",
    },
    "ref_already": {
        "ru": "ℹ️ Реферальная привязка уже есть.",
        "en": "ℹ️ Referral link already attached.",
    },
    "ref_self": {
        "ru": "Нельзя пригласить самого себя 🙂",
        "en": "You can't invite yourself 🙂",
    },
    "ref_season_closed": {
        "ru": "Сезон уже закрыт — новые рефералы не принимаются.",
        "en": "Season is closed — new referrals are not accepted.",
    },
    "ref_winner_dm": {
        "ru": (
            "🏆 Поздравляем! Вы в топ-{rank} сезона <b>{season}</b> "
            "({qualified} засчитанных рефералов).\n"
            "Приз: <b>NFT-подарок Telegram</b>. Мы свяжемся / отправим подарок."
        ),
        "en": (
            "🏆 Congrats! You're top-{rank} in season <b>{season}</b> "
            "({qualified} qualified referrals).\n"
            "Prize: a <b>Telegram NFT gift</b>. We'll send it soon."
        ),
    },
    "btn_ref_lb": {"ru": "🏆 Лидерборд", "en": "🏆 Leaderboard"},
    "btn_ref_back": {"ru": "⬅️ К рефералке", "en": "⬅️ Back to referral"},
}


def normalize_lang(code: Optional[str]) -> str:
    raw = (code or "").strip().lower().replace("_", "-")
    if not raw:
        return DEFAULT_LANG
    primary = raw.split("-", 1)[0]
    if primary in SUPPORTED_LANGS:
        return primary
    return DEFAULT_LANG


def is_cis_language(language_code: Optional[str]) -> bool:
    """True, если Telegram language_code относится к СНГ / постсоветскому региону."""
    raw = (language_code or "").strip().lower().replace("_", "-")
    if not raw:
        # Пустой код — не угадываем СНГ; покажем выбор языка
        return False
    primary = raw.split("-", 1)[0]
    return primary in CIS_LANGUAGE_CODES


def t(key: str, lang: Optional[str] = None, **kwargs: object) -> str:
    lang_n = normalize_lang(lang)
    bucket = _STRINGS.get(key) or {}
    text = bucket.get(lang_n) or bucket.get(DEFAULT_LANG) or key
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, ValueError):
            return text
    return text


def lang_display(lang: Optional[str]) -> str:
    code = normalize_lang(lang)
    return "RU" if code == "ru" else "EN"
