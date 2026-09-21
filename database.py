#!/usr/bin/env python3
import sqlite3
import time
import os
import re
from datetime import datetime
from contextlib import contextmanager

DB_PATH = os.environ.get("DB_PATH", "/data/iptv.db")


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                is_alive INTEGER DEFAULT 0,
                channel_count INTEGER DEFAULT 0,
                last_check REAL,
                response_time_ms REAL,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                inf_line TEXT,
                group_title TEXT DEFAULT 'Другое',
                enabled INTEGER DEFAULT 1,
                last_check REAL,
                is_alive INTEGER DEFAULT 0,
                response_time_ms REAL,
                total_checks INTEGER DEFAULT 0,
                alive_checks INTEGER DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s','now')),
                FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS group_aliases (
                raw TEXT PRIMARY KEY,
                canonical TEXT NOT NULL,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS group_overrides (
                norm_name TEXT PRIMARY KEY,
                group_title TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS epg_categories (
                norm_name TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS epg_icons (
                norm_name TEXT PRIMARY KEY,
                icon_url TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS playlist_groups (
                norm_name TEXT PRIMARY KEY,
                group_title TEXT NOT NULL,
                updated_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS group_exclusions (
                mech_name TEXT PRIMARY KEY,
                group_title TEXT NOT NULL,
                disabled_ids TEXT DEFAULT '[]',
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS disabled_by_dedup (
                channel_id INTEGER PRIMARY KEY,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
        """)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS epg_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                last_download REAL,
                channel_count INTEGER DEFAULT 0,
                is_alive INTEGER DEFAULT 0,
                response_time_ms REAL,
                last_check REAL,
                error_message TEXT,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE TABLE IF NOT EXISTS access_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT,
                timestamp REAL DEFAULT (strftime('%s','now'))
            );
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(epg_sources)").fetchall()]
        if "is_alive" not in cols:
            conn.execute("ALTER TABLE epg_sources ADD COLUMN is_alive INTEGER DEFAULT 0")
        if "response_time_ms" not in cols:
            conn.execute("ALTER TABLE epg_sources ADD COLUMN response_time_ms REAL")
        if "last_check" not in cols:
            conn.execute("ALTER TABLE epg_sources ADD COLUMN last_check REAL")
        if "error_message" not in cols:
            conn.execute("ALTER TABLE epg_sources ADD COLUMN error_message TEXT")
        ch_cols = [r[1] for r in conn.execute("PRAGMA table_info(channels)").fetchall()]
        if "total_checks" not in ch_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN total_checks INTEGER DEFAULT 0")
        if "alive_checks" not in ch_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN alive_checks INTEGER DEFAULT 0")
        if "enabled" not in ch_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN enabled INTEGER DEFAULT 1")
        if "source_group" not in ch_cols:
            conn.execute("ALTER TABLE channels ADD COLUMN source_group TEXT")
            # Backfill: для каналов без ручных правок inf_line содержит группу источника
            try:
                rows = conn.execute("SELECT id, inf_line FROM channels").fetchall()
                for r in rows:
                    inf = r["inf_line"] or ""
                    m = re.search(r'group-title="([^"]*)"', inf)
                    if m:
                        conn.execute("UPDATE channels SET source_group=? WHERE id=?", (m.group(1), r["id"]))
                    else:
                        conn.execute("UPDATE channels SET source_group=group_title WHERE id=?", (r["id"],))
            except Exception as e:
                print(f"[db] source_group backfill failed: {e}")
        src_cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)").fetchall()]
        if "source_type" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN source_type TEXT DEFAULT 'url'")
        if "file_path" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN file_path TEXT")
        if "file_name" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN file_name TEXT")
        if "is_alive" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN is_alive INTEGER DEFAULT 0")
        if "channel_count" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN channel_count INTEGER DEFAULT 0")
        if "last_check" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN last_check REAL")
        if "response_time_ms" not in src_cols:
            conn.execute("ALTER TABLE sources ADD COLUMN response_time_ms REAL")
        alias_cols = [r[1] for r in conn.execute("PRAGMA table_info(group_aliases)").fetchall()]
        if "raw_display" not in alias_cols:
            conn.execute("ALTER TABLE group_aliases ADD COLUMN raw_display TEXT")
            # Backfill исходного написания из source_group каналов
            try:
                src_groups = conn.execute("SELECT DISTINCT source_group FROM channels").fetchall()
                seen = {}
                for r in src_groups:
                    sg = r["source_group"] or ""
                    key = sg.strip().lower().replace("ё", "е")
                    if key and key not in seen:
                        seen[key] = sg.strip()
                for key, disp in seen.items():
                    conn.execute("UPDATE group_aliases SET raw_display=? WHERE raw=? AND (raw_display IS NULL OR raw_display='')", (disp, key))
            except Exception as e:
                print(f"[db] raw_display backfill failed: {e}")
        # Чистка бессмысленных алиасов вида "релакс -> Релакс"
        try:
            for r in conn.execute("SELECT raw, canonical FROM group_aliases").fetchall():
                if (r["raw"] or "").strip().lower().replace("ё", "е") == (r["canonical"] or "").strip().lower().replace("ё", "е"):
                    conn.execute("DELETE FROM group_aliases WHERE raw=?", (r["raw"],))
        except Exception as e:
            print(f"[db] no-op alias cleanup failed: {e}")
        row = conn.execute("SELECT value FROM settings WHERE key='check_interval'").fetchone()
        if not row:
            conn.execute("INSERT INTO settings (key, value) VALUES ('check_interval', '3600')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('check_parallel', '50')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('check_timeout', '10')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('auth_login', 'admin')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('auth_password', 'admin')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('playlist_all', '1')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('playlist_fast', '1')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('playlist_medium', '1')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('playlist_slow', '1')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('epg_update_interval', '86400')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('availability_period_days', '7')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('min_availability', '0')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('dedup_strategy', 'availability')")
            conn.execute("INSERT INTO settings (key, value) VALUES ('auto_group_synonyms', '1')")
        else:
            # доустановка новых настроек на существующих БД
            if not conn.execute("SELECT value FROM settings WHERE key='dedup_strategy'").fetchone():
                conn.execute("INSERT INTO settings (key, value) VALUES ('dedup_strategy', 'availability')")
            if not conn.execute("SELECT value FROM settings WHERE key='auto_group_synonyms'").fetchone():
                conn.execute("INSERT INTO settings (key, value) VALUES ('auto_group_synonyms', '1')")
        epg_count = conn.execute("SELECT COUNT(*) as cnt FROM epg_sources").fetchone()["cnt"]
        if epg_count == 0:
            conn.execute("INSERT INTO epg_sources (name, url, enabled) VALUES ('IPTVX One', 'http://iptvx.one/epg/epg_lite.xml.gz', 1)")
            conn.execute("INSERT INTO epg_sources (name, url, enabled) VALUES ('ProgramTV', 'http://programtv.ru/xmltv.xml.gz', 1)")
            conn.execute("INSERT INTO epg_sources (name, url, enabled) VALUES ('EPG It999 (Universal)', 'http://epg.it999.ru/edem.xml.gz', 1)")
            conn.execute("INSERT INTO epg_sources (name, url, enabled) VALUES ('EPG It999 RU', 'http://epg.it999.ru/ru2.xml.gz', 1)")
            conn.execute("INSERT INTO epg_sources (name, url, enabled) VALUES ('OTT EPG', 'https://ottepg.ru/ottepg.xml.gz', 1)")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key, value):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def add_source(name, url, enabled=True, source_type="url", file_path=None, file_name=None):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sources (name, url, enabled, source_type, file_path, file_name) VALUES (?, ?, ?, ?, ?, ?)",
            (name, url, 1 if enabled else 0, source_type, file_path, file_name),
        )
        return cur.lastrowid


def set_source_file(source_id, file_path, file_name):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sources SET source_type='file', file_path=?, file_name=?, url=? WHERE id=?",
            (file_path, file_name, f"file://{file_name}", source_id),
        )


def update_source(source_id, name=None, url=None, enabled=None):
    with get_conn() as conn:
        fields, values = [], []
        if name is not None:
            fields.append("name=?")
            values.append(name)
        if url is not None:
            fields.append("url=?")
            values.append(url)
        if enabled is not None:
            fields.append("enabled=?")
            values.append(1 if enabled else 0)
        if fields:
            values.append(source_id)
            conn.execute(f"UPDATE sources SET {','.join(fields)} WHERE id=?", values)


def update_source_status(source_id, is_alive, channel_count, response_time_ms=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE sources SET is_alive=?, channel_count=?, last_check=?, response_time_ms=? WHERE id=?",
            (1 if is_alive else 0, channel_count, time.time(), response_time_ms, source_id),
        )


def delete_source(source_id):
    with get_conn() as conn:
        row = conn.execute("SELECT file_path, source_type FROM sources WHERE id=?", (source_id,)).fetchone()
        conn.execute("DELETE FROM channels WHERE source_id=?", (source_id,))
        conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
        if row:
            return dict(row)
        return None


def get_sources(enabled_only=False):
    with get_conn() as conn:
        q = "SELECT * FROM sources"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY name"
        return [dict(r) for r in conn.execute(q).fetchall()]


def upsert_channel(source_id, name, url, inf_line, group_title, source_group=None):
    if source_group is None:
        source_group = group_title
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM channels WHERE source_id=? AND url=?", (source_id, url)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE channels SET name=?, inf_line=?, group_title=?, source_group=? WHERE id=?",
                (name, inf_line, group_title, source_group, existing["id"]),
            )
            return existing["id"]
        else:
            cur = conn.execute(
                "INSERT INTO channels (source_id, name, url, inf_line, group_title, source_group) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, name, url, inf_line, group_title, source_group),
            )
            return cur.lastrowid


def upsert_channels_bulk(source_id, rows):
    """Пакетный upsert каналов источника в одной транзакции.

    rows: список dict с ключами name, url, inf_line, group_title, source_group.
    Возвращает {url: channel_id}.
    """
    if not rows:
        return {}
    result = {}
    with get_conn() as conn:
        existing = {
            r["url"]: r["id"]
            for r in conn.execute("SELECT id, url FROM channels WHERE source_id=?", (source_id,)).fetchall()
        }
        inserts = []
        for ch in rows:
            cid = existing.get(ch["url"])
            if cid:
                conn.execute(
                    "UPDATE channels SET name=?, inf_line=?, group_title=?, source_group=? WHERE id=?",
                    (ch["name"], ch["inf_line"], ch["group_title"], ch["source_group"], cid),
                )
                result[ch["url"]] = cid
            else:
                inserts.append(ch)
        for ch in inserts:
            cur = conn.execute(
                "INSERT INTO channels (source_id, name, url, inf_line, group_title, source_group) VALUES (?, ?, ?, ?, ?, ?)",
                (source_id, ch["name"], ch["url"], ch["inf_line"], ch["group_title"], ch["source_group"]),
            )
            result[ch["url"]] = cur.lastrowid
        return result


def update_channel_status(channel_id, is_alive, response_time_ms=None):
    with get_conn() as conn:
        alive_val = 1 if is_alive else 0
        conn.execute(
            "UPDATE channels SET is_alive=?, response_time_ms=?, last_check=?, total_checks=total_checks+1, alive_checks=alive_checks+? WHERE id=?",
            (alive_val, response_time_ms, time.time(), alive_val, channel_id),
        )


def toggle_channel(channel_id, enabled):
    with get_conn() as conn:
        conn.execute("UPDATE channels SET enabled=? WHERE id=?", (1 if enabled else 0, channel_id))


def set_channels_enabled(channel_ids, enabled):
    ids = list(channel_ids or [])
    if not ids:
        return 0
    ph = ",".join(["?"] * len(ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE channels SET enabled=? WHERE id IN ({ph})",
            [1 if enabled else 0] + ids,
        )
        return cur.rowcount


def update_channel_group(channel_id, group_title):
    with get_conn() as conn:
        conn.execute("UPDATE channels SET group_title=? WHERE id=?", (group_title, channel_id))
        row = conn.execute("SELECT inf_line FROM channels WHERE id=?", (channel_id,)).fetchone()
        if row and row["inf_line"]:
            new_inf = re.sub(r'group-title="[^"]*"', f'group-title="{group_title}"', row["inf_line"])
            if 'group-title' not in new_inf:
                new_inf = new_inf.replace("#EXTINF:", f'#EXTINF: group-title="{group_title}" ', 1)
            conn.execute("UPDATE channels SET inf_line=? WHERE id=?", (new_inf, channel_id))


# ---------------------------------------------------------------------------
# Groups: aliases, manual overrides, EPG categories, playlist snapshot
#
# Приоритет определения группы канала:
#   1. group_overrides (ручная правка по нормализованному имени)
#   2. group_aliases (подтверждённые пользователем слияния/переименования)
#   3. group-title из источника плейлиста
#   4. категория EPG (fallback, когда группа пустая или "Другое")
# ---------------------------------------------------------------------------

def get_group_aliases():
    with get_conn() as conn:
        return {r["raw"]: r["canonical"]
                for r in conn.execute("SELECT raw, canonical FROM group_aliases").fetchall()}


def get_alias_list():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT raw, canonical FROM group_aliases ORDER BY raw").fetchall()]


def set_group_alias(raw, canonical, raw_display=None):
    with get_conn() as conn:
        if raw_display:
            conn.execute(
                "INSERT INTO group_aliases (raw, canonical, raw_display) VALUES (?, ?, ?) "
                "ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical, raw_display=excluded.raw_display",
                (raw, canonical, raw_display),
            )
        else:
            conn.execute(
                "INSERT INTO group_aliases (raw, canonical) VALUES (?, ?) "
                "ON CONFLICT(raw) DO UPDATE SET canonical=excluded.canonical",
                (raw, canonical),
            )


def delete_group_alias(raw):
    with get_conn() as conn:
        conn.execute("DELETE FROM group_aliases WHERE raw=?", (raw,))


def get_alias_rows():
    """Сырые строки алиасов с исходным написанием и датой."""
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT raw, COALESCE(NULLIF(raw_display,''), raw) AS display, canonical, created_at "
            "FROM group_aliases ORDER BY created_at").fetchall()]


# ---------------------------------------------------------------------------
# Исключение групп из плейлиста
# ---------------------------------------------------------------------------

def get_excluded_groups():
    with get_conn() as conn:
        return {r["mech_name"]: dict(r) for r in conn.execute(
            "SELECT * FROM group_exclusions").fetchall()}


def set_group_exclusion(mech_name, group_title, disabled_ids=None):
    import json as _json
    ids = list(disabled_ids or [])
    with get_conn() as conn:
        row = conn.execute("SELECT disabled_ids FROM group_exclusions WHERE mech_name=?",
                           (mech_name,)).fetchone()
        if row:
            try:
                prev = set(_json.loads(row["disabled_ids"] or "[]"))
            except Exception:
                prev = set()
            ids = sorted(prev | set(ids))
        conn.execute(
            "INSERT INTO group_exclusions (mech_name, group_title, disabled_ids) VALUES (?, ?, ?) "
            "ON CONFLICT(mech_name) DO UPDATE SET group_title=excluded.group_title, disabled_ids=excluded.disabled_ids",
            (mech_name, group_title, _json.dumps(ids)),
        )
        return ids


def remove_group_exclusion(mech_name):
    import json as _json
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM group_exclusions WHERE mech_name=?",
                           (mech_name,)).fetchone()
        conn.execute("DELETE FROM group_exclusions WHERE mech_name=?", (mech_name,))
        if not row:
            return None
        d = dict(row)
        try:
            d["disabled_ids"] = _json.loads(d.get("disabled_ids") or "[]")
        except Exception:
            d["disabled_ids"] = []
        return d


# ---------------------------------------------------------------------------
# Учёт каналов, отключённых автоотсевом дублей (для точного возврата)
# ---------------------------------------------------------------------------

def record_dedup_disabled(channel_ids):
    ids = list(channel_ids or [])
    if not ids:
        return 0
    with get_conn() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO disabled_by_dedup (channel_id) VALUES (?)",
            [(i,) for i in ids],
        )
        return len(ids)


def unrecord_dedup_disabled(channel_ids=None):
    with get_conn() as conn:
        if channel_ids is None:
            cur = conn.execute("DELETE FROM disabled_by_dedup")
        else:
            ids = list(channel_ids)
            if not ids:
                return 0
            ph = ",".join(["?"] * len(ids))
            cur = conn.execute(f"DELETE FROM disabled_by_dedup WHERE channel_id IN ({ph})", ids)
        return cur.rowcount


def get_dedup_disabled():
    with get_conn() as conn:
        return {r["channel_id"] for r in conn.execute(
            "SELECT channel_id FROM disabled_by_dedup").fetchall()}


def bulk_move_group(old_groups, target):
    """Переместить все каналы из old_groups в target. Возвращает число каналов."""
    if not old_groups:
        return 0
    ph = ",".join(["?"] * len(old_groups))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE channels SET group_title=? WHERE group_title IN ({ph})",
            [target] + list(old_groups),
        )
        moved = cur.rowcount
        # Ручные правки следуют за слиянием: override со старым именем -> target
        conn.execute(
            f"UPDATE group_overrides SET group_title=?, updated_at=strftime('%s','now') WHERE group_title IN ({ph})",
            [target] + list(old_groups),
        )
        conn.execute(
            f"UPDATE playlist_groups SET group_title=?, updated_at=strftime('%s','now') WHERE group_title IN ({ph})",
            [target] + list(old_groups),
        )
        # Запомненные объединения тоже следуют за переименованием:
        # алиасы, указывавшие на старые имена, перенаправляются на target.
        # Иначе при следующем импорте старое имя группы воскреснет.
        # Сравнение - на Python-стороне: SQLite lower() не работает с кириллицей.
        def _k(s):
            return str(s or "").strip().lower().replace("ё", "е")
        old_set = {_k(g) for g in old_groups}
        stale = [r["raw"] for r in conn.execute(
            "SELECT raw, canonical FROM group_aliases").fetchall()
            if _k(r["canonical"]) in old_set]
        if stale:
            sph = ",".join(["?"] * len(stale))
            conn.execute(
                f"UPDATE group_aliases SET canonical=? WHERE raw IN ({sph})",
                [target] + stale,
            )
        # Исключения групп тоже следуют за переименованием/слиянием
        import json as _json
        excl_rows = [r for r in conn.execute(
            "SELECT mech_name, disabled_ids FROM group_exclusions").fetchall()
            if r["mech_name"] in old_set]
        if excl_rows:
            merged = set()
            for r in excl_rows:
                try:
                    merged |= set(_json.loads(r["disabled_ids"] or "[]"))
                except Exception:
                    pass
                conn.execute("DELETE FROM group_exclusions WHERE mech_name=?", (r["mech_name"],))
            new_mech = _k(target)
            prev = conn.execute("SELECT disabled_ids FROM group_exclusions WHERE mech_name=?",
                                (new_mech,)).fetchone()
            if prev:
                try:
                    merged |= set(_json.loads(prev["disabled_ids"] or "[]"))
                except Exception:
                    pass
            conn.execute(
                "INSERT INTO group_exclusions (mech_name, group_title, disabled_ids) VALUES (?, ?, ?) "
                "ON CONFLICT(mech_name) DO UPDATE SET group_title=excluded.group_title, disabled_ids=excluded.disabled_ids",
                (new_mech, target, _json.dumps(sorted(merged))),
            )
        return moved


def get_group_overrides():
    with get_conn() as conn:
        return {r["norm_name"]: r["group_title"]
                for r in conn.execute("SELECT norm_name, group_title FROM group_overrides").fetchall()}


def set_group_override(norm_name, group_title):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO group_overrides (norm_name, group_title, updated_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            (norm_name, group_title),
        )


def delete_group_overrides(norm_names=None):
    """Удалить override'ы. norm_names=None -> удалить все."""
    with get_conn() as conn:
        if norm_names is None:
            cur = conn.execute("DELETE FROM group_overrides")
        else:
            norms = list(norm_names)
            if not norms:
                return 0
            ph = ",".join(["?"] * len(norms))
            cur = conn.execute(f"DELETE FROM group_overrides WHERE norm_name IN ({ph})", norms)
        return cur.rowcount


def update_channels_group_by_ids(channel_ids, group_title):
    ids = list(channel_ids)
    if not ids:
        return 0
    ph = ",".join(["?"] * len(ids))
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE channels SET group_title=? WHERE id IN ({ph})",
            [group_title] + ids,
        )
        return cur.rowcount


def get_group_stats():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT group_title AS name, COUNT(*) AS channels, "
            "SUM(CASE WHEN is_alive=1 THEN 1 ELSE 0 END) AS alive, "
            "SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END) AS enabled "
            "FROM channels GROUP BY group_title ORDER BY channels DESC"
        ).fetchall()]


def save_epg_categories(mapping):
    with get_conn() as conn:
        conn.execute("DELETE FROM epg_categories")
        conn.executemany(
            "INSERT INTO epg_categories (norm_name, category, updated_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            [(k, v) for k, v in mapping.items() if k and v],
        )


def get_epg_categories():
    with get_conn() as conn:
        return {r["norm_name"]: r["category"]
                for r in conn.execute("SELECT norm_name, category FROM epg_categories").fetchall()}


def save_epg_icons(mapping):
    with get_conn() as conn:
        conn.execute("DELETE FROM epg_icons")
        conn.executemany(
            "INSERT INTO epg_icons (norm_name, icon_url, updated_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            [(k, v) for k, v in mapping.items() if k and v],
        )


def get_epg_icons():
    with get_conn() as conn:
        return {r["norm_name"]: r["icon_url"]
                for r in conn.execute("SELECT norm_name, icon_url FROM epg_icons").fetchall()}


def save_playlist_groups(mapping):
    with get_conn() as conn:
        conn.execute("DELETE FROM playlist_groups")
        conn.executemany(
            "INSERT INTO playlist_groups (norm_name, group_title, updated_at) "
            "VALUES (?, ?, strftime('%s','now'))",
            [(k, v) for k, v in mapping.items() if k and v],
        )


def get_playlist_groups():
    with get_conn() as conn:
        return {r["norm_name"]: r["group_title"]
                for r in conn.execute("SELECT norm_name, group_title FROM playlist_groups").fetchall()}


def delete_stale_channels(source_id, valid_urls):
    with get_conn() as conn:
        if valid_urls:
            placeholders = ",".join(["?"] * len(valid_urls))
            conn.execute(
                f"DELETE FROM channels WHERE source_id=? AND url NOT IN ({placeholders})",
                [source_id] + list(valid_urls),
            )
        else:
            conn.execute("DELETE FROM channels WHERE source_id=?", (source_id,))


CHANNEL_LIGHT_COLUMNS = (
    "c.id, c.source_id, c.name, c.group_title, c.enabled, c.is_alive, "
    "c.response_time_ms, c.total_checks, c.alive_checks, c.last_check, c.url"
)


def get_channels(source_id=None, alive_only=False, light=False, active_sources_only=False):
    with get_conn() as conn:
        cols = CHANNEL_LIGHT_COLUMNS if light else "c.*"
        q = (f"SELECT {cols}, s.name as source_name, s.enabled as source_enabled "
             f"FROM channels c LEFT JOIN sources s ON c.source_id=s.id")
        conditions, params = [], []
        if source_id:
            conditions.append("c.source_id=?")
            params.append(source_id)
        if alive_only:
            conditions.append("c.is_alive=1")
        if active_sources_only:
            conditions.append("s.enabled=1")
        if conditions:
            q += " WHERE " + " AND ".join(conditions)
        q += " ORDER BY c.group_title, c.name"
        return [dict(r) for r in conn.execute(q, params).fetchall()]


def get_stats():
    with get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM channels c JOIN sources s ON s.id=c.source_id WHERE s.enabled=1"
        ).fetchone()["cnt"]
        alive = conn.execute(
            "SELECT COUNT(*) as cnt FROM channels c JOIN sources s ON s.id=c.source_id WHERE s.enabled=1 AND c.is_alive=1"
        ).fetchone()["cnt"]
        sources = conn.execute("SELECT COUNT(*) as cnt FROM sources WHERE enabled=1").fetchone()["cnt"]
        inactive_sources = conn.execute(
            "SELECT COUNT(*) as cnt FROM sources WHERE enabled=0"
        ).fetchone()["cnt"]
        inactive_channels = conn.execute(
            "SELECT COUNT(*) as cnt FROM channels c JOIN sources s ON s.id=c.source_id WHERE s.enabled=0"
        ).fetchone()["cnt"]
        avg_ms = conn.execute(
            "SELECT AVG(c.response_time_ms) as avg_ms FROM channels c JOIN sources s ON s.id=c.source_id "
            "WHERE s.enabled=1 AND c.is_alive=1 AND c.response_time_ms IS NOT NULL"
        ).fetchone()["avg_ms"]
        return {
            "total_channels": total,
            "alive_channels": alive,
            "dead_channels": total - alive,
            "sources": sources,
            "inactive_sources": inactive_sources,
            "inactive_source_channels": inactive_channels,
            "avg_response_ms": round(avg_ms, 1) if avg_ms else None,
        }


def clear_channels_for_source(source_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM channels WHERE source_id=?", (source_id,))


def add_epg_source(name, url, enabled=True):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO epg_sources (name, url, enabled) VALUES (?, ?, ?)",
            (name, url, 1 if enabled else 0),
        )
        return cur.lastrowid


def update_epg_source(epg_id, name=None, url=None, enabled=None):
    with get_conn() as conn:
        fields, values = [], []
        if name is not None:
            fields.append("name=?")
            values.append(name)
        if url is not None:
            fields.append("url=?")
            values.append(url)
        if enabled is not None:
            fields.append("enabled=?")
            values.append(1 if enabled else 0)
        if fields:
            values.append(epg_id)
            conn.execute(f"UPDATE epg_sources SET {','.join(fields)} WHERE id=?", values)


def delete_epg_source(epg_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM epg_sources WHERE id=?", (epg_id,))


def get_epg_sources(enabled_only=False):
    with get_conn() as conn:
        q = "SELECT * FROM epg_sources"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY name"
        return [dict(r) for r in conn.execute(q).fetchall()]


def update_epg_download_stats(epg_id, channel_count):
    with get_conn() as conn:
        conn.execute(
            "UPDATE epg_sources SET last_download=?, channel_count=? WHERE id=?",
            (time.time(), channel_count, epg_id),
        )


def update_epg_source_status(epg_id, is_alive, response_time_ms=None, error_message=None):
    with get_conn() as conn:
        conn.execute(
            "UPDATE epg_sources SET is_alive=?, response_time_ms=?, last_check=?, error_message=? WHERE id=?",
            (1 if is_alive else 0, response_time_ms, time.time(), error_message, epg_id),
        )


def log_access(endpoint, ip_address, user_agent):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO access_log (endpoint, ip_address, user_agent) VALUES (?, ?, ?)",
            (endpoint, ip_address, user_agent),
        )


def get_access_log(endpoint=None, limit=10, offset=0, date_from=None, date_to=None):
    with get_conn() as conn:
        conditions = []
        params = []
        if endpoint:
            conditions.append("endpoint=?")
            params.append(endpoint)
        if date_from:
            from datetime import datetime
            ts = datetime.strptime(date_from, "%Y-%m-%d").timestamp()
            conditions.append("timestamp>=?")
            params.append(ts)
        if date_to:
            from datetime import datetime, timedelta
            ts = datetime.strptime(date_to, "%Y-%m-%d").timestamp() + 86400
            conditions.append("timestamp<=?")
            params.append(ts)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = conn.execute(
            f"SELECT * FROM access_log{where} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()
        total = conn.execute(f"SELECT COUNT(*) as cnt FROM access_log{where}", params).fetchone()["cnt"]
        return {"items": [dict(r) for r in rows], "total": total}


def get_access_stats(date_from=None, date_to=None):
    with get_conn() as conn:
        conditions = []
        params = []
        if date_from:
            ts = datetime.strptime(date_from, "%Y-%m-%d").timestamp()
            conditions.append("timestamp>=?")
            params.append(ts)
        if date_to:
            ts = datetime.strptime(date_to, "%Y-%m-%d").timestamp() + 86400
            conditions.append("timestamp<=?")
            params.append(ts)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        total = conn.execute(f"SELECT COUNT(*) as cnt FROM access_log{where}", params).fetchone()["cnt"]
        playlist = conn.execute(f"SELECT COUNT(*) as cnt FROM access_log{where}{' AND' if where else ' WHERE '} endpoint='playlist'", params).fetchone()["cnt"]
        epg = conn.execute(f"SELECT COUNT(*) as cnt FROM access_log{where}{' AND' if where else ' WHERE '} endpoint='epg'", params).fetchone()["cnt"]
        unique_ips = conn.execute(f"SELECT COUNT(DISTINCT ip_address) as cnt FROM access_log{where}", params).fetchone()["cnt"]
        return {
            "total": total,
            "playlist": playlist,
            "epg": epg,
            "unique_ips": unique_ips,
        }


def delete_access_log(date_from=None, date_to=None):
    with get_conn() as conn:
        conditions = []
        params = []
        if date_from:
            ts = datetime.strptime(date_from, "%Y-%m-%d").timestamp()
            conditions.append("timestamp>=?")
            params.append(ts)
        if date_to:
            ts = datetime.strptime(date_to, "%Y-%m-%d").timestamp() + 86400
            conditions.append("timestamp<=?")
            params.append(ts)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        result = conn.execute(f"DELETE FROM access_log{where}", params)
        return result.rowcount
