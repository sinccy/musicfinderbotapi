"""
Парсинг OCR-текста с обложек: удаление «мусорных» фраз и генерация
кандидатных поисковых запросов (от более уверенных к менее).
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)

# Известные юридические / альтернативные имена → сценическое имя в каталогах
ARTIST_ALIASES: dict[str, str] = {
    "tyler okonma": "Tyler, The Creator",
    "tyler the creator": "Tyler, The Creator",
    "ye": "Kanye West",
    "kanye": "Kanye West",
    "kanye omari west": "Kanye West",
    "sean carter": "Jay-Z",
    "shawn carter": "Jay-Z",
    "aubrey graham": "Drake",
    "belcalis almanzar": "Cardi B",
    "robyn fenty": "Rihanna",
    "onika maraj": "Nicki Minaj",
    "jacques webster": "Travis Scott",
    "marshall mathers": "Eminem",
    "slim shady": "Eminem",
    "calvin broadus": "Snoop Dogg",
    "andre benjamin": "Andre 3000",
    "donald glover": "Childish Gambino",
    "boris brejha": "Boris Brejcha",
    "boris breicha": "Boris Brejcha",
    "boris brejcha": "Boris Brejcha",
    "future hendrix": "Future",
    "nayvadius wilburn": "Future",
    "og buda": "OG Buda",
    "ог буда": "OG Buda",
    "ogбуда": "OG Buda",
    # huzzy b — фанатские написания
    "хази б": "huzzy b",
    "хаззи б": "huzzy b",
    "хаззиб": "huzzy b",
    "хазиб": "huzzy b",
    "хази": "huzzy b",
    "хаззи": "huzzy b",
    "хуззи б": "huzzy b",
    "хузи б": "huzzy b",
    "hazi b": "huzzy b",
    "hazzi b": "huzzy b",
    "hazy b": "huzzy b",
    "huzzi b": "huzzy b",
    "huzy b": "huzzy b",
    "khazi b": "huzzy b",
    "khazzi b": "huzzy b",
    "huzzy": "huzzy b",
    # FRIENDLY THUG 52 NGG — фанатский транслит / укорочения
    "friendly thug 52 ngg": "FRIENDLY THUG 52 NGG",
    "friendly thug 52": "FRIENDLY THUG 52 NGG",
    "friendly thug52": "FRIENDLY THUG 52 NGG",
    "friendly thug": "FRIENDLY THUG 52 NGG",
    "friendlythug": "FRIENDLY THUG 52 NGG",
    "friendlythug52": "FRIENDLY THUG 52 NGG",
    "thug 52 ngg": "FRIENDLY THUG 52 NGG",
    "thug52": "FRIENDLY THUG 52 NGG",
    "френдли таг 52 ngg": "FRIENDLY THUG 52 NGG",
    "френдли таг 52": "FRIENDLY THUG 52 NGG",
    "френдли таг52": "FRIENDLY THUG 52 NGG",
    "френдли таг": "FRIENDLY THUG 52 NGG",
    "френдли траг": "FRIENDLY THUG 52 NGG",
    "френдли траг 52": "FRIENDLY THUG 52 NGG",
    "френдлитаг": "FRIENDLY THUG 52 NGG",
    "френдлитраг": "FRIENDLY THUG 52 NGG",
    "frendli tag": "FRIENDLY THUG 52 NGG",
    "frendly tag": "FRIENDLY THUG 52 NGG",
    "frendli thug": "FRIENDLY THUG 52 NGG",
    "frendly thug": "FRIENDLY THUG 52 NGG",
    "frendli trag": "FRIENDLY THUG 52 NGG",
    "frendly trag": "FRIENDLY THUG 52 NGG",
    # похожие «короткое фанатское ↔ длинное каталожное»
    "alblak 52": "ALBLAK 52",
    "alblak52": "ALBLAK 52",
    "алблак 52": "ALBLAK 52",
    "алблак": "ALBLAK 52",
    "alblak": "ALBLAK 52",
    # Markul
    "markul": "Markul",
    "маркул": "Markul",
    "маркуль": "Markul",
    "markül": "Markul",
}

# Фанатский EN после кривого транслита (френдли таг → frendli tag)
_FAN_LATIN_FIXES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bfrendl[iy]\b", re.I), "friendly"),
    (re.compile(r"\bfriendli\b", re.I), "friendly"),
    (re.compile(r"\btag\b", re.I), "thug"),
    (re.compile(r"\btrag\b", re.I), "thug"),
    (re.compile(r"\btug\b", re.I), "thug"),
    (re.compile(r"\bthug52\b", re.I), "thug 52"),
)

# Стоп-фразы и кредиты, часто попадающие с оборота обложки
_STOP_PHRASE_RES: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\ball\s+songs?\b",
        r"\bwritten\b",
        r"\bproduced\b",
        r"\barranged\b",
        r"\bcomposed\b",
        r"\bengineered\b",
        r"\brecorded\b",
        r"\bmixed\b",
        r"\bmastered\b",
        r"\bperformed\b",
        r"\bfeat\.?\b",
        r"\bft\.?\b",
        r"\bfeaturing\b",
        r"\bdeluxe\s+edition\b",
        r"\bexpanded\s+edition\b",
        r"\bremaster(ed)?\b",
        r"\bspecial\s+edition\b",
        r"\blimited\s+edition\b",
        r"\bbonus\s+tracks?\b",
        r"\bexplicit\s+(content|lyrics)?\b",
        r"\bparental\s+advisory\b",
        r"\ball\s+rights\s+reserved\b",
        r"\bcopyright\b",
        r"\bcompact\s+disc\b",
        r"\bvinyl\b",
        r"\bstereo\b",
        r"\bmono\b",
        # Артикли (the/of) намеренно НЕ удаляем — иначе ломаются
        # «The Wall», «Tyler, The Creator» и т.п.
        r"\bby\b",
        r"©|®|™",
    )
]

# Целые строки-кредиты вида «ALL SONGS WRITTEN ... BY NAME»
_CREDIT_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"(?:all\s+)?(?:songs?|music|lyrics)?\s*"
    r"(?:written|produced|arranged|composed|performed)"
    r"(?:\s*,?\s*(?:written|produced|arranged|composed|performed))*"
    r"(?:\s+and)?\s+by\s+"
    r")(.+)$"
)

_BY_TAIL_RE = re.compile(r"(?i)\bby\s+([A-ZА-ЯЁ][\wА-Яа-яЁё .,'-]{1,60})\s*$")
_SEPARATOR_SPLIT = re.compile(r"\s*[-–—|:•·/\\]\s+")
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_MULTI_SPACE = re.compile(r"\s+")
# Последовательности CAPS / Capitalized слов — вероятные имена
_CAPS_PHRASE_RE = re.compile(
    r"\b(?:[A-ZА-ЯЁ]{2,}(?:\s+[A-ZА-ЯЁ]{2,}){0,5}"
    r"|[A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){0,5})\b"
)


def _normalize_spaces(text: str) -> str:
    return _MULTI_SPACE.sub(" ", text).strip(" ,.;:-–—|")


def _strip_stop_phrases(text: str) -> str:
    cleaned = text
    for pattern in _STOP_PHRASE_RES:
        cleaned = pattern.sub(" ", cleaned)
    cleaned = _YEAR_RE.sub(" ", cleaned)
    return _normalize_spaces(cleaned)


def _apply_alias(name: str) -> str | None:
    key = _normalize_spaces(name).lower()
    return ARTIST_ALIASES.get(key)


# --- транслит латиница ↔ кириллица (og buda ↔ ог буда) ---
_LAT_TO_CYR_MULTI: tuple[tuple[str, str], ...] = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("yo", "ё"),
    ("yu", "ю"),
    ("ya", "я"),
    ("ja", "я"),
    ("ju", "ю"),
    ("je", "е"),
    ("ye", "е"),
    ("yi", "и"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
)
_LAT_TO_CYR = {
    "a": "а",
    "b": "б",
    "c": "к",
    "d": "д",
    "e": "е",
    "f": "ф",
    "g": "г",
    "h": "х",
    "i": "и",
    "j": "й",
    "k": "к",
    "l": "л",
    "m": "м",
    "n": "н",
    "o": "о",
    "p": "п",
    "q": "к",
    "r": "р",
    "s": "с",
    "t": "т",
    "u": "у",
    "v": "в",
    "w": "в",
    "x": "кс",
    "y": "и",
    "z": "з",
}
_CYR_TO_LAT_MULTI: tuple[tuple[str, str], ...] = (
    ("щ", "shch"),
    ("ё", "yo"),
    ("ю", "yu"),
    ("я", "ya"),
    ("ж", "zh"),
    # «х» → h (не kh): хази → hazi, не khazi — так чаще ищут в iTunes
    ("ц", "ts"),
    ("ч", "ch"),
    ("ш", "sh"),
)
_CYR_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яёА-ЯЁ]", text or ""))


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[a-zA-Z]", text or ""))


def latin_to_cyrillic(text: str) -> str:
    """Фонетический транслит: og buda → ог буда."""
    if not text:
        return ""
    out: list[str] = []
    i = 0
    low = text.lower()
    while i < len(low):
        ch = low[i]
        if ch in " \t-_./":
            out.append(" " if ch in " \t" else ch)
            i += 1
            continue
        matched = False
        for src, dst in _LAT_TO_CYR_MULTI:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                matched = True
                break
        if matched:
            continue
        out.append(_LAT_TO_CYR.get(ch, ch))
        i += 1
    return _normalize_spaces("".join(out))


def cyrillic_to_latin(text: str) -> str:
    """Обратный транслит: ог буда → og buda."""
    if not text:
        return ""
    out: list[str] = []
    i = 0
    low = text.lower()
    while i < len(low):
        ch = low[i]
        if ch in " \t-_./":
            out.append(" " if ch in " \t" else ch)
            i += 1
            continue
        matched = False
        for src, dst in _CYR_TO_LAT_MULTI:
            if low.startswith(src, i):
                out.append(dst)
                i += len(src)
                matched = True
                break
        if matched:
            continue
        out.append(_CYR_TO_LAT.get(ch, ch))
        i += 1
    return _normalize_spaces("".join(out))


def script_variants(text: str) -> list[str]:
    """Варианты написания: латиница ↔ кириллица (+ как есть)."""
    q = _normalize_spaces(text or "")
    if not q:
        return []
    out = [q]
    seen = {q.lower()}

    def _add(s: str) -> None:
        s = _normalize_spaces(s)
        if not s or s.lower() in seen:
            return
        seen.add(s.lower())
        out.append(s)

    if _has_latin(q) and not _has_cyrillic(q):
        _add(latin_to_cyrillic(q))
    if _has_cyrillic(q):
        lat = cyrillic_to_latin(q)
        _add(lat)
        # х → kh альтернатива (h уже в основном транслите)
        if "х" in q.lower() and lat:
            _add(re.sub(r"(^|[^k])h", r"\1kh", lat, count=1))
    if _has_latin(q) and _has_cyrillic(q):
        parts = q.split()
        _add(
            " ".join(
                latin_to_cyrillic(p)
                if _has_latin(p) and not _has_cyrillic(p)
                else p
                for p in parts
            )
        )
        _add(
            " ".join(
                cyrillic_to_latin(p) if _has_cyrillic(p) else p for p in parts
            )
        )
    return out


def _alnum_key(text: str) -> str:
    """og-buda / OG Buda / ог буда → сравнимый ключ без пробелов/знаков."""
    t = (text or "").lower().replace("ё", "е").replace("$", "s")
    return re.sub(r"[^a-z0-9а-я]+", "", t, flags=re.IGNORECASE)


def phonetic_key(text: str) -> str:
    """
    Грубый фонетический ключ для сценических имён.
    huzzy b / хази б / hazzi b → huzib-подобные формы.
    """
    t = (text or "").strip().lower().replace("ё", "е")
    if _has_cyrillic(t):
        t = cyrillic_to_latin(t)
    t = t.replace("kh", "h").replace("zh", "j").replace("ts", "c")
    t = t.replace("y", "i")
    # удвоенные согласные: zz→z, ss→s
    t = re.sub(r"([a-z])\1+", r"\1", t)
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def phonetic_keys(text: str) -> set[str]:
    """Ключи с гибкими гласными (a↔u, i↔e) — хази↔huzzy."""
    base = phonetic_key(text)
    if not base:
        return set()
    keys = {base}
    # убрать одиночную финальную b (хази б / huzzy b)
    if len(base) > 3 and base.endswith("b"):
        keys.add(base[:-1])
    # a↔u на каждой позиции
    extra: set[str] = set()
    for k in list(keys):
        for i, ch in enumerate(k):
            if ch == "a":
                extra.add(k[:i] + "u" + k[i + 1 :])
            elif ch == "u":
                extra.add(k[:i] + "a" + k[i + 1 :])
            elif ch == "i":
                extra.add(k[:i] + "e" + k[i + 1 :])
            elif ch == "e":
                extra.add(k[:i] + "i" + k[i + 1 :])
    keys |= extra
    return keys


def search_term_variants(query: str) -> list[str]:
    """
    Расширенные строки для iTunes search.
    «хази б» → huzzy b (канон первым), hazi b, hazzi b, …
    """
    q = _normalize_spaces(query or "")
    if not q:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = _normalize_spaces(s)
        if not s or s.lower() in seen:
            return
        seen.add(s.lower())
        out.append(s)

    # канон алиаса — сразу первым запросом в iTunes
    canon = _resolve_artist_alias(q)
    if canon:
        _add(canon)

    for v in expand_query_aliases(q):
        _add(v)
        for sv in script_variants(v):
            _add(sv)
        fixed = apply_fan_latin_fixes(
            cyrillic_to_latin(v) if _has_cyrillic(v) else v
        )
        if fixed and fixed.lower() != v.lower():
            _add(fixed)

    # фонетические латинские догадки из кириллицы / кривого транслита
    lat = cyrillic_to_latin(q) if _has_cyrillic(q) else q.lower()
    lat = _normalize_spaces(lat)
    lat_fixed = apply_fan_latin_fixes(lat)
    cores = {lat, lat_fixed}
    parts = lat.split()
    # «б»/b только если пользователь уже написал хвост (хази б),
    # НЕ дописываем «markul b» к обычным артистам
    had_b_suffix = len(parts) >= 2 and parts[-1] in {"b", "б"}
    if had_b_suffix:
        cores.add(" ".join(parts[:-1]))
        cores.add(" ".join(parts[:-1]) + " b")

    for core in list(cores):
        _add(core)
        c = core.lower()
        # удвоение z/s: hazi ↔ hazzi, huzi ↔ huzzy
        _add(re.sub(r"([zs])(?![zs])", r"\1\1", c, count=1))
        _add(re.sub(r"([zs])\1+", r"\1", c))
        if "i" in c:
            _add(c.replace("i", "y", 1))
        if "y" in c:
            _add(c.replace("y", "i", 1))
        # a↔u (хази ↔ huzzy)
        if "a" in c:
            _add(c.replace("a", "u", 1))
        if "u" in c:
            _add(c.replace("u", "a", 1))
        # комбо: a→u и i→y  →  hazi → huzzy
        if "a" in c and "i" in c:
            _add(c.replace("a", "u", 1).replace("i", "y", 1))
        if "u" in c and "y" in c:
            _add(c.replace("u", "a", 1).replace("y", "i", 1))
        # hazi b → huzzy b — только при явном суффиксе b
        if had_b_suffix and (c.endswith(" b") or (len(c) > 1 and c.endswith("b") and " " not in c)):
            stem = c[:-2].rstrip() if c.endswith(" b") else c[:-1]
            if stem:
                _add(stem + " b")
                _add(stem.replace("a", "u", 1).replace("i", "y", 1) + " b")
                _add(re.sub(r"([zs])(?![zs])", r"\1\1", stem, count=1) + " b")

    return out


def match_keys(text: str) -> set[str]:
    """Все ключи для сравнения имени (алиасы + транслит + фонетика)."""
    keys: set[str] = set()
    for v in expand_query_aliases(text) or [text]:
        k = _alnum_key(v)
        if k:
            keys.add(k)
        for sv in script_variants(v):
            sk = _alnum_key(sv)
            if sk:
                keys.add(sk)
        keys |= phonetic_keys(v)
    keys |= phonetic_keys(text)
    return keys


def apply_fan_latin_fixes(text: str) -> str:
    """frendli tag → friendly thug (фанатский транслит)."""
    s = _normalize_spaces(text or "")
    if not s:
        return s
    for pat, repl in _FAN_LATIN_FIXES:
        s = pat.sub(repl, s)
    return _normalize_spaces(s)


def _resolve_artist_alias(name: str) -> str | None:
    """
    Канон из ARTIST_ALIASES.
    Прямое совпадение / кириллица→латиница / fan-fix латиницы.
    Не делаем латиница→кириллица: иначе Khazi B → хази б → huzzy b.
    """
    n = _normalize_spaces(name or "")
    if not n:
        return None
    for cand in (
        n,
        cyrillic_to_latin(n) if _has_cyrillic(n) else "",
        apply_fan_latin_fixes(n),
        apply_fan_latin_fixes(cyrillic_to_latin(n)) if _has_cyrillic(n) else "",
    ):
        if not cand:
            continue
        hit = _apply_alias(cand)
        if hit:
            return hit
    return None


def _prefix_name_score(q_keys: set[str], a_keys: set[str]) -> int:
    """
    Короткий запрос ⊂ длинное имя каталога:
    friendlythug ⊂ friendlythug52ngg.
    Мин. длина 8 — чтобы khazi/hazi не цеплялись к чужим именам.
    """
    best = 0
    for qk in q_keys:
        if len(qk) < 8:
            continue
        for ak in a_keys:
            if len(ak) < 8:
                continue
            if ak.startswith(qk) or qk.startswith(ak):
                # почти полное покрытие
                ratio = min(len(qk), len(ak)) / max(len(qk), len(ak))
                if ratio >= 0.55:
                    best = max(best, 95 if ratio >= 0.7 else 85)
            elif qk in ak and len(qk) >= 10:
                best = max(best, 88)
    return best


def artist_match_score(query: str, artist_name: str) -> int:
    """
    Насколько уверенно запрос = артист.
    100+ — почти наверняка (алиас/точное имя);
    80+ — транслит / префикс (friendly thug ⊂ FRIENDLY THUG 52 NGG);
    ≤55 — только фонетика (не «exact»).
    """
    import difflib

    q = (query or "").strip()
    a = (artist_name or "").strip()
    if not q or not a:
        return 0
    if q.lower() == a.lower():
        return 120
    if len(a) > 48 and "," in a:
        return 0

    # Алиасы только для запроса пользователя (хази б → huzzy b).
    # Не резолвим имя из каталога: иначе Khazi B → huzzy b через alias «khazi b».
    q_alias = _resolve_artist_alias(q)
    if q_alias and q_alias.lower() == a.lower():
        return 115
    if q_alias and _alnum_key(q_alias) == _alnum_key(a):
        return 110

    a_keys = {_alnum_key(v) for v in script_variants(a) if _alnum_key(v)}
    a_keys.add(_alnum_key(a))

    # Есть канон-алиас: сравниваем артиста с каноном (+ префикс)
    if q_alias:
        alias_keys = {
            _alnum_key(v) for v in script_variants(q_alias) if _alnum_key(v)
        }
        alias_keys.add(_alnum_key(q_alias))
        if alias_keys & a_keys:
            return 100
        pref = _prefix_name_score(alias_keys, a_keys)
        if pref:
            return pref
        q_ph = phonetic_keys(q_alias)
        a_ph = phonetic_keys(a)
        if q_ph & a_ph:
            return 55
        return 0

    # транслит/скрипт + fan-fix (ог буда ↔ OG Buda; frendli tag → friendly thug)
    q_variants = list(script_variants(q))
    fixed = apply_fan_latin_fixes(
        cyrillic_to_latin(q) if _has_cyrillic(q) else q
    )
    if fixed:
        q_variants.append(fixed)
        q_variants.extend(script_variants(fixed))
    q_keys = {_alnum_key(v) for v in q_variants if _alnum_key(v)}
    if q_keys & a_keys:
        return 90

    pref = _prefix_name_score(q_keys, a_keys)
    if pref:
        return pref

    # слова запроса (длина ≥3) все встречаются в имени артиста
    q_words = [
        _alnum_key(w)
        for w in re.split(r"\s+", apply_fan_latin_fixes(
            cyrillic_to_latin(q) if _has_cyrillic(q) else q
        ).lower())
        if len(_alnum_key(w)) >= 3
    ]
    a_blob = _alnum_key(a)
    if q_words and all(w in a_blob for w in q_words) and sum(len(w) for w in q_words) >= 8:
        return 88

    q_ph = phonetic_keys(fixed or q)
    a_ph = phonetic_keys(a)
    if q_ph & a_ph:
        core = min(q_ph, key=len)
        if len(core) <= 4:
            return 30
        return 55

    best = 0.0
    for qq in q_ph:
        for aa in a_ph:
            if not qq or not aa:
                continue
            # префикс по фонетике (разница длин ок)
            if len(qq) >= 8 and (aa.startswith(qq) or qq.startswith(aa)):
                return 85
            if abs(len(qq) - len(aa)) > 4:
                continue
            best = max(best, difflib.SequenceMatcher(None, qq, aa).ratio())
    if best >= 0.92:
        return 45
    if best >= 0.88:
        return 35
    return 0


def artist_names_match(query: str, artist_name: str) -> bool:
    """
    True если запрос и имя артиста — одно и то же написание
    (og buda ↔ ог буда; хази б ↔ huzzy b).
    Фонетика-only (Khazi B) — False.
    """
    return artist_match_score(query, artist_name) >= 80


def expand_query_aliases(query: str) -> list[str]:
    """
    Варианты запроса: алиасы + транслит.
    «og buda» → «ог буда»; «Boris Brejha…» → «Boris Brejcha…»
    """
    q = _normalize_spaces(query or "")
    if not q:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def _add(s: str) -> None:
        s = _normalize_spaces(s)
        if not s:
            return
        key = s.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(s)

    def _best_alias_sub(base: str) -> str | None:
        """
        Одна замена — самый длинный ключ.
        «хази б» → huzzy b; не «huzzy» внутри «huzzy b» → huzzy b b.
        """
        low = base.lower()
        direct = _apply_alias(base)
        if direct:
            return direct
        for alias_key, canonical in sorted(
            ARTIST_ALIASES.items(), key=lambda x: -len(x[0])
        ):
            if low == alias_key:
                return canonical
            if not low.startswith(alias_key + " "):
                continue
            rest = base[len(alias_key) :].strip()
            # «huzzy»+«b» при каноне «huzzy b» — не дублируем b
            if rest.lower() in {"b", "б"} and canonical.lower().endswith(" b"):
                return canonical
            return f"{canonical} {rest}".strip()
        return None

    # канон алиаса — первым (важнее для iTunes search)
    direct = _apply_alias(q)
    if direct:
        _add(direct)
    elif _has_cyrillic(q):
        lat = cyrillic_to_latin(q)
        direct = _apply_alias(lat)
        if direct:
            _add(direct)

    for base in script_variants(q):
        _add(base)
        fixed = _best_alias_sub(base)
        if fixed:
            _add(fixed)
        words = base.split()
        for n in (3, 2, 1):
            if len(words) < n:
                continue
            head = " ".join(words[:n])
            rest = " ".join(words[n:])
            alias = _apply_alias(head)
            if alias:
                # не клеим канон + хвост, если канон уже полное имя
                # и хвост — одиночная «б»/b (хази + б → huzzy b б)
                if rest.lower() in {"b", "б"} and alias.lower().endswith(" b"):
                    _add(alias)
                else:
                    _add(f"{alias} {rest}".strip())
            # транслит головы: для чистого артиста (rest пуст) или головы из 2+ слов
            if rest and n < 2:
                continue
            for hv in script_variants(head):
                if hv.lower() == head.lower():
                    continue
                _add(f"{hv} {rest}".strip())
    return out


_NOISE_QUERIES = {
    "album",
    "music",
    "songs",
    "song",
    "tracks",
    "record",
    "records",
    "and",
    "or",
    "the",
    "by",
    "with",
    "from",
    "for",
    "produced",
    "written",
    "arranged",
    "composed",
}


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        key = _normalize_spaces(item)
        if not key or len(key) < 2:
            continue
        low = key.lower()
        if low in seen:
            continue
        # Отбрасываем стоп-слова и обрывки вроде «AND», «AND Tyler…»
        tokens = [t for t in low.replace(",", " ").split() if t]
        if not tokens or all(t in _NOISE_QUERIES for t in tokens):
            continue
        if tokens[0] in {"and", "or", "by", "with", "from", "for"}:
            continue
        if tokens[-1] in {"and", "or", "by"}:
            continue
        seen.add(low)
        out.append(key)
    return out


def extract_search_query(ocr_text: str) -> list[str]:
    """
    Извлекает список кандидатных поисковых запросов из OCR-текста.

    Порядок — по убыванию уверенности.
    Пример:
        «ALL SONGS WRITTEN, PRODUCED AND ARRANGED BY TYLER OKONMA»
        → ["Tyler, The Creator", "TYLER OKONMA", "TYLER", "OKONMA", ...]
    """
    if not ocr_text or not ocr_text.strip():
        return []

    raw = ocr_text.replace("\r", "\n")
    lines = [_normalize_spaces(ln) for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln]

    candidates: list[str] = []
    names_from_credits: list[str] = []
    meaningful_lines: list[str] = []

    for line in lines:
        credit = _CREDIT_LINE_RE.match(line)
        if credit:
            name = _normalize_spaces(credit.group(1))
            if name:
                names_from_credits.append(name)
                alias = _apply_alias(name)
                if alias:
                    candidates.append(alias)
                candidates.append(name)
            continue

        by_tail = _BY_TAIL_RE.search(line)
        if by_tail and re.search(r"(?i)\b(written|produced|arranged|composed)\b", line):
            name = _normalize_spaces(by_tail.group(1))
            if name:
                names_from_credits.append(name)
                alias = _apply_alias(name)
                if alias:
                    candidates.append(alias)
                candidates.append(name)
            # дальше пробуем вытащить остаток строки без кредита
            remainder = _strip_stop_phrases(_BY_TAIL_RE.sub(" ", line))
            if remainder:
                meaningful_lines.append(remainder)
            continue

        cleaned_line = _strip_stop_phrases(line)
        if cleaned_line:
            meaningful_lines.append(cleaned_line)
        elif _CAPS_PHRASE_RE.search(line):
            # Даже «шумная» строка может содержать CAPS-имя
            for m in _CAPS_PHRASE_RE.finditer(line):
                phrase = _strip_stop_phrases(m.group(0))
                if phrase:
                    meaningful_lines.append(phrase)

    # Разделители «Artist – Album»
    for line in meaningful_lines:
        parts = [p for p in _SEPARATOR_SPLIT.split(line) if p.strip()]
        if len(parts) == 2:
            left, right = (_normalize_spaces(parts[0]), _normalize_spaces(parts[1]))
            for side in (left, right):
                alias = _apply_alias(side)
                if alias:
                    candidates.append(alias)
            candidates.append(f"{left} {right}")
            candidates.append(f"{right} {left}")
            candidates.append(left)
            candidates.append(right)

    # Первые осмысленные строки как есть + комбинации
    for line in meaningful_lines[:4]:
        alias = _apply_alias(line)
        if alias:
            candidates.append(alias)
        candidates.append(line)

    if len(meaningful_lines) >= 2:
        a, b = meaningful_lines[0], meaningful_lines[1]
        candidates.append(f"{a} {b}")
        candidates.append(f"{b} {a}")
        alias_a = _apply_alias(a)
        if alias_a:
            candidates.append(f"{alias_a} {b}")
            candidates.append(f"{b} {alias_a}")

    # Имена из кредитов + возможные названия альбома из других строк
    for name in names_from_credits:
        alias = _apply_alias(name) or name
        for line in meaningful_lines[:3]:
            if line.lower() not in name.lower():
                candidates.append(f"{alias} {line}")
                candidates.append(f"{line} {alias}")

    # Отдельные токены / CAPS-фразы из всего текста
    flat = _strip_stop_phrases(" ".join(lines))
    for m in _CAPS_PHRASE_RE.finditer(flat):
        phrase = _normalize_spaces(m.group(0))
        if phrase:
            alias = _apply_alias(phrase)
            if alias:
                candidates.append(alias)
            candidates.append(phrase)

    # Разбиение многословных имён: «TYLER OKONMA» → «TYLER», «OKONMA»
    for name in list(names_from_credits) + meaningful_lines[:2]:
        tokens = name.split()
        if len(tokens) >= 2:
            candidates.append(tokens[0])
            candidates.append(tokens[-1])
            if len(tokens) >= 3:
                candidates.append(" ".join(tokens[:2]))

    # Если почти всё вычистили — используем укороченный сырой текст
    if not candidates and flat:
        candidates.append(flat[:80])

    result = _unique(candidates)
    logger.info(
        "OCR candidates (%d): %s | raw=%r",
        len(result),
        result[:12],
        ocr_text[:200],
    )
    return result
