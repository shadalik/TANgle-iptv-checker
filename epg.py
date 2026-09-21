#!/usr/bin/env python3

import gzip
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import database as db


HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux) IPTV-Checker"
}

EPG_PATH = os.environ.get("EPG_PATH", "/playlist/epg.xml")
EPG_CACHE_DIR = os.environ.get("EPG_CACHE_DIR", "/data/epg_cache")
PLAYLIST_DIR = os.path.dirname(os.environ.get("PLAYLIST_PATH", "/playlist/rus_fixed.m3u")) or "/playlist"
DEFAULT_PLAYLIST_FILENAME = os.path.basename(os.environ.get("PLAYLIST_PATH", "/playlist/rus_fixed.m3u")) or "rus_fixed.m3u"


def _playlist_path():
    """Путь к текущему плейлисту (имя настраивается в БД)."""
    name = (db.get_setting("playlist_filename", DEFAULT_PLAYLIST_FILENAME) or "").strip()
    return os.path.join(PLAYLIST_DIR, name or DEFAULT_PLAYLIST_FILENAME)

# Количество одновременных загрузок.
# RAM практически не увеличивается, так как данные сразу пишутся на диск.
DOWNLOAD_WORKERS = int(os.environ.get("EPG_DOWNLOAD_WORKERS", "3"))

# Размер буфера при потоковом копировании.
COPY_BUFFER_SIZE = 1024 * 1024  # 1 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_name(name):
    """
    Нормализация названия канала.

    Сохраняет поведение старого epg.py.
    """
    if not name:
        return ""

    name = name.lower().strip()
    name = re.sub(r"\b(hd|fhd|uhd|4k|sd)\b", "", name)
    name = re.sub(r"\[.*?\]", "", name)
    name = re.sub(r"\(.*?\)", "", name)
    name = re.sub(r'[«»""]', "", name)
    name = re.sub(r"\s+", " ", name).strip()

    return name


def _safe_makedirs(path):
    if path:
        os.makedirs(path, exist_ok=True)


def _cache_path(source_id):
    return os.path.join(
        EPG_CACHE_DIR,
        f"epg_{source_id}.xml"
    )


def _cache_age_hours(source_id):
    path = _cache_path(source_id)

    if not os.path.exists(path):
        return 999

    mtime = os.path.getmtime(path)

    return (time.time() - mtime) / 3600


def _copy_stream(src, dst):
    """
    Потоковое копирование без загрузки файла в память.
    """
    while True:
        chunk = src.read(COPY_BUFFER_SIZE)

        if not chunk:
            break

        dst.write(chunk)


def cleanup_cache_temp(max_age_seconds=6 * 3600):
    """Удалить временные хвосты загрузок из кэша EPG.

    `.download` — сжатые файлы, которые раньше не удалялись после успешной
    распаковки; `*.tmp*` — недокачанные/осиротевшие файлы.
    """
    if not os.path.isdir(EPG_CACHE_DIR):
        return 0
    now = time.time()
    removed = 0
    for name in os.listdir(EPG_CACHE_DIR):
        if not (name.endswith(".download") or ".tmp" in name):
            continue
        path = os.path.join(EPG_CACHE_DIR, name)
        try:
            if name.endswith(".download") or now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
                removed += 1
        except OSError:
            pass
    if removed:
        print(f"[epg] Cleaned {removed} temp cache files")
    return removed


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _create_session(retries=2):
    session = requests.Session()

    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=4,
    )

    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


# ---------------------------------------------------------------------------
# EPG download
# ---------------------------------------------------------------------------

def download_epg_source(url, destination=None):
    """
    Скачать EPG на диск.

    В отличие от старой версии:
        r.content
        gzip.decompress()
        BytesIO()
        zf.read()

    здесь файл никогда целиком не помещается в RAM.

    Поддерживаются:
        - обычный XML
        - gzip XML
        - ZIP с XML
    """

    _safe_makedirs(EPG_CACHE_DIR)

    if destination is None:
        # Уникальный временный файл для скачивания.
        timestamp = time.time_ns()

        destination = os.path.join(
            EPG_CACHE_DIR,
            f".download_{timestamp}.tmp"
        )

    compressed_path = destination + ".download"

    session = _create_session(retries=2)

    try:
        print(f"[epg] Downloading {url}")

        with session.get(
            url,
            timeout=120,
            headers=HEADERS,
            stream=True,
        ) as response:

            response.raise_for_status()

            # Отключаем автоматическую декомпрессию requests.
            # Нам нужно самостоятельно определить gzip/zip по magic bytes.
            response.raw.decode_content = False

            with open(
                compressed_path,
                "wb",
                buffering=COPY_BUFFER_SIZE,
            ) as f:
                _copy_stream(response.raw, f)

        file_size = os.path.getsize(compressed_path)

        if file_size == 0:
            raise ValueError("Downloaded file is empty")

        # Определяем формат по magic bytes.
        with open(compressed_path, "rb") as f:
            magic = f.read(4)

        # ------------------------------------------------------------------
        # GZIP
        # ------------------------------------------------------------------

        if magic[:2] == b"\x1f\x8b":
            print(f"[epg] Decompressing gzip: {url}")

            tmp_xml = destination + ".tmp"

            try:
                with gzip.open(
                    compressed_path,
                    "rb",
                ) as src, open(
                    tmp_xml,
                    "wb",
                    buffering=COPY_BUFFER_SIZE,
                ) as dst:

                    _copy_stream(src, dst)

                os.replace(tmp_xml, destination)

            finally:
                if os.path.exists(tmp_xml):
                    try:
                        os.remove(tmp_xml)
                    except OSError:
                        pass

        # ------------------------------------------------------------------
        # ZIP
        # ------------------------------------------------------------------

        elif magic[:2] == b"PK":
            print(f"[epg] Extracting ZIP: {url}")

            tmp_xml = destination + ".tmp"

            try:
                with zipfile.ZipFile(compressed_path, "r") as zf:

                    names = zf.namelist()

                    if not names:
                        raise ValueError("ZIP archive is empty")

                    # Сначала ищем XML.
                    xml_names = [
                        name
                        for name in names
                        if name.lower().endswith(".xml")
                    ]

                    selected_name = (
                        xml_names[0]
                        if xml_names
                        else names[0]
                    )

                    print(
                        f"[epg] ZIP member: {selected_name}"
                    )

                    with zf.open(selected_name, "r") as src, \
                            open(
                                tmp_xml,
                                "wb",
                                buffering=COPY_BUFFER_SIZE,
                            ) as dst:

                        _copy_stream(src, dst)

                os.replace(tmp_xml, destination)

            finally:
                if os.path.exists(tmp_xml):
                    try:
                        os.remove(tmp_xml)
                    except OSError:
                        pass

        # ------------------------------------------------------------------
        # Plain XML
        # ------------------------------------------------------------------

        else:
            print(f"[epg] Plain XML: {url}")

            os.replace(
                compressed_path,
                destination,
            )

        # Проверяем, что итоговый файл существует и не пустой.
        final_size = os.path.getsize(destination)

        if final_size == 0:
            raise ValueError("Resulting XML file is empty")

        # Удаляем сжатый временный файл: при ошибке его чистит except,
        # но при успешной распаковке он раньше оставался на диске.
        if os.path.exists(compressed_path):
            try:
                os.remove(compressed_path)
            except OSError:
                pass

        print(
            f"[epg] Downloaded {url}: "
            f"{final_size:,} bytes"
        )

        return destination

    except Exception as e:
        print(
            f"[epg] Error downloading {url}: {e}"
        )

        for path in (
            compressed_path,
            destination,
            destination + ".tmp",
        ):
            if os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

        return None

    finally:
        session.close()


def check_epg_source(url, timeout=15):
    """
    Быстрая проверка доступности EPG.
    """
    session = _create_session(retries=1)

    start = time.monotonic()

    try:
        response = session.get(
            url,
            timeout=timeout,
            headers=HEADERS,
            stream=True,
        )

        elapsed = (
            time.monotonic() - start
        ) * 1000

        alive = response.status_code in (
            200,
            301,
            302,
            307,
            308,
        )

        response.close()

        return (
            alive,
            round(elapsed, 1) if alive else None,
            None,
        )

    except Exception as e:
        elapsed = (
            time.monotonic() - start
        ) * 1000

        return (
            False,
            None,
            str(e)[:200],
        )

    finally:
        session.close()


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

def build_channel_set(m3u_path):
    """
    Создаёт множество нормализованных названий каналов
    из текущего M3U.

    Это позволяет не хранить EPG для каналов,
    которых нет в плейлисте.
    """
    names = set()

    try:
        with open(
            m3u_path,
            "r",
            encoding="utf-8",
            errors="replace",
        ) as f:

            for line in f:
                line = line.strip()

                if not line.startswith("#EXTINF:"):
                    continue

                parts = line.rsplit(",", 1)

                if len(parts) != 2:
                    continue

                norm = normalize_name(parts[1])

                if norm:
                    names.add(norm)

    except FileNotFoundError:
        pass

    except Exception as e:
        print(
            f"[epg] Error reading playlist "
            f"{m3u_path}: {e}"
        )

    return names


# ---------------------------------------------------------------------------
# Streaming XMLTV parser
# ---------------------------------------------------------------------------

def _get_display_name(channel_element):
    """
    Получить display-name из <channel>.
    """
    for child in channel_element:
        tag = child.tag

        if isinstance(tag, str) and tag.endswith("display-name"):
            text = child.text

            if text:
                return text.strip()

    return ""


def _get_icon_src(channel_element):
    """Получить src первого <icon> из <channel> (для логотипов каналов)."""
    for child in channel_element:
        tag = child.tag

        if isinstance(tag, str) and tag.endswith("icon"):
            src = (child.get("src") or "").strip()

            if src:
                return src

    return ""


def _get_child_text(element, wanted_name):
    """
    Найти текст дочернего XML элемента.
    Поддерживает XML namespaces.
    """
    for child in element:
        tag = child.tag

        if not isinstance(tag, str):
            continue

        if tag == wanted_name or tag.endswith(
            "}" + wanted_name
        ):
            return child.text or ""

    return ""


def _strip_namespace(tag):
    """
    Удаляет namespace:

        {http://...}programme

    превращается в:

        programme
    """
    if not isinstance(tag, str):
        return ""

    if "}" in tag:
        return tag.rsplit("}", 1)[1]

    return tag


def _open_streaming_epg_database(path):
    """
    Временная SQLite DB на диске.

    Она используется только во время merge.

    Благодаря этому миллионы programme не занимают
    гигабайты Python RAM.
    """

    conn = sqlite3.connect(
        path,
        timeout=60,
    )

    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=FILE")
    conn.execute("PRAGMA cache_size=-8192")

    conn.execute(
        """
        CREATE TABLE programmes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_name TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            start TEXT NOT NULL,
            stop TEXT,
            title TEXT NOT NULL,
            description TEXT,
            UNIQUE(channel_name, start)
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE channels (
            channel_name TEXT PRIMARY KEY,
            channel_id TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX idx_programmes_channel_start
        ON programmes(channel_name, start)
        """
    )

    return conn


def _parse_xmltv_to_database(
    xml_path,
    playlist_channel_names,
    conn,
):
    """
    Потоковый XMLTV parser.

    Важнейшая часть оптимизации:

        ET.iterparse()

    вместо:

        ET.fromstring()

    После обработки каждого programme/channel элемент
    удаляется из XML дерева.
    """

    matched_channels = set()
    channel_ids = {}
    channel_icons = {}

    matched_programmes = 0
    inserted_programmes = 0

    # ----------------------------------------------------------------------
    # Pass #1
    #
    # Сначала читаем channel.
    # ----------------------------------------------------------------------

    try:
        context = ET.iterparse(
            xml_path,
            events=("end",),
        )

        for event, elem in context:
            tag = _strip_namespace(elem.tag)

            if tag == "channel":
                channel_id = (
                    elem.get("id", "")
                    or ""
                ).strip()

                display_name = _get_display_name(
                    elem
                )

                norm_name = normalize_name(
                    display_name
                )

                if (
                    channel_id
                    and norm_name
                    and norm_name in playlist_channel_names
                ):
                    matched_channels.add(norm_name)

                    # Важный момент:
                    # сохраняем ID первого найденного источника,
                    # как делал старый epg.py.
                    if norm_name not in channel_ids:
                        channel_ids[norm_name] = channel_id

                    # Логотип канала из EPG (первый у канала).
                    if norm_name not in channel_icons:
                        icon = _get_icon_src(elem)
                        if icon:
                            channel_icons[norm_name] = icon

                # Дочерние элементы (display-name, icon) нужны до этой очистки.
                elem.clear()

            elif tag == "programme":
                # В первом проходе программы не нужны, но без очистки
                # дерево всего XML накапливается в RAM. Дочерние
                # элементы освобождаются вместе с родителем.
                elem.clear()

    except ET.ParseError as e:
        print(
            f"[epg] XML parse error in "
            f"{xml_path}: {e}"
        )
        return set(), 0, {}, {}

    except Exception as e:
        print(
            f"[epg] Error parsing channels "
            f"{xml_path}: {e}"
        )
        return set(), 0, {}, {}

    # ----------------------------------------------------------------------
    # Сохраняем channel ID.
    # ----------------------------------------------------------------------

    if channel_ids:
        conn.executemany(
            """
            INSERT OR IGNORE INTO channels
                (channel_name, channel_id)
            VALUES (?, ?)
            """,
            channel_ids.items(),
        )

        conn.commit()

    # ----------------------------------------------------------------------
    # Pass #2
    #
    # XMLTV programme.
    #
    # Это второй проход по файлу, но RAM остаётся минимальной.
    # ----------------------------------------------------------------------

    try:
        context = ET.iterparse(
            xml_path,
            events=("end",),
        )

        pending = []

        # Доминирующая категория передач для каждого сматченного канала.
        # Только для group-fallback (память: каналы x несколько категорий).
        category_votes = defaultdict(Counter)

        # Обратное отображение channel_id -> нормализованное имя.
        # Строим один раз: иначе на каждой программе шёл линейный
        # перебор channel_ids (квадратичная сложность).
        id_to_name = {}
        for name, cid in channel_ids.items():
            id_to_name.setdefault(cid, name)

        for event, elem in context:
            tag = _strip_namespace(elem.tag)

            if tag != "programme":
                # Каналы в этом проходе не нужны; programme очищаются
                # ниже после извлечения title/desc/category.
                if tag == "channel":
                    elem.clear()
                continue

            channel_id = (
                elem.get("channel", "")
                or ""
            ).strip()

            norm_name = id_to_name.get(channel_id) if channel_id else None

            if (
                not norm_name
                or norm_name not in playlist_channel_names
            ):
                elem.clear()
                continue

            start = (
                elem.get("start", "")
                or ""
            ).strip()

            stop = (
                elem.get("stop", "")
                or ""
            ).strip()

            title = _get_child_text(
                elem,
                "title",
            ).strip()

            description = _get_child_text(
                elem,
                "desc",
            ).strip()

            if not start or not title:
                elem.clear()
                continue

            category = _get_child_text(
                elem,
                "category",
            ).strip()

            if category:
                category_votes[norm_name][category] += 1

            pending.append(
                (
                    norm_name,
                    channel_ids[norm_name],
                    start,
                    stop,
                    title,
                    description,
                )
            )

            # Не держим большой executemany batch.
            if len(pending) >= 5000:
                before_changes = conn.total_changes
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO programmes
                        (
                            channel_name,
                            channel_id,
                            start,
                            stop,
                            title,
                            description
                        )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    pending,
                )

                conn.commit()

                inserted_programmes += conn.total_changes - before_changes
                matched_programmes += len(
                    pending
                )

                pending.clear()

            # Ключевой момент:
            # удаляем programme из XML дерева.
            elem.clear()

        if pending:
            before_changes = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO programmes
                    (
                        channel_name,
                        channel_id,
                        start,
                        stop,
                        title,
                        description
                    )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                pending,
            )

            conn.commit()

            inserted_programmes += conn.total_changes - before_changes
            matched_programmes += len(
                pending
            )

            pending.clear()

        dropped = matched_programmes - inserted_programmes
        if dropped > 0:
            print(
                f"[epg] {os.path.basename(xml_path)}: "
                f"dropped {dropped:,} duplicate slots "
                f"(same channel and start time)"
            )

    except ET.ParseError as e:
        print(
            f"[epg] XML programme parse error "
            f"in {xml_path}: {e}"
        )

    except Exception as e:
        print(
            f"[epg] Error parsing programmes "
            f"{xml_path}: {e}"
        )

    dominant_categories = {
        name: votes.most_common(1)[0][0]
        for name, votes in category_votes.items()
        if votes
    }

    return (
        matched_channels,
        matched_programmes,
        dominant_categories,
        channel_icons,
    )


def parse_xmltv(xml_bytes):
    """
    Совместимость со старым API.

    Внутренне этот метод больше не используется для merge,
    потому что он требует держать весь XML в RAM.

    Оставлен намеренно, чтобы сторонний код TANgle,
    если он где-либо вызывает parse_xmltv(),
    не получил AttributeError.

    ВНИМАНИЕ:
    этот compatibility API всё ещё работает в RAM.
    Основной update_epg() его НЕ использует.
    """

    if not xml_bytes:
        return {}, {}

    try:
        text = xml_bytes.decode(
            "utf-8"
        )

    except UnicodeDecodeError:
        try:
            text = xml_bytes.decode(
                "windows-1251"
            )

        except UnicodeDecodeError:
            return {}, {}

    root = ET.fromstring(text)

    channel_map = {}

    for ch_el in root.findall("channel"):
        ch_id = ch_el.get(
            "id",
            "",
        )

        display_names = ch_el.findall(
            "display-name"
        )

        if (
            display_names
            and display_names[0].text
        ):
            norm_name = normalize_name(
                display_names[0].text
            )

            if (
                norm_name
                and norm_name not in channel_map
            ):
                channel_map[norm_name] = ch_id

    id_to_norm = {
        value: key
        for key, value in channel_map.items()
    }

    programmes = {}

    for prog in root.findall("programme"):
        ch_id = prog.get(
            "channel",
            "",
        )

        norm_name = id_to_norm.get(
            ch_id
        )

        if not norm_name:
            continue

        start = prog.get(
            "start",
            "",
        )

        stop = prog.get(
            "stop",
            "",
        )

        title_el = prog.find("title")
        desc_el = prog.find("desc")

        title = (
            title_el.text
            if title_el is not None
            and title_el.text
            else ""
        )

        desc = (
            desc_el.text
            if desc_el is not None
            and desc_el.text
            else ""
        )

        if norm_name not in programmes:
            programmes[norm_name] = []

        programmes[norm_name].append(
            {
                "start": start,
                "stop": stop,
                "title": title,
                "desc": desc,
                "channel_id": ch_id,
            }
        )

    return (
        channel_map,
        programmes,
    )


# ---------------------------------------------------------------------------
# Download all sources
# ---------------------------------------------------------------------------

def download_all_sources(epg_sources_data):
    _safe_makedirs(
        EPG_CACHE_DIR
    )

    def fetch_one(source):
        cache = _cache_path(
            source["id"]
        )

        tmp = (
            cache
            + ".tmp"
        )

        # download_epg_source() пишет XML
        # непосредственно в tmp.
        result = download_epg_source(
            source["url"],
            destination=tmp,
        )

        if result is None:
            db.update_epg_source_status(
                source["id"],
                False,
                None,
                "Download failed",
            )

            return source, False

        try:
            os.replace(
                tmp,
                cache,
            )

            size = os.path.getsize(
                cache
            )

            db.update_epg_source_status(
                source["id"],
                True,
                None,
                None,
            )

            print(
                f"[epg] Cached "
                f"{source['name']} "
                f"({size:,} bytes)"
            )

            return source, True

        except Exception as e:
            print(
                f"[epg] Error caching "
                f"{source['name']}: {e}"
            )

            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass

            db.update_epg_source_status(
                source["id"],
                False,
                None,
                str(e)[:200],
            )

            return source, False

    with ThreadPoolExecutor(
        max_workers=DOWNLOAD_WORKERS
    ) as executor:

        results = list(
            executor.map(
                fetch_one,
                epg_sources_data,
            )
        )

    for source, ok in results:
        if not ok:
            print(
                f"[epg] Failed to cache "
                f"{source['name']}"
            )


# ---------------------------------------------------------------------------
# Merge
# ---------------------------------------------------------------------------

def _create_temp_merge_db():
    """
    Временная SQLite БД.

    Используется на диске, а не в RAM.
    """

    _safe_makedirs(
        EPG_CACHE_DIR
    )

    path = os.path.join(
        EPG_CACHE_DIR,
        ".epg_merge.sqlite",
    )

    # Удаляем старую временную БД.
    for suffix in (
        "",
        "-wal",
        "-shm",
        "-journal",
    ):
        try:
            os.remove(
                path + suffix
            )
        except FileNotFoundError:
            pass
        except OSError:
            pass

    return path


def _xml_element_to_bytes(
    tag,
    attributes=None,
    text=None,
):
    """
    Создаёт небольшой XML element.

    Здесь размер элемента ничтожен по сравнению
    со всем EPG.
    """

    element = ET.Element(
        tag,
        attributes or {},
    )

    if text:
        element.text = text

    return ET.tostring(
        element,
        encoding="utf-8",
        short_empty_elements=True,
    )


def _write_epg_stream(
    conn,
    destination,
    channel_count,
):
    """
    Потоковая генерация итогового XMLTV.

    В отличие от старой версии здесь НЕ создаётся:

        root = ET.Element("tv")

    содержащий весь EPG.

    В памяти находится только один небольшой XML element.
    """

    tmp_path = destination + ".tmp"

    _safe_makedirs(
        os.path.dirname(destination)
    )

    try:
        with open(
            tmp_path,
            "wb",
            buffering=COPY_BUFFER_SIZE,
        ) as f:

            f.write(
                b'<?xml version="1.0" encoding="UTF-8"?>\n'
            )

            f.write(
                b'<tv generator-info-name="iptv-autocheck">\n'
            )

            # --------------------------------------------------------------
            # Channels
            # --------------------------------------------------------------

            channel_cursor = conn.execute(
                """
                SELECT channel_name, channel_id
                FROM channels
                WHERE channel_name IN (
                    SELECT DISTINCT channel_name
                    FROM programmes
                )
                ORDER BY channel_name
                """
            )

            written_ids = set()
            written_channels = 0
            id_by_name = {}

            for row in channel_cursor:
                channel_name = row[0]
                channel_id = (row[1] or "").strip()

                if not channel_id:
                    channel_id = channel_name

                # Разные каналы из разных источников могут иметь одинаковый id
                # (например, числовые id совпадают). Делаем id уникальным,
                # чтобы каждая программа ссылалась на объявленный канал.
                final_id = channel_id
                suffix = 2
                while final_id in written_ids:
                    final_id = f"{channel_id}#{suffix}"
                    suffix += 1

                written_ids.add(final_id)
                id_by_name[channel_name] = final_id
                written_channels += 1

                channel_element = ET.Element(
                    "channel",
                    {
                        "id": final_id
                    },
                )

                display = ET.SubElement(
                    channel_element,
                    "display-name",
                )

                display.text = channel_name

                f.write(
                    ET.tostring(
                        channel_element,
                        encoding="utf-8",
                        short_empty_elements=True,
                    )
                )

                # Освобождаем маленький element.
                channel_element.clear()

            # --------------------------------------------------------------
            # Programmes
            # --------------------------------------------------------------

            cursor = conn.execute(
                """
                SELECT
                    p.channel_name,
                    p.start,
                    p.stop,
                    p.title,
                    p.description
                FROM programmes p
                ORDER BY
                    p.channel_name,
                    p.start
                """
            )

            for row in cursor:
                (
                    channel_name,
                    start,
                    stop,
                    title,
                    description,
                ) = row

                channel_id = id_by_name.get(channel_name)
                if not channel_id:
                    continue

                attributes = {
                    "start": start,
                    "stop": stop or "",
                    "channel": channel_id,
                }

                programme = ET.Element(
                    "programme",
                    attributes,
                )

                title_element = ET.SubElement(
                    programme,
                    "title",
                )

                title_element.text = title

                if description:
                    desc_element = ET.SubElement(
                        programme,
                        "desc",
                    )

                    desc_element.text = description

                f.write(
                    ET.tostring(
                        programme,
                        encoding="utf-8",
                        short_empty_elements=True,
                    )
                )

                programme.clear()

            f.write(
                b"</tv>\n"
            )

        os.replace(
            tmp_path,
            destination,
        )

        return written_channels

    except Exception:
        try:
            if os.path.exists(
                tmp_path
            ):
                os.remove(
                    tmp_path
                )
        except OSError:
            pass

        raise


def merge_from_cache(
    epg_sources_data,
    playlist_channel_names,
):
    """
    Объединяет EPG из всех кэшей.

    Главное отличие от старого кода:

        старый:
            merged_programmes = {}

        новый:
            временная SQLite DB на диске.

    Благодаря этому размер EPG не приводит к гигантскому
    потреблению Python RAM.
    """

    if not playlist_channel_names:
        return 0

    merge_db_path = (
        _create_temp_merge_db()
    )

    conn = None

    try:
        conn = _open_streaming_epg_database(
            merge_db_path
        )

        total_channels = set()
        category_sources = defaultdict(Counter)
        icon_sources = {}

        for source in epg_sources_data:
            cache = _cache_path(
                source["id"]
            )

            if not os.path.exists(cache):
                print(
                    f"[epg] Cache missing for "
                    f"{source['name']}"
                )
                continue

            try:
                print(
                    f"[epg] Parsing "
                    f"{source['name']}..."
                )
                _t0 = time.time()

                before = conn.execute(
                    "SELECT COUNT(*) FROM programmes"
                ).fetchone()[0]

                matched_channels, _, file_categories, file_icons = (
                    _parse_xmltv_to_database(
                        cache,
                        playlist_channel_names,
                        conn,
                    )
                )

                for norm_name, icon in file_icons.items():
                    if icon:
                        icon_sources.setdefault(norm_name, icon)

                for norm_name, category in file_categories.items():
                    if category:
                        category_sources[norm_name][category] += 1

                after = conn.execute(
                    "SELECT COUNT(*) FROM programmes"
                ).fetchone()[0]

                new_programmes = (
                    after - before
                )

                total_channels.update(
                    matched_channels
                )

                db.update_epg_download_stats(
                    source["id"],
                    len(matched_channels),
                )

                print(
                    f"[epg] {source['name']}: "
                    f"{len(matched_channels)} "
                    f"channels matched, "
                    f"{new_programmes:,} new programmes "
                    f"in {time.time() - _t0:.1f}s"
                )

            except Exception as e:
                print(
                    f"[epg] Error parsing cached "
                    f"{source['name']}: {e}"
                )

        # --------------------------------------------------------------
        # Оптимизируем SQLite перед генерацией.
        # --------------------------------------------------------------

        try:
            conn.execute(
                "ANALYZE"
            )
        except Exception:
            pass

        programme_count = conn.execute(
            "SELECT COUNT(*) FROM programmes"
        ).fetchone()[0]

        print(
            f"[epg] Total unique programmes: "
            f"{programme_count:,}"
        )

        print(
            f"[epg] Generating EPG..."
        )

        written_channels = _write_epg_stream(
            conn,
            EPG_PATH,
            len(total_channels),
        )
        print(
            f"[epg] Written channels with programmes: {written_channels}"
        )

        # Сжатая версия для клиентов с Accept-Encoding: gzip
        # (одно сжатие на сборку, дальше отдаётся готовый файл).
        gzip_epg_file()

        # Доминирующие категории передач -> fallback для групп каналов.
        # Сохраняем сырые категории; маппинг в группы - на этапе resolve.
        try:
            dominant = {
                norm_name: votes.most_common(1)[0][0]
                for norm_name, votes in category_sources.items()
                if votes
            }
            db.save_epg_categories(dominant)
            print(
                f"[epg] Saved {len(dominant)} channel categories "
                f"for group fallback"
            )
        except Exception as e:
            print(f"[epg] Error saving categories: {e}")

        # Логотипы каналов из EPG -> дозаполнение tvg-logo в плейлисте.
        # Не затираем прежние иконки, если в этот раз их не собрали.
        if icon_sources:
            try:
                db.save_epg_icons(icon_sources)
                print(
                    f"[epg] Saved {len(icon_sources)} channel icons "
                    f"for playlist logos"
                )
            except Exception as e:
                print(f"[epg] Error saving icons: {e}")

        return written_channels

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

        # Удаляем временную SQLite DB.
        for suffix in (
            "",
            "-wal",
            "-shm",
            "-journal",
        ):
            try:
                os.remove(
                    merge_db_path + suffix
                )
            except FileNotFoundError:
                pass
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_GZIP_LOCK = threading.Lock()


def _gzip_epg_locked(src_path, dst_path, compresslevel=6):
    """Потоково сжать готовый epg.xml в epg.xml.gz (без загрузки в RAM)."""
    if not os.path.exists(src_path):
        return None
    tmp_path = dst_path + ".tmp"
    try:
        # Имя и mtime задаём явно: иначе gzip запишет в заголовок basename
        # временного файла ("epg.xml.gz.tmp") и архив будет невоспроизводимым.
        with open(src_path, "rb") as src, open(tmp_path, "wb") as raw, gzip.GzipFile(
            filename=os.path.basename(src_path),
            mode="wb",
            fileobj=raw,
            compresslevel=compresslevel,
            mtime=int(os.path.getmtime(src_path)),
        ) as dst:
            _copy_stream(src, dst)
        os.replace(tmp_path, dst_path)
        print(f"[epg] Gzip saved to {dst_path} ({os.path.getsize(dst_path):,} bytes)")
        return dst_path
    except Exception as e:
        print(f"[epg] Error gzipping {src_path}: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None


def gzip_epg_file(src_path=None, dst_path=None, compresslevel=6):
    """Сжать EPG. Блокировка защищает от одновременного сжатия
    (сборка EPG + старт приложения + запрос клиента)."""
    src = src_path or EPG_PATH
    dst = dst_path or (src + ".gz")
    with _GZIP_LOCK:
        return _gzip_epg_locked(src, dst, compresslevel)


def ensure_epg_gzip():
    """Достроить epg.xml.gz, если он отсутствует или старше epg.xml."""
    gz = EPG_PATH + ".gz"
    if not os.path.exists(EPG_PATH):
        return None

    def _fresh():
        try:
            return os.path.exists(gz) and os.path.getmtime(gz) >= os.path.getmtime(EPG_PATH)
        except OSError:
            return False

    if _fresh():
        return gz
    with _GZIP_LOCK:
        if _fresh():
            return gz
        return _gzip_epg_locked(EPG_PATH, gz)


def write_epg_file(xml_content):
    """
    Старый API.

    Используется только если другой код TANgle
    напрямую вызывает write_epg_file().

    Основной update_epg() больше не формирует огромную
    XML строку и эту функцию не использует.
    """

    _safe_makedirs(
        os.path.dirname(EPG_PATH)
    )

    tmp_path = (
        EPG_PATH
        + ".tmp"
    )

    with open(
        tmp_path,
        "w",
        encoding="utf-8",
    ) as f:
        f.write(
            xml_content
        )

    os.replace(
        tmp_path,
        EPG_PATH,
    )

    # Сразу готовим сжатую версию (отдаётся клиентам с Accept-Encoding: gzip).
    gzip_epg_file()


def validate_source_file(path, limit_channels=25):
    """Потоковая проверка XMLTV-файла: считает каналы, не загружая XML в RAM.

    Возвращает (channels_found, error). Используется при добавлении
    EPG-источника вместо parse_xmltv(), который держал весь файл в памяти.
    """
    count = 0
    try:
        for event, elem in ET.iterparse(path, events=("end",)):
            tag = _strip_namespace(elem.tag)
            if tag == "channel":
                count += 1
            elem.clear()
            if count >= limit_channels:
                break
    except ET.ParseError as e:
        return 0, f"XML parse error: {e}"
    except Exception as e:
        return 0, str(e)[:200]
    return count, None


def download_epg_sources():
    """
    Скачать все включённые EPG источники.
    """

    epg_sources = db.get_epg_sources(
        enabled_only=True
    )

    if not epg_sources:
        print(
            "[epg] No enabled EPG sources to download"
        )
        return

    print(
        f"[epg] Downloading "
        f"{len(epg_sources)} EPG sources..."
    )

    download_all_sources(
        epg_sources
    )

    print(
        "[epg] Download complete"
    )


def update_epg(force=False):
    """
    Полное обновление EPG.

    Алгоритм:

        1. Проверить кэш.
        2. При необходимости скачать EPG на диск.
        3. Прочитать playlist.
        4. Потоково разобрать каждый XML.
        5. Сохранить только нужные каналы.
        6. Дедуплицировать через SQLite.
        7. Потоково создать итоговый epg.xml.

    Пересборка выполняется только если что-то изменилось
    (кэши, список каналов плейлиста) или истёк страховочный интервал.
    force=True - принудительная пересборка.

    В RAM больше не находится весь EPG.
    """

    epg_sources = db.get_epg_sources(
        enabled_only=True
    )

    if not epg_sources:
        print(
            "[epg] No enabled EPG sources"
        )

        write_epg_file(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<tv/>'
        )

        db.set_setting("epg_channel_count", "0")
        return 0

    channel_names = build_channel_set(
        _playlist_path()
    )

    if not channel_names:
        print(
            "[epg] No channels in playlist"
        )

        write_epg_file(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<tv/>'
        )

        db.set_setting("epg_channel_count", "0")
        return 0

    # ----------------------------------------------------------------------
    # Нужна ли пересборка вообще?
    # ----------------------------------------------------------------------

    fingerprint = hashlib.sha1(
        "\n".join(sorted(channel_names)).encode("utf-8")
    ).hexdigest()

    last_build = float(
        db.get_setting("epg_last_build", "0") or 0
    )

    stored_hash = db.get_setting("epg_playlist_hash", "")

    stale_sources = [
        s for s in epg_sources
        if _cache_age_hours(s["id"]) > 23
    ]

    cache_newer = False
    for s in epg_sources:
        path = _cache_path(s["id"])
        try:
            if os.path.exists(path) and os.path.getmtime(path) > last_build:
                cache_newer = True
                break
        except OSError:
            pass

    rebuild_interval = int(
        db.get_setting("epg_update_interval", "86400")
    )
    interval_elapsed = (
        (time.time() - last_build) >= rebuild_interval
    )

    if (
        not force
        and not stale_sources
        and not cache_newer
        and fingerprint == stored_hash
        and not interval_elapsed
    ):
        cached_count = int(
            db.get_setting("epg_channel_count", "0") or 0
        )

        print(
            f"[epg] Up to date "
            f"(built {int(time.time() - last_build)}s ago), "
            f"rebuild skipped"
        )

        return cached_count

    _build_t0 = time.time()

    # ----------------------------------------------------------------------
    # Download stale sources
    # ----------------------------------------------------------------------

    for source in epg_sources:
        age = _cache_age_hours(
            source["id"]
        )

        if age > 23:
            print(
                f"[epg] Cache stale for "
                f"{source['name']}, downloading..."
            )

            cache = _cache_path(
                source["id"]
            )

            tmp = (
                cache
                + ".tmp"
            )

            result = download_epg_source(
                source["url"],
                destination=tmp,
            )

            if result is not None:
                try:
                    os.replace(
                        tmp,
                        cache,
                    )

                    size = os.path.getsize(
                        cache
                    )

                    db.update_epg_source_status(
                        source["id"],
                        True,
                        None,
                        None,
                    )

                    print(
                        f"[epg] Cached "
                        f"{source['name']} "
                        f"({size:,} bytes)"
                    )

                except Exception as e:
                    print(
                        f"[epg] Error saving "
                        f"{source['name']}: {e}"
                    )

                    try:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass

                    db.update_epg_source_status(
                        source["id"],
                        False,
                        None,
                        str(e)[:200],
                    )

            else:
                db.update_epg_source_status(
                    source["id"],
                    False,
                    None,
                    "Download failed",
                )

    # ----------------------------------------------------------------------
    # Merge
    # ----------------------------------------------------------------------

    print(
        f"[epg] Merging EPG for "
        f"{len(channel_names)} playlist channels..."
    )

    merged_count = merge_from_cache(
        epg_sources,
        channel_names,
    )

    try:
        final_size = os.path.getsize(
            EPG_PATH
        )
    except OSError:
        final_size = 0

    print(
        f"[epg] EPG saved to "
        f"{EPG_PATH} "
        f"({merged_count} channels, "
        f"{final_size:,} bytes) "
        f"in {time.time() - _build_t0:.1f}s"
    )

    try:
        hwm_kb = next(
            line.split()[1]
            for line in open("/proc/self/status")
            if line.startswith("VmHWM")
        )
        print(f"[epg] Peak RSS: {int(hwm_kb) / 1024:.0f} MB")
    except Exception:
        pass

    db.set_setting("epg_last_build", int(time.time()))
    db.set_setting("epg_playlist_hash", fingerprint)
    db.set_setting("epg_channel_count", merged_count or 0)

    return merged_count
