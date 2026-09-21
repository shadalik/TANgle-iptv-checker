#!/usr/bin/env python3
import json
import os
import re
import time
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor
import database as db
import groups as groups_module

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux) IPTV-Checker"}


def translate_group(group_str):
    # Совместимость: перевод EN-групп теперь в groups.translate_group.
    return groups_module.translate_group(group_str)


def clean_title(title):
    title = re.sub(r'\(.*?\d+[рp].*?\)', '', title)
    title = re.sub(r'\[.*?\d+[рp].*?\]', '', title)
    title = re.sub(r'\b(hd|fhd|uhd|4k|sd)\b', '', title, flags=re.IGNORECASE)
    title = re.sub(r'\(.*?\)', '', title)
    title = re.sub(r'\[.*?\]', '', title)
    return re.sub(r'\s+', ' ', title).strip()


def parse_m3u(text):
    channels = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    i = 0
    while i < len(lines):
        if lines[i].startswith("#EXTINF:"):
            inf_line = lines[i]
            group_name = "Другое"
            group_match = re.search(r'group-title="([^"]+)"', inf_line)
            if group_match:
                group_name = translate_group(group_match.group(1))
            curr = i + 1
            while curr < len(lines) and lines[curr].startswith("#"):
                if lines[curr].startswith("#EXTGRP:"):
                    group_name = translate_group(lines[curr].replace("#EXTGRP:", "").strip())
                curr += 1
            title_match = re.search(r",([^,]+)$", inf_line)
            raw_title = title_match.group(1).strip() if title_match else "Unknown"
            clean_name = clean_title(raw_title)
            inf_clean = re.sub(r'group-title="[^"]*"', f'group-title="{group_name}"', inf_line)
            if 'group-title' not in inf_clean:
                inf_clean = inf_clean.replace("#EXTINF:", f'#EXTINF: group-title="{group_name}" ', 1)
            if curr < len(lines):
                channels.append({
                    "inf_line": inf_clean,
                    "name": clean_name if clean_name else raw_title,
                    "url": lines[curr],
                    "group_title": group_name,
                })
                i = curr
        i += 1
    return channels


def fetch_source(url):
    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=Retry(total=2)))
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=2)))
    r = session.get(url, timeout=15, headers=HEADERS)
    r.raise_for_status()
    return r.text


def check_single_channel(session, url, timeout=10):
    start = time.monotonic()
    try:
        with session.get(url, timeout=timeout, headers=HEADERS, stream=True) as r:
            elapsed = (time.monotonic() - start) * 1000
            alive = r.status_code in (200, 301, 302, 307, 308)
            return alive, round(elapsed, 1) if alive else None
    except Exception:
        return False, None


def batch_update_channels(results):
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    now = time.time()
    try:
        conn.executemany(
            "UPDATE channels SET is_alive=?, response_time_ms=?, last_check=?, total_checks=total_checks+1, alive_checks=alive_checks+? WHERE id=?",
            [(1 if alive else 0, ms, now, 1 if alive else 0, cid) for cid, alive, ms in results],
        )
        conn.commit()
    finally:
        conn.close()


def load_source_text(source):
    """Текст плейлиста источника: локальный файл для source_type='file', иначе URL."""
    if (source.get("source_type") or "url") == "file":
        path = source.get("file_path") or ""
        if not path or not os.path.exists(path):
            raise FileNotFoundError(f"Файл источника не найден: {path}")
        with open(path, "rb") as f:
            raw = f.read()
        for enc in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    return fetch_source(source["url"])


def import_source(source, text):
    """Разобрать плейлист и записать каналы источника в БД.

    Общая логика для проверки и для загрузки файла.
    Возвращает число каналов в источнике.
    """
    channels = parse_m3u(text)
    aliases = db.get_group_aliases()
    overrides = db.get_group_overrides()
    excluded = db.get_excluded_groups()
    auto_synonyms = db.get_setting("auto_group_synonyms", "1") == "1"
    rows = []
    for ch in channels:
        raw_translated = ch["group_title"]  # parse_m3u уже перевёл EN->RU
        final_group = groups_module.canonicalize_import(
            raw_translated, aliases, auto_synonyms=auto_synonyms)
        norm = groups_module.norm_name(ch["name"])
        if norm in overrides:
            final_group = overrides[norm]
        rows.append({
            "name": ch["name"],
            "url": ch["url"],
            "inf_line": ch["inf_line"],
            "group_title": final_group,
            "source_group": raw_translated,
        })
    url_to_id = db.upsert_channels_bulk(source["id"], rows)
    valid_urls = set(url_to_id.keys())
    excluded_ids = [
        url_to_id[r["url"]] for r in rows
        if groups_module.mech_key(r["group_title"]) in excluded
    ]
    if excluded_ids:
        # Новые каналы исключённых групп сразу выключаются,
        # чтобы таблица каналов совпадала с правилом
        db.set_channels_enabled(excluded_ids, False)
        for mech, row in excluded.items():
            try:
                prev = set(json.loads(row.get("disabled_ids") or "[]"))
            except Exception:
                prev = set()
            prev |= set(excluded_ids)
            db.set_group_exclusion(mech, row.get("group_title") or mech, sorted(prev))
    db.delete_stale_channels(source["id"], valid_urls)
    return len(channels)


def run_check(progress_callback=None):
    sources = db.get_sources(enabled_only=True)
    timeout = int(db.get_setting("check_timeout", "10"))
    parallel = int(db.get_setting("check_parallel", "50"))

    if progress_callback:
        progress_callback(0, len(sources), "Loading sources")

    for i, source in enumerate(sources):
        if progress_callback:
            progress_callback(i, len(sources), f"Source: {source['name']}")
        start = time.monotonic()
        try:
            text = load_source_text(source)
            count = import_source(source, text)
            elapsed = (time.monotonic() - start) * 1000
            db.update_source_status(source["id"], True, count, round(elapsed, 1))
            print(f"[checker] Source {source['name']}: {count} channels, {round(elapsed)}ms")
        except Exception as e:
            elapsed = (time.monotonic() - start) * 1000
            db.update_source_status(source["id"], False, 0, round(elapsed, 1))
            print(f"[checker] Error fetching source {source['name']}: {e}")
            continue

    all_channels = db.get_channels(active_sources_only=True)
    if not all_channels:
        if progress_callback:
            progress_callback(1, 1, "No channels")
        return

    session = requests.Session()
    session.mount("http://", HTTPAdapter(max_retries=Retry(total=2)))
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=2)))

    total = len(all_channels)
    print(f"[checker] Checking {total} channels ({parallel} workers, timeout={timeout}s)...")

    if progress_callback:
        progress_callback(0, total, "Checking channels")

    checked = [0]
    lock = __import__('threading').Lock()

    def check_one(ch):
        alive, ms = check_single_channel(session, ch["url"], timeout)
        with lock:
            checked[0] += 1
            if progress_callback:
                progress_callback(checked[0], total, "Checking channels")
        return (ch["id"], alive, ms)

    with ThreadPoolExecutor(max_workers=parallel) as executor:
        results = list(executor.map(check_one, all_channels))

    batch_update_channels(results)

    alive_count = sum(1 for _, alive, _ in results if alive)
    print(f"[checker] Done. Alive: {alive_count}/{total}")
