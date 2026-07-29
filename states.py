"""FSM-состояния режимов поиска."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class SearchMode(StatesGroup):
    menu = State()
    text = State()
    cover = State()
    tops = State()
    # фильтр альбомов внутри дискографии артиста
    artist_filter = State()
    # ожидание ссылки Apple / Spotify / Genius / YouTube…
    link = State()
    # создание пользовательского плейлиста — ждём имя
    playlist_name = State()
