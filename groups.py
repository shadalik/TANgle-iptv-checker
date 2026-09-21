#!/usr/bin/env python3
"""Умное объединение групп каналов.

Чистые функции без доступа к БД (данные передаются параметрами),
чтобы их можно было тестировать отдельно.

Приоритет определения группы канала:
    1. group_overrides  - ручная правка по нормализованному имени
    2. group_aliases    - подтверждённые пользователем слияния/переименования
    3. group-title из источника плейлиста (после перевода EN->RU)
    4. категория EPG    - fallback, когда группа пустая или "Другое"

Похожие, но разные названия ("Детские"/"Детям") НЕ сливаются автоматически:
они предлагаются через suggest_merges(), пользователь подтверждает в UI,
решение сохраняется в group_aliases и применяется при следующих импортах.
"""
import re
import difflib
from collections import Counter, defaultdict

# Перевод англоязычных групп (сохранено поведение старого checker_core).
GROUPS_TRANSLATION = {
    "Animation": "Мультфильмы", "Business": "Бизнес", "Classic": "Классика",
    "Comedy": "Комедия", "Cooking": "Кухня", "Culture": "Культура",
    "Documentary": "Документальное", "Education": "Образование",
    "Entertainment": "Развлекательное", "Family": "Семейное", "General": "Общие",
    "Kids": "Детские", "Lifestyle": "Стиль жизни", "Movies": "Кино",
    "Music": "Музыка", "News": "Новости", "Outdoor": "Активный отдых",
    "Religious": "Религия", "Science": "Наука", "Series": "Сериалы",
    "Shop": "Магазин", "Sports": "Спорт", "Travel": "Путешествия",
    "Weather": "Погода", "Undefined": "Другое", "Unknown": "Другое",
}

# Встроенный словарь синонимов: ключ - mech_key(), значение - предлагаемое
# каноническое имя. Используется ТОЛЬКО для предложений (suggest_merges),
# автоматически ничего не применяется.
BUILTIN_SYNONYMS = {
    "детям": "Детские",
    "детские": "Детские",
    "информационные": "Новости",
    "новости": "Новости",
    "общественные": "Общие",
    "основные": "Общие",
    "общие": "Общие",
    "знания": "Познавательные",
    "познавательные": "Познавательные",
    "образовательные": "Познавательные",
    "спортивные": "Спорт",
    "спорт": "Спорт",
    "музыкальные": "Музыка",
    "музыка": "Музыка",
    "развлечение": "Развлечения",
    "развлекательные": "Развлечения",
    "хобби и увлечения": "Хобби",
    "хобби": "Хобби",
    "кино и сериалы": "Кино",
    "кино online": "Кино",
    "кино и сериалы (российские)": "Кино",
    "кино": "Кино",
    "развлекательные (местные)": "Местные",
    "местные": "Местные",
    "христианские": "Религия",
    "религия": "Религия",
    "сериалы": "Сериалы",
    "культура": "Культура",
    "релакс": "Релакс",
    "медитативные": "Медитативные",
}

# Маппинг категорий EPG -> каноническая группа (fallback).
EPG_CATEGORY_GROUPS = {
    "новости": "Новости",
    "news": "Новости",
    "информационная": "Новости",
    "фильм": "Кино",
    "фильмы": "Кино",
    "movie": "Кино",
    "кино": "Кино",
    "сериал": "Сериалы",
    "сериалы": "Сериалы",
    "series": "Сериалы",
    "детям": "Детские",
    "детские": "Детские",
    "детский": "Детские",
    "children": "Детские",
    "мультфильм": "Мультфильмы",
    "мультфильмы": "Мультфильмы",
    "спорт": "Спорт",
    "sports": "Спорт",
    "спортивная": "Спорт",
    "музыка": "Музыка",
    "music": "Музыка",
    "музыкальная": "Музыка",
    "документальный": "Документальное",
    "документальное": "Документальное",
    "documentary": "Документальное",
    "познавательная": "Познавательные",
    "познавательное": "Познавательные",
    "наука": "Наука",
    "развлекательная": "Развлечения",
    "развлекательное": "Развлечения",
    "развлечения": "Развлечения",
    "юмор": "Комедия",
    "комедия": "Комедия",
    "культура": "Культура",
    "религия": "Религия",
    "религиозная": "Религия",
    "путешествия": "Путешествия",
    "погода": "Погода",
    "бизнес": "Бизнес",
    "кухня": "Кухня",
    "кулинарная": "Кухня",
    "образование": "Образование",
    "семейный": "Семейное",
}

EMPTY_GROUPS = {"", "другое", "undefined", "unknown", "other", "misc", "разное"}

FUZZY_THRESHOLD = 0.82


def mech_key(group):
    """Безопасный ключ сравнения групп: регистр/пробелы/ё."""
    if not group:
        return ""
    s = group.strip().lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s)


def translate_group(group_str):
    """Перевод EN-групп и взятие первой части до ';' (старое поведение)."""
    if not group_str:
        return "Другое"
    parts = [p.strip() for p in group_str.split(";")]
    translated = [GROUPS_TRANSLATION.get(p, p) for p in parts]
    return translated[0]


def canonicalize_import(raw_group, aliases=None, auto_synonyms=False):
    """Группа канала при импорте из плейлиста.

    translate -> механическая чистка -> подтверждённые алиасы.
    Если auto_synonyms=True, встроенный словарь BUILTIN_SYNONYMS применяется
    прямо при импорте (например "Развлечение" само уедет в "Развлечения"),
    затем результат снова прогоняется через алиасы, чтобы синоним попал
    в запомненное объединение (например "Кино" -> алиас -> "Фильмы").
    """
    aliases = aliases or {}
    g = translate_group(raw_group).strip()
    g = re.sub(r"\s+", " ", g)
    if not g:
        return "Другое"
    for _ in range(3):
        alias = aliases.get(mech_key(g))
        if alias:
            g = alias
            continue
        if auto_synonyms:
            target = BUILTIN_SYNONYMS.get(mech_key(g))
            if target and target != g and mech_key(target) != mech_key(g):
                g = target
                continue
        break
    return g or "Другое"


def norm_name(name):
    """Нормализованное имя канала (совпадает с app.normalize_channel_name)."""
    if not name:
        return ""
    name = name.lower().strip()
    name = re.sub(r'\(.*?\d+[рp].*?\)', '', name)
    name = re.sub(r'\[.*?\d+[рp].*?\]', '', name)
    name = re.sub(r'\b(hd|fhd|uhd|4k|sd)\b', '', name)
    name = re.sub(r'\(.*?\)', '', name)
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def category_to_group(category, aliases=None):
    """EPG-категория -> группа. Возвращает None, если маппинга нет."""
    if not category:
        return None
    cat = category.strip()
    key = mech_key(cat)
    if key in EPG_CATEGORY_GROUPS:
        target = EPG_CATEGORY_GROUPS[key]
    else:
        # Неизвестная категория: не выдумываем, это не группа
        return None
    if aliases:
        return aliases.get(mech_key(target), target)
    return target


def is_empty_group(group):
    return mech_key(group) in EMPTY_GROUPS


def resolve_output_groups(items, overrides=None, epg_categories=None, aliases=None):
    """Итоговая группа для каждого уникального нормализованного имени.

    items: список dict с ключами name, group_title, response_time_ms (или None).
    Возвращает dict norm_name -> group_title.
    Дубли между источниками голосуют большинством; ничья -> самый быстрый.
    """
    overrides = overrides or {}
    epg_categories = epg_categories or {}
    by_norm = defaultdict(list)
    for ch in items:
        by_norm[norm_name(ch.get("name") or "")].append(ch)

    result = {}
    for norm, dups in by_norm.items():
        if not norm:
            continue
        if norm in overrides:
            result[norm] = overrides[norm]
            continue
        groups = [d.get("group_title") or "Другое" for d in dups]
        if len(dups) == 1:
            winner = groups[0]
        else:
            votes = Counter(groups)
            top = votes.most_common()
            if len(top) > 1 and top[0][1] == top[1][1]:
                # Ничья: группа самого быстрого дубля
                best = min(dups, key=lambda d: d.get("response_time_ms") or 99999)
                winner = best.get("group_title") or "Другое"
            else:
                winner = top[0][0]
        if is_empty_group(winner):
            epg_cat = epg_categories.get(norm)
            mapped = category_to_group(epg_cat, aliases) if epg_cat else None
            result[norm] = mapped or "Другое"
        else:
            if aliases:
                winner = aliases.get(mech_key(winner), winner)
            result[norm] = winner
    return result


# ---------------------------------------------------------------------------
# Отбор каналов для плейлиста и выбор победителя среди дублей.
# Одна и та же логика используется генератором плейлиста и API дублей,
# чтобы пометка "в плейлисте" в UI всегда совпадала с файлом.
# ---------------------------------------------------------------------------

def channel_availability(ch):
    """Доступность 0..1. Непроверенные (total=0) считаются худшими."""
    total = ch.get("total_checks", 0) or 0
    alive = ch.get("alive_checks", 0) or 0
    if total > 0:
        return alive / total
    return 0.0


def passes_playlist_filters(ch, *, pl_fast=True, pl_medium=True, pl_slow=True,
                            min_avail=0, excluded=None):
    if not ch.get("enabled", 1):
        return False
    if excluded and mech_key(ch.get("group_title") or "") in excluded:
        return False
    if not ch.get("is_alive") or ch.get("last_check") is None:
        return False
    ms = ch.get("response_time_ms")
    if ms is None:
        return False
    total = ch.get("total_checks", 0) or 0
    if total > 0 and min_avail and (ch.get("alive_checks", 0) / total * 100) < min_avail:
        return False
    if ms < 1000:
        return bool(pl_fast)
    if ms < 3000:
        return bool(pl_medium)
    return bool(pl_slow)


def pick_winner(copies, strategy="availability"):
    """Победитель среди копий одного канала. copies непустой."""
    if strategy == "speed":
        return min(copies, key=lambda c: c.get("response_time_ms") or 99999)

    def _key(c):
        return (-channel_availability(c), c.get("response_time_ms") or 99999)

    return min(copies, key=_key)


def select_playlist_channels(channels, *, pl_fast=True, pl_medium=True, pl_slow=True,
                             min_avail=0, excluded=None, strategy="availability"):
    """Один победитель на нормализованное имя.

    Возвращает (unique_sorted, winners_by_norm, candidates_by_norm).
    """
    excluded = excluded or set()
    by_norm = defaultdict(list)
    for ch in channels:
        if not passes_playlist_filters(ch, pl_fast=pl_fast, pl_medium=pl_medium,
                                       pl_slow=pl_slow, min_avail=min_avail,
                                       excluded=excluded):
            continue
        key = norm_name(ch.get("name") or "")
        if key:
            by_norm[key].append(ch)
    winners = {k: pick_winner(v, strategy) for k, v in by_norm.items()}
    unique = sorted(winners.values(), key=lambda c: (c.get("name") or "").lower())
    return unique, winners, by_norm


def suggest_merges(groups_with_counts, aliases=None):
    """Предложить объединения похожих групп.

    groups_with_counts: список (name, channels_count).
    aliases: dict mech_key(raw)->canonical - цели прогоняются через алиасы,
        а группы, которые алиас и так сведёт при следующем импорте,
        в предложения не попадают.
    Возвращает список {members:[...], target, channels, confidence, reason}.
    """
    aliases = aliases or {}

    def _alias_target(name):
        a = aliases.get(mech_key(name))
        return a if a else name

    def _already_merged(members, target):
        tk = mech_key(target)
        return bool(members) and all(mech_key(_alias_target(m)) == tk for m in members)

    names = [n for n, _ in groups_with_counts if n]
    counts = dict(groups_with_counts)
    used = set()
    suggestions = []

    # Группы, которые алиас и так сведёт при следующем импорте, не предлагаем
    for n in names:
        a = aliases.get(mech_key(n))
        if a and mech_key(a) != mech_key(n):
            used.add(n)

    # Этап 1: кластеры по встроенному словарю (высокая уверенность)
    target_buckets = defaultdict(list)
    for n in names:
        if n in used:
            continue
        key = mech_key(n)
        target = BUILTIN_SYNONYMS.get(key)
        if not target:
            continue
        target = _alias_target(target)
        if mech_key(target) != key:
            target_buckets[target].append(n)
            used.add(n)
        else:
            used.add(n)  # уже каноническое имя, не трогаем
    for target, members in target_buckets.items():
        # если целевая группа уже существует - тоже включаем её в members
        all_members = sorted(set(members + ([target] if target in names and target not in members else [])),
                             key=lambda m: -counts.get(m, 0))
        if _already_merged(all_members, target):
            continue
        suggestions.append({
            "members": all_members,
            "target": target,
            "channels": sum(counts.get(m, 0) for m in all_members),
            "confidence": "high",
            "reason": "synonym",
        })

    # Этап 2: скобочные варианты "X (Y)" -> "X" или другие группы
    rest = [n for n in names if n not in used]
    for n in rest:
        m = re.match(r"^(.*?)\s*\([^()]*\)\s*$", n)
        if not m:
            continue
        base = m.group(1).strip()
        base_key = mech_key(base)
        partner = None
        for other in names:
            if other != n and mech_key(other) == base_key:
                partner = other
                break
        if partner and partner not in used:
            target = _alias_target(partner)
            members = sorted([n, partner], key=lambda m_: -counts.get(m_, 0))
            if _already_merged(members, target):
                used.add(n)
                used.add(partner)
                continue
            suggestions.append({
                "members": members,
                "target": target,
                "channels": counts.get(n, 0) + counts.get(partner, 0),
                "confidence": "high",
                "reason": "parenthesis",
            })
            used.add(n)
            used.add(partner)

    # Этап 3: fuzzy по схожести строк
    rest = [n for n in rest if n not in used]
    for i, a in enumerate(rest):
        if a in used:
            continue
        cluster = [a]
        ka = mech_key(a)
        for b in rest[i + 1:]:
            if b in used:
                continue
            kb = mech_key(b)
            if difflib.SequenceMatcher(None, ka, kb).ratio() >= FUZZY_THRESHOLD:
                cluster.append(b)
        if len(cluster) > 1:
            target = _alias_target(max(cluster, key=lambda m: counts.get(m, 0)))
            members = sorted(cluster, key=lambda m: -counts.get(m, 0))
            if _already_merged(members, target):
                used.update(cluster)
                continue
            suggestions.append({
                "members": members,
                "target": target,
                "channels": sum(counts.get(m, 0) for m in cluster),
                "confidence": "medium",
                "reason": "similar",
            })
            used.update(cluster)

    suggestions.sort(key=lambda s: -s["channels"])
    return suggestions
