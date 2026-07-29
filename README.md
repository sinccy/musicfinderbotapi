# Advanced Telegram Music Bot

Полнофункциональный музыкальный бот:

- 🏠 Главное меню с режимами  
- 🔍 Поиск по названию / артисту (пагинация 7 шт., новые сверху)  
- 🖼 Поиск по обложке (OCR + pHash)  
- 🔤 Поиск по строке из текста песни (Genius)  
- 📊 Топы Genius (+ fallback Apple Music)  
- ⬇ Скачивание MP3: выбор трека или ZIP альбома (yt-dlp)  
- Ссылки: YouTube, Spotify, Apple Music, Яндекс.Музыка  

## Требования

- **Python 3.10–3.12** (не 3.14)  
- Желательно **ffmpeg** в PATH (для MP3 и сжатия)  
- VPN, если `api.telegram.org` недоступен  

## Установка (macOS)

```bash
cd путь/к/проекту
chmod +x start_bot.sh
./start_bot.sh
```

Или вручную:

```bash
brew install python@3.12 ffmpeg
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # заполните BOT_TOKEN и OCR_SPACE_API_KEY
.venv/bin/python bot.py
```

Для скачивания YouTube на Mac лучше:
`YTDLP_COOKIES_FROM_BROWSER=safari` (или `chrome`).

Опционально `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` —
с февраля 2026 для Web API нужен Spotify Premium у владельца app.
Без ключей скачивание идёт через Deezer + YouTube Music.

## Установка (Windows)

1. Дважды кликните `start_bot.bat`  
   или вручную:

```powershell
cd путь\к\проекту
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
# заполните BOT_TOKEN и OCR_SPACE_API_KEY
.\.venv\Scripts\python.exe bot.py
```

## Переменные (.env)

| Переменная | Обязательно | Описание |
|---|---|---|
| `BOT_TOKEN` | да | @BotFather |
| `OCR_SPACE_API_KEY` | да | OCR.space free key |
| `DEFAULT_COUNTRY` | нет | `ru` |
| `TELEGRAM_PROXY` | нет | socks5/http прокси |
| `SPOTIFY_CLIENT_ID/SECRET` | нет | прямые Spotify-ссылки |
| `YANDEX_MUSIC_TOKEN` | нет | точнее Яндекс.Музыка |

## Структура

```
bot.py          — handlers, режимы, меню
config.py       — env
keyboards.py    — inline-клавиатуры
ocr.py          — OCR.space + очистка
parser.py       — кандидаты из OCR
music.py        — iTunes / MusicBrainz
lyrics.py       — поиск по тексту (Genius)
charts.py       — Genius + Apple fallback
download.py     — yt-dlp MP3
links.py        — платформенные ссылки
cache.py        — TTL-кэш + session
utils.py        — хелперы
```

## Команды

- `/start` — меню  
- `/top` — Genius  
- `/country ru` — регион  
- `/newreleases` — свежие альбомы  
- фото / голос / текст — авто-режим  

## Замечания

- Genius chart API/HTML может меняться — есть fallback на Apple most-played.  
- Скачивание зависит от доступности YouTube и ffmpeg.  
- Соблюдайте авторские права в вашей юрисдикции.  
