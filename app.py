#!/usr/bin/env python3
import os
import re
import time
import uuid
import json
import hashlib
import secrets
import threading
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Response, Request, UploadFile, File, Form
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel
import database as db
import checker_core as checker
import epg as epg_module
import groups as groups_module

SOURCES_DIR = os.environ.get("SOURCES_DIR", "/data/sources")
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

sessions = {}
scheduler_running = False
scheduler_thread = None
check_in_progress = False
last_check_time = 0.0
last_epg_update_time = 0.0
check_progress = {"current": 0, "total": 0, "stage": ""}


def scheduler_loop():
    global scheduler_running, last_check_time
    while scheduler_running:
        interval = int(db.get_setting("check_interval", "3600"))
        now = time.time()
        wait = max(0, interval - (now - last_check_time))
        time.sleep(wait)
        if not scheduler_running:
            break
        run_check_background()


def run_check_background():
    global check_in_progress, last_check_time, check_progress
    if check_in_progress:
        print("[scheduler] Check already in progress, skipping")
        return
    check_in_progress = True
    check_progress = {"current": 0, "total": 0, "stage": "Starting check"}
    try:
        print("[scheduler] Starting check...")

        def update_progress(current, total, stage):
            global check_progress
            check_progress = {"current": current, "total": total, "stage": stage}

        checker.run_check(progress_callback=update_progress)
        last_check_time = time.time()
        check_progress = {"current": 1, "total": 1, "stage": "Generating playlist"}
        generate_playlist_file()
        check_progress = {"current": 1, "total": 1, "stage": "Updating TV Guide"}
        try:
            global epg_channel_count, last_epg_update_time
            before_build = float(db.get_setting("epg_last_build", "0") or 0)
            epg_channel_count = epg_module.update_epg() or 0
            after_build = float(db.get_setting("epg_last_build", "0") or 0)
            if after_build > before_build:
                last_epg_update_time = after_build
        except Exception as e:
            print(f"[scheduler] EPG update failed: {e}")
    except Exception as e:
        print(f"[scheduler] Check failed: {e}")
    finally:
        check_progress = {"current": 0, "total": 0, "stage": ""}
        check_in_progress = False


def start_scheduler():
    global scheduler_running, scheduler_thread
    if scheduler_running:
        return
    scheduler_running = True
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True)
    scheduler_thread.start()


def stop_scheduler():
    global scheduler_running
    scheduler_running = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    global last_epg_update_time
    db.init_db()
    try:
        epg_module.cleanup_cache_temp()
    except Exception as e:
        print(f"[epg] Cache cleanup failed: {e}")
    try:
        last_epg_update_time = float(db.get_setting("epg_last_build", "0") or 0)
    except Exception:
        last_epg_update_time = 0.0
    # Если epg.xml есть, а .gz отсутствует/устарел — достроить в фоне.
    threading.Thread(target=epg_module.ensure_epg_gzip, daemon=True).start()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="TANgle - IPTV Checker", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def add_cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (path == "/" or path.startswith("/static/") or path == "/epg.xml" or path == "/epg.xml.gz"
            or path.endswith(".m3u") or path.endswith(".m3u8")):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


class SourceCreate(BaseModel):
    name: str
    url: str
    enabled: bool = True


class SourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None


class SettingsUpdate(BaseModel):
    check_interval: int | None = None
    check_timeout: int | None = None
    check_parallel: int | None = None
    auth_login: str | None = None
    auth_password: str | None = None
    playlist_all: bool | None = None
    playlist_fast: bool | None = None
    playlist_medium: bool | None = None
    playlist_slow: bool | None = None
    epg_update_interval: int | None = None
    availability_period_days: int | None = None
    min_availability: int | None = None
    dedup_strategy: str | None = None
    auto_group_synonyms: bool | None = None
    playlist_filename: str | None = None
    public_base_url: str | None = None


class LoginRequest(BaseModel):
    login: str
    password: str


class EPGSourceCreate(BaseModel):
    name: str
    url: str
    enabled: bool = True


class EPGSourceUpdate(BaseModel):
    name: str | None = None
    url: str | None = None
    enabled: bool | None = None


class GroupRename(BaseModel):
    old: str
    new: str
    remember: bool = True


class GroupMerge(BaseModel):
    sources: list[str]
    target: str
    remember: bool = True


class BulkGroup(BaseModel):
    ids: list[int]
    group: str


class GroupReset(BaseModel):
    group: str | None = None


class GroupUnmerge(BaseModel):
    raws: list[str]
    canonical: str
    preview: bool = False


class GroupExclude(BaseModel):
    group: str
    excluded: bool = True


class GroupExcludeBulk(BaseModel):
    groups: list[str]
    excluded: bool = True


class GroupResetBulk(BaseModel):
    groups: list[str]


class DupAction(BaseModel):
    norm_name: str
    channel_id: int | None = None


class DupAuto(BaseModel):
    preview: bool = True


def get_session_token(request: Request):
    token = request.cookies.get("session")
    if token and token in sessions:
        return token
    return None


def require_auth(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(401, detail="Unauthorized")
    return token


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not get_session_token(request):
        return RedirectResponse(url="/login")
    html_path = os.path.join(os.path.dirname(__file__), "static", "index.html")
    with open(html_path, "rb") as f:
        raw = f.read()
    etag = '"' + hashlib.sha1(raw).hexdigest() + '"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return HTMLResponse(content=raw, headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    html_path = os.path.join(os.path.dirname(__file__), "static", "login.html")
    with open(html_path, "r", encoding="utf-8") as f:
        return f.read()


@app.post("/api/login")
async def login(req: LoginRequest, response: Response):
    login_val = db.get_setting("auth_login", "admin")
    password_val = db.get_setting("auth_password", "admin")
    if req.login == login_val and req.password == password_val:
        token = secrets.token_hex(32)
        sessions[token] = req.login
        response.set_cookie("session", token, httponly=True, max_age=86400)
        return {"ok": True}
    raise HTTPException(401, detail="Invalid credentials")


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    token = get_session_token(request)
    if token:
        sessions.pop(token, None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/check_login")
async def check_login(request: Request):
    return {"logged_in": get_session_token(request) is not None}


@app.get("/api/stats")
async def get_stats():
    return db.get_stats()


@app.get("/api/settings")
async def get_settings():
    return {
        "check_interval": int(db.get_setting("check_interval", "3600")),
        "check_timeout": int(db.get_setting("check_timeout", "10")),
        "check_parallel": int(db.get_setting("check_parallel", "50")),
        "auth_login": db.get_setting("auth_login", "admin"),
        "auth_password": db.get_setting("auth_password", "admin"),
        "playlist_all": db.get_setting("playlist_all", "1") == "1",
        "playlist_fast": db.get_setting("playlist_fast", "1") == "1",
        "playlist_medium": db.get_setting("playlist_medium", "1") == "1",
        "playlist_slow": db.get_setting("playlist_slow", "1") == "1",
        "epg_update_interval": int(db.get_setting("epg_update_interval", "86400")),
        "availability_period_days": int(db.get_setting("availability_period_days", "7")),
        "min_availability": int(db.get_setting("min_availability", "0")),
        "dedup_strategy": db.get_setting("dedup_strategy", "availability"),
        "auto_group_synonyms": db.get_setting("auto_group_synonyms", "1") == "1",
        "playlist_filename": playlist_filename(),
        "public_base_url": db.get_setting("public_base_url", ""),
    }


@app.put("/api/settings")
async def update_settings(s: SettingsUpdate):
    if s.check_interval is not None:
        db.set_setting("check_interval", s.check_interval)
    if s.check_timeout is not None:
        db.set_setting("check_timeout", s.check_timeout)
    if s.check_parallel is not None:
        db.set_setting("check_parallel", s.check_parallel)
    if s.auth_login is not None:
        db.set_setting("auth_login", s.auth_login)
    if s.auth_password is not None:
        db.set_setting("auth_password", s.auth_password)
    if s.playlist_all is not None:
        db.set_setting("playlist_all", "1" if s.playlist_all else "0")
    if s.playlist_fast is not None:
        db.set_setting("playlist_fast", "1" if s.playlist_fast else "0")
    if s.playlist_medium is not None:
        db.set_setting("playlist_medium", "1" if s.playlist_medium else "0")
    if s.playlist_slow is not None:
        db.set_setting("playlist_slow", "1" if s.playlist_slow else "0")
    if s.epg_update_interval is not None:
        db.set_setting("epg_update_interval", s.epg_update_interval)
    if s.availability_period_days is not None:
        db.set_setting("availability_period_days", s.availability_period_days)
    if s.min_availability is not None:
        db.set_setting("min_availability", s.min_availability)
    if s.dedup_strategy is not None:
        if s.dedup_strategy not in ("availability", "speed"):
            raise HTTPException(400, detail="Unknown dedup_strategy")
        db.set_setting("dedup_strategy", s.dedup_strategy)
    if s.auto_group_synonyms is not None:
        db.set_setting("auto_group_synonyms", "1" if s.auto_group_synonyms else "0")
    if s.public_base_url is not None:
        base = (s.public_base_url or "").strip().rstrip("/")
        if base and not re.match(r"^https?://[^/\s]+$", base):
            raise HTTPException(400, detail="Публичный адрес должен быть вида http://host:port")
        db.set_setting("public_base_url", base)
    if s.playlist_filename is not None:
        new_name = (s.playlist_filename or "").strip()
        if not valid_playlist_filename(new_name):
            raise HTTPException(400, detail="Недопустимое имя файла плейлиста (латиница, цифры, . _ -, окончание .m3u/.m3u8)")
        old_name = playlist_filename()
        if new_name != old_name:
            db.set_setting("playlist_filename", new_name)
            try:
                generate_playlist_file()
            except Exception as e:
                db.set_setting("playlist_filename", old_name)
                raise HTTPException(500, detail=f"Не удалось пересобрать плейлист: {str(e)[:200]}")
            old_path = os.path.join(PLAYLIST_DIR, old_name)
            new_path = os.path.join(PLAYLIST_DIR, new_name)
            if old_path != new_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except OSError as e:
                    print(f"[playlist] Failed to remove old file {old_path}: {e}")
    return {"ok": True}


@app.get("/api/sources")
async def list_sources():
    return db.get_sources()


@app.post("/api/sources")
async def create_source(s: SourceCreate):
    try:
        text = checker.fetch_source(s.url)
        channels = _validate_playlist_text(text)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Не удалось загрузить плейлист: {str(e)[:200]}")
    sid = db.add_source(s.name, s.url, s.enabled)
    db.update_source_status(sid, True, len(channels))
    return {"id": sid, "ok": True, "channels_found": len(channels)}


def _validate_playlist_text(text):
    if not text or not text.strip():
        raise HTTPException(400, detail="Файл пустой")
    if not text.strip().startswith("#EXTM3U") and "#EXTINF:" not in text:
        raise HTTPException(400, detail="Файл не является M3U/M3U8 плейлистом (не найдены теги #EXTM3U или #EXTINF)")
    channels = checker.parse_m3u(text)
    if not channels:
        raise HTTPException(400, detail="Плейлист не содержит каналов")
    return channels


def _decode_upload(raw):
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def _read_upload(file: UploadFile):
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, detail=f"Файл больше {MAX_UPLOAD_BYTES // (1024 * 1024)} МБ")
    name = (file.filename or "").strip()
    if name and not name.lower().endswith((".m3u", ".m3u8")):
        raise HTTPException(400, detail="Поддерживаются только файлы .m3u и .m3u8")
    return raw, name or "playlist.m3u"


def _store_upload_file(raw, original_name):
    os.makedirs(SOURCES_DIR, exist_ok=True)
    path = os.path.join(SOURCES_DIR, uuid.uuid4().hex + ".m3u")
    tmp_path = path + ".tmp"
    with open(tmp_path, "wb") as f:
        f.write(raw)
    os.replace(tmp_path, path)
    return path


@app.post("/api/sources/upload")
async def upload_source_file(name: str = Form(...), enabled: bool = Form(True), file: UploadFile = File(...)):
    raw, original_name = await _read_upload(file)
    channels = _validate_playlist_text(_decode_upload(raw))
    path = _store_upload_file(raw, original_name)
    sid = db.add_source(name, "", enabled, "file", path, original_name)
    source = {"id": sid}
    imported = checker.import_source(source, _decode_upload(raw))
    db.update_source_status(sid, True, imported)
    return {"id": sid, "ok": True, "channels_found": imported}


@app.post("/api/sources/{source_id}/file")
async def replace_source_file(source_id: int, file: UploadFile = File(...)):
    sources = {s["id"]: s for s in db.get_sources()}
    source = sources.get(source_id)
    if not source:
        raise HTTPException(404, detail="Источник не найден")
    raw, original_name = await _read_upload(file)
    text = _decode_upload(raw)
    channels = _validate_playlist_text(text)
    old_path = source.get("file_path")
    path = _store_upload_file(raw, original_name)
    db.set_source_file(source_id, path, original_name)
    source["file_path"] = path
    imported = checker.import_source(source, text)
    db.update_source_status(source_id, True, imported)
    if old_path and old_path != path and os.path.exists(old_path):
        try:
            os.remove(old_path)
        except OSError:
            pass
    return {"ok": True, "channels_found": imported}


@app.put("/api/sources/{source_id}")
async def update_source(source_id: int, s: SourceUpdate):
    source = next((x for x in db.get_sources() if x["id"] == source_id), None)
    if not source:
        raise HTTPException(404, detail="Источник не найден")
    url = s.url
    if (source.get("source_type") or "url") == "file":
        url = None  # файловому источнику URL не меняем
    db.update_source(source_id, s.name, url, s.enabled)
    return {"ok": True}


@app.delete("/api/sources/{source_id}")
async def delete_source(source_id: int):
    removed = db.delete_source(source_id)
    if removed and removed.get("source_type") == "file" and removed.get("file_path"):
        try:
            if os.path.exists(removed["file_path"]):
                os.remove(removed["file_path"])
        except OSError as e:
            print(f"[sources] Failed to remove file {removed['file_path']}: {e}")
    return {"ok": True}


@app.get("/api/epg-sources")
async def list_epg_sources():
    return db.get_epg_sources()


@app.post("/api/epg-sources")
async def create_epg_source(s: EPGSourceCreate):
    alive, ms, error = epg_module.check_epg_source(s.url, timeout=10)
    if not alive:
        raise HTTPException(400, detail=f"Источник недоступен: {error or 'HTTP error'}")
    tmp_path = None
    try:
        tmp_path = epg_module.download_epg_source(s.url)
        if tmp_path is None:
            raise HTTPException(400, detail="Не удалось скачать файл")
        channels_found, parse_error = epg_module.validate_source_file(tmp_path)
        if parse_error:
            raise HTTPException(400, detail=f"Ошибка парсинга: {parse_error}")
        if not channels_found:
            raise HTTPException(400, detail="Файл не содержит XMLTV данные (нет каналов). Поддерживаются: .xml, .xml.gz, .zip с XML")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, detail=f"Ошибка парсинга: {str(e)[:200]}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    sid = db.add_epg_source(s.name, s.url, s.enabled)
    return {"id": sid, "ok": True, "channels_found": channels_found}


@app.put("/api/epg-sources/{epg_id}")
async def update_epg_source(epg_id: int, s: EPGSourceUpdate):
    db.update_epg_source(epg_id, s.name, s.url, s.enabled)
    return {"ok": True}


@app.delete("/api/epg-sources/{epg_id}")
async def delete_epg_source(epg_id: int):
    db.delete_epg_source(epg_id)
    return {"ok": True}


@app.post("/api/epg/download")
async def download_epg():
    try:
        epg_module.download_epg_sources()
        return {"ok": True, "message": "EPG sources downloaded"}
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@app.post("/api/epg/rebuild")
async def rebuild_epg(force: bool = True):
    """Пересобрать EPG (опционально форсированно) в фоне."""
    if check_in_progress:
        raise HTTPException(409, detail="Check or rebuild already in progress")

    def worker():
        global check_in_progress, check_progress, last_epg_update_time, epg_channel_count
        check_in_progress = True
        check_progress = {"current": 1, "total": 1, "stage": "Updating TV Guide"}
        try:
            epg_module.cleanup_cache_temp()
            epg_channel_count = epg_module.update_epg(force=force) or 0
            last_epg_update_time = time.time()
        except Exception as e:
            print(f"[epg] Rebuild failed: {e}")
        finally:
            check_progress = {"current": 0, "total": 0, "stage": ""}
            check_in_progress = False

    threading.Thread(target=worker, daemon=True).start()
    return {"ok": True, "message": "EPG rebuild started"}


@app.get("/api/epg")
async def get_epg(request: Request):
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    db.log_access("epg", client_ip, user_agent)
    if not os.path.exists(epg_module.EPG_PATH):
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><tv/>',
                        media_type="application/xml")
    return await _epg_response(request)


@app.get("/api/channels")
async def list_channels(source_id: int | None = None, alive: bool | None = None):
    channels = db.get_channels(source_id=source_id, alive_only=(alive is True), light=True)
    payload = json.dumps(_serialize_channels(channels), ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
    return Response(content=payload, media_type="application/json")


def _serialize_channels(channels):
    """Лёгкая выдача для таблицы + метки дублей (без inf_line/source_group)."""
    st = _selection_settings()
    norm_by_id = {}
    by_norm = {}
    for ch in channels:
        norm = groups_module.norm_name(ch.get("name") or "")
        norm_by_id[ch["id"]] = norm
        if norm:
            by_norm.setdefault(norm, []).append(ch)
    winner_by_norm = {}
    count_by_norm = {}
    for norm, copies in by_norm.items():
        if len(copies) < 2:
            continue
        count_by_norm[norm] = len(copies)
        cand = [c for c in copies if groups_module.passes_playlist_filters(
            c, pl_fast=st["pl_fast"], pl_medium=st["pl_medium"], pl_slow=st["pl_slow"],
            min_avail=st["min_avail"], excluded=st["excluded"])]
        winner = groups_module.pick_winner(cand, st["strategy"]) if cand else None
        if winner:
            winner_by_norm[norm] = winner["id"]
    fields = ("id", "source_id", "source_name", "name", "group_title", "enabled",
              "is_alive", "response_time_ms", "total_checks", "alive_checks",
              "last_check", "url")
    result = []
    for ch in channels:
        row = {f: ch.get(f) for f in fields}
        norm = norm_by_id.get(ch["id"], "")
        row["norm_name"] = norm
        row["dup_count"] = count_by_norm.get(norm, 1)
        row["dup_winner"] = winner_by_norm.get(norm) == ch["id"]
        result.append(row)
    return result


@app.put("/api/channels/{channel_id}/toggle")
async def toggle_channel(channel_id: int, enabled: bool):
    db.toggle_channel(channel_id, enabled)
    return {"ok": True}


@app.put("/api/channels/{channel_id}/group")
async def update_channel_group(channel_id: int, group_title: str):
    group_title = (group_title or "").strip()
    if not group_title:
        raise HTTPException(400, detail="Empty group")
    channels = db.get_channels()
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        raise HTTPException(404, detail="Channel not found")
    norm = groups_module.norm_name(target["name"])
    # Правка привязывается к имени канала: применяется ко всем дублям
    db.set_group_override(norm, group_title)
    affected = 0
    for c in channels:
        if groups_module.norm_name(c["name"]) == norm:
            db.update_channel_group(c["id"], group_title)
            affected += 1
    return {"ok": True, "affected": affected}


@app.delete("/api/channels/{channel_id}/override")
async def reset_channel_override(channel_id: int):
    """Сбросить ручную правку: вернуться к группе из источника."""
    channels = db.get_channels()
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        raise HTTPException(404, detail="Channel not found")
    norm = groups_module.norm_name(target["name"])
    db.delete_group_overrides([norm])
    aliases = db.get_group_aliases()
    auto_syn = db.get_setting("auto_group_synonyms", "1") == "1"
    affected = 0
    for c in channels:
        if groups_module.norm_name(c["name"]) == norm:
            raw = c.get("source_group") or c.get("group_title") or "Другое"
            db.update_channel_group(c["id"], groups_module.canonicalize_import(raw, aliases, auto_synonyms=auto_syn))
            affected += 1
    return {"ok": True, "affected": affected}


@app.get("/api/channels/{channel_id}/duplicates")
async def channel_duplicates(channel_id: int):
    channels = db.get_channels()
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        raise HTTPException(404, detail="Channel not found")
    norm = groups_module.norm_name(target["name"])
    dupes = [c for c in channels if groups_module.norm_name(c["name"]) == norm]
    overrides = db.get_group_overrides()
    return {
        "norm_name": norm,
        "count": len(dupes),
        "ids": [c["id"] for c in dupes],
        "groups": sorted({c["group_title"] for c in dupes}),
        "has_override": norm in overrides,
        "override": overrides.get(norm),
    }


@app.put("/api/channels/bulk-group")
async def bulk_set_group(b: BulkGroup):
    group_title = (b.group or "").strip()
    if not group_title:
        raise HTTPException(400, detail="Empty group")
    if not b.ids:
        raise HTTPException(400, detail="No channels selected")
    channels = db.get_channels()
    wanted = set(b.ids)
    norms = {groups_module.norm_name(c["name"]) for c in channels if c["id"] in wanted}
    norms.discard("")
    for norm in norms:
        db.set_group_override(norm, group_title)
    affected = 0
    for c in channels:
        if groups_module.norm_name(c["name"]) in norms:
            db.update_channel_group(c["id"], group_title)
            affected += 1
    return {"ok": True, "affected": affected, "names": len(norms)}


@app.get("/api/groups")
async def list_groups():
    stats = db.get_group_stats()
    playlist_groups = db.get_playlist_groups()
    in_playlist = {}
    for g in playlist_groups.values():
        in_playlist[g] = in_playlist.get(g, 0) + 1
    excluded = db.get_excluded_groups()
    overrides = db.get_group_overrides()
    channels = db.get_channels()
    manual_by_group = {}
    for c in channels:
        if groups_module.norm_name(c["name"]) in overrides:
            manual_by_group[c["group_title"]] = manual_by_group.get(c["group_title"], 0) + 1
    return {
        "groups": [
            {
                "name": s["name"],
                "channels": s["channels"],
                "alive": s["alive"] or 0,
                "enabled": s["enabled"] or 0,
                "in_playlist": in_playlist.get(s["name"], 0),
                "excluded": groups_module.mech_key(s["name"]) in excluded,
                "manual_overrides": manual_by_group.get(s["name"], 0),
            }
            for s in stats
        ],
        "aliases": db.get_alias_list(),
        "merges": _build_merges(channels),
        "overrides_count": len(overrides),
    }


def _build_merges(channels):
    """Объединения, сгруппированные по цели, с читаемыми именами."""
    rows = db.get_alias_rows()
    by_raw_source = {}
    for c in channels:
        sg = (c.get("source_group") or "").strip()
        key = groups_module.mech_key(sg)
        if key and key not in by_raw_source:
            by_raw_source[key] = sg
    grouped = {}
    for a in rows:
        grouped.setdefault(a["canonical"], []).append(a)
    result = []
    for canonical, items in grouped.items():
        sources = []
        total = 0
        for a in items:
            display = a["display"] if a["display"] != a["raw"] else by_raw_source.get(a["raw"], a["raw"])
            matched = sum(1 for c in channels
                          if groups_module.mech_key(c.get("source_group") or "") == a["raw"]
                          and c["group_title"] == canonical)
            total += matched
            sources.append({"raw": a["raw"], "display": display,
                            "channels": matched, "created_at": a["created_at"]})
        sources.sort(key=lambda s: -s["channels"])
        result.append({"canonical": canonical, "sources": sources,
                       "channels": total, "aliases": len(sources)})
    result.sort(key=lambda r: -r["channels"])
    return result


@app.get("/api/groups/suggestions")
async def group_suggestions():
    stats = db.get_group_stats()
    return groups_module.suggest_merges(
        [(s["name"], s["channels"]) for s in stats],
        aliases=db.get_group_aliases(),
    )


@app.get("/api/groups/aliases")
async def list_aliases():
    return db.get_alias_rows()


@app.post("/api/groups/merge")
async def merge_groups(m: GroupMerge):
    target = (m.target or "").strip()
    sources = [s for s in (m.sources or []) if s and s != target]
    if not target or not sources:
        raise HTTPException(400, detail="Need target and at least one source group")
    if m.remember:
        for s in sources:
            db.set_group_alias(groups_module.mech_key(s), target, s.strip())
    moved = db.bulk_move_group(sources, target)
    return {"ok": True, "moved": moved}


@app.get("/api/groups/rename/preview")
async def rename_preview(old: str, new: str):
    old = (old or "").strip()
    new = (new or "").strip()
    if not old or not new:
        raise HTTPException(400, detail="Need old and new names")
    channels = db.get_channels()
    ch_count = sum(1 for c in channels if c["group_title"] == old)
    aliases = db.get_group_aliases()
    retarget = [k for k, v in aliases.items()
                if groups_module.mech_key(v) == groups_module.mech_key(old)
                and groups_module.mech_key(v) != groups_module.mech_key(new)]
    return {
        "channels": ch_count,
        "aliases_retargeted": len(retarget),
        "target_exists": any(c["group_title"] == new for c in channels),
        "noop": groups_module.mech_key(old) == groups_module.mech_key(new),
    }


@app.post("/api/groups/unmerge")
async def unmerge_groups(u: GroupUnmerge):
    target = (u.canonical or "").strip()
    raws = [groups_module.mech_key(r) for r in (u.raws or []) if r]
    if not target or not raws:
        raise HTTPException(400, detail="Need canonical and at least one source")
    aliases = db.get_group_aliases()
    remaining = {k: v for k, v in aliases.items() if k not in raws}
    channels = db.get_channels()
    affected = [c for c in channels
                if groups_module.mech_key(c.get("source_group") or "") in raws
                and c["group_title"] == target]
    if u.preview:
        return {"ok": True, "channels": len(affected), "aliases": len(raws)}
    overrides = db.get_group_overrides()
    auto_syn = db.get_setting("auto_group_synonyms", "1") == "1"
    restored = 0
    skipped = 0
    for c in affected:
        if groups_module.norm_name(c["name"]) in overrides:
            skipped += 1
            continue
        raw = c.get("source_group") or target
        db.update_channel_group(c["id"], groups_module.canonicalize_import(raw, remaining, auto_synonyms=auto_syn))
        restored += 1
    for r in raws:
        db.delete_group_alias(r)
    return {"ok": True, "restored": restored,
            "skipped_overrides": skipped, "aliases_deleted": len(raws)}


def _apply_group_exclusion(group, excluded, channels, by_id):
    """Исключить группу из плейлиста или вернуть её. Возвращает число каналов."""
    mech = groups_module.mech_key(group)
    if excluded:
        ids = [c["id"] for c in channels
               if groups_module.mech_key(c.get("group_title") or "") == mech and c.get("enabled", 1)]
        db.set_group_exclusion(mech, group, ids)
        db.set_channels_enabled(ids, False)
        return len(ids)
    row = db.remove_group_exclusion(mech)
    recorded = (row or {}).get("disabled_ids") or []
    to_enable = [i for i in recorded
                 if i in by_id
                 and not by_id[i].get("enabled", 1)
                 and groups_module.mech_key(by_id[i].get("group_title") or "") == mech]
    db.set_channels_enabled(to_enable, True)
    return len(to_enable)


@app.put("/api/groups/exclude")
async def exclude_group(e: GroupExclude):
    group = (e.group or "").strip()
    if not group:
        raise HTTPException(400, detail="Empty group")
    channels = db.get_channels()
    by_id = {c["id"]: c for c in channels}
    count = _apply_group_exclusion(group, e.excluded, channels, by_id)
    if e.excluded:
        return {"ok": True, "excluded": True, "disabled": count}
    return {"ok": True, "excluded": False, "enabled": count}


@app.put("/api/groups/exclude-bulk")
async def exclude_groups_bulk(e: GroupExcludeBulk):
    groups = [g.strip() for g in (e.groups or []) if g and g.strip()]
    if not groups:
        raise HTTPException(400, detail="No groups selected")
    channels = db.get_channels()
    by_id = {c["id"]: c for c in channels}
    total = sum(_apply_group_exclusion(g, e.excluded, channels, by_id) for g in groups)
    if e.excluded:
        return {"ok": True, "excluded": True, "groups": len(groups), "disabled": total}
    return {"ok": True, "excluded": False, "groups": len(groups), "enabled": total}


@app.put("/api/groups/rename")
async def rename_group(r: GroupRename):
    return await merge_groups(GroupMerge(sources=[r.old], target=r.new.strip(), remember=r.remember))


@app.delete("/api/groups/alias")
async def delete_alias(raw: str):
    db.delete_group_alias(groups_module.mech_key(raw))
    return {"ok": True}


def _reset_overrides_for_groups(groups, channels=None, aliases=None):
    """Сбросить ручные правки для указанных групп (None/[] -> все группы)."""
    channels = channels if channels is not None else db.get_channels()
    aliases = aliases if aliases is not None else db.get_group_aliases()
    auto_syn = db.get_setting("auto_group_synonyms", "1") == "1"
    if groups:
        wanted = set(groups)
        norms = {groups_module.norm_name(c["name"]) for c in channels if c["group_title"] in wanted}
    else:
        norms = set(db.get_group_overrides().keys())
    norms.discard("")
    if not norms:
        return 0
    db.delete_group_overrides(list(norms))
    affected = 0
    for c in channels:
        if groups_module.norm_name(c["name"]) in norms:
            raw = c.get("source_group") or c.get("group_title") or "Другое"
            db.update_channel_group(c["id"], groups_module.canonicalize_import(raw, aliases, auto_synonyms=auto_syn))
            affected += 1
    return affected


@app.post("/api/groups/reset")
async def reset_group_overrides(r: GroupReset):
    """Сбросить ручные правки (для одной группы или все) и пересчитать из источников."""
    groups = [r.group] if r.group else None
    return {"ok": True, "affected": _reset_overrides_for_groups(groups)}


@app.post("/api/groups/reset-bulk")
async def reset_group_overrides_bulk(r: GroupResetBulk):
    """Сбросить ручные правки для нескольких выбранных групп."""
    groups = [g for g in (r.groups or []) if g]
    if not groups:
        raise HTTPException(400, detail="No groups selected")
    return {"ok": True, "affected": _reset_overrides_for_groups(groups)}


@app.get("/api/debug/headers")
async def debug_headers(request: Request):
    headers = dict(request.headers)
    return {
        "client": str(request.client),
        "headers": headers,
    }


def _selection_settings():
    return {
        "pl_fast": db.get_setting("playlist_fast", "1") == "1",
        "pl_medium": db.get_setting("playlist_medium", "1") == "1",
        "pl_slow": db.get_setting("playlist_slow", "1") == "1",
        "min_avail": int(db.get_setting("min_availability", "0")),
        "excluded": set(db.get_excluded_groups().keys()),
        "strategy": db.get_setting("dedup_strategy", "availability"),
    }


def _dup_availability(c):
    total = c.get("total_checks", 0) or 0
    if total <= 0:
        return None
    return round((c.get("alive_checks", 0) or 0) / total * 100, 1)


def _serialize_copy(c, winner_id):
    return {
        "id": c["id"],
        "source_id": c.get("source_id"),
        "source_name": c.get("source_name") or "",
        "name": c["name"],
        "group_title": c.get("group_title") or "",
        "enabled": bool(c.get("enabled", 1)),
        "is_alive": bool(c.get("is_alive")),
        "response_time_ms": c.get("response_time_ms"),
        "availability": _dup_availability(c),
        "last_check": c.get("last_check"),
        "is_winner": c["id"] == winner_id,
    }


def _duplicate_keys(channels):
    by_norm = {}
    for c in channels:
        key = groups_module.norm_name(c.get("name") or "")
        if key:
            by_norm.setdefault(key, []).append(c)
    return {k: v for k, v in by_norm.items() if len(v) > 1}


@app.get("/api/duplicates")
async def list_duplicates(search: str | None = None, cross_source: bool | None = None,
                          limit: int = 50, offset: int = 0):
    channels = db.get_channels()
    sources = {s["id"]: s["name"] for s in db.get_sources()}
    for c in channels:
        c["source_name"] = sources.get(c.get("source_id"), "")
    st = _selection_settings()
    keys = _duplicate_keys(channels)
    # Поиск по нормализованному имени: "Первый канал HD" найдёт ключ "первый канал"
    needle = groups_module.norm_name(search) if search else ""
    items = []
    for norm, copies in keys.items():
        if needle and needle not in norm:
            continue
        srcs = {c.get("source_id") for c in copies}
        is_cross = len(srcs) > 1
        if cross_source is True and not is_cross:
            continue
        if cross_source is False and is_cross:
            continue
        cand = [c for c in copies if groups_module.passes_playlist_filters(
            c, pl_fast=st["pl_fast"], pl_medium=st["pl_medium"], pl_slow=st["pl_slow"],
            min_avail=st["min_avail"], excluded=st["excluded"])]
        winner = groups_module.pick_winner(cand, st["strategy"]) if cand else None
        display = max({c["name"] for c in copies},
                      key=lambda n: sum(1 for c in copies if c["name"] == n))
        items.append({
            "norm_name": norm,
            "display_name": display,
            "count": len(copies),
            "cross_source": is_cross,
            "alive_copies": sum(1 for c in copies if c.get("is_alive")),
            "winner_id": winner["id"] if winner else None,
            "copies": [_serialize_copy(c, winner["id"] if winner else None) for c in copies],
        })
    items.sort(key=lambda i: (-i["count"], i["display_name"].lower()))
    return {"items": items[offset:offset + limit], "total": len(items),
            "strategy": st["strategy"]}


@app.put("/api/duplicates/keep-best")
async def dup_keep_best(a: DupAction):
    return await _dup_set_winner(a.norm_name, None)


@app.put("/api/duplicates/choose")
async def dup_choose(a: DupAction):
    if not a.channel_id:
        raise HTTPException(400, detail="Need channel_id")
    return await _dup_set_winner(a.norm_name, a.channel_id)


async def _dup_set_winner(norm_name, channel_id):
    channels = db.get_channels()
    copies = [c for c in channels if groups_module.norm_name(c.get("name") or "") == norm_name]
    if len(copies) < 2:
        raise HTTPException(404, detail="No duplicates found")
    if channel_id is None:
        st = _selection_settings()
        cand = [c for c in copies if groups_module.passes_playlist_filters(
            c, pl_fast=st["pl_fast"], pl_medium=st["pl_medium"], pl_slow=st["pl_slow"],
            min_avail=st["min_avail"], excluded=st["excluded"])]
        pool = cand or copies
        winner = groups_module.pick_winner(pool, st["strategy"])
    else:
        winner = next((c for c in copies if c["id"] == channel_id), None)
        if not winner:
            raise HTTPException(404, detail="Channel not in duplicates")
    losers = [c["id"] for c in copies if c["id"] != winner["id"] and c.get("enabled", 1)]
    db.set_channels_enabled(losers, False)
    db.record_dedup_disabled(losers)
    db.set_channels_enabled([winner["id"]], True)
    db.unrecord_dedup_disabled([winner["id"]])
    return {"ok": True, "winner_id": winner["id"], "disabled": len(losers)}


@app.put("/api/duplicates/enable-all")
async def dup_enable_all(a: DupAction):
    channels = db.get_channels()
    ids = [c["id"] for c in channels
           if groups_module.norm_name(c.get("name") or "") == a.norm_name]
    if not ids:
        raise HTTPException(404, detail="No duplicates found")
    db.set_channels_enabled(ids, True)
    db.unrecord_dedup_disabled(ids)
    return {"ok": True, "enabled": len(ids)}


@app.post("/api/duplicates/auto")
async def dup_auto(a: DupAuto):
    channels = db.get_channels()
    st = _selection_settings()
    keys = _duplicate_keys(channels)
    to_disable = set()
    keys_affected = 0
    dead_disabled = 0
    for norm, copies in keys.items():
        cand = [c for c in copies if groups_module.passes_playlist_filters(
            c, pl_fast=st["pl_fast"], pl_medium=st["pl_medium"], pl_slow=st["pl_slow"],
            min_avail=st["min_avail"], excluded=st["excluded"])]
        if not cand:
            continue  # все мёртвые/выключенные - не трогаем
        winner = groups_module.pick_winner(cand, st["strategy"])
        losers = [c["id"] for c in copies
                  if c["id"] != winner["id"] and c.get("enabled", 1)]
        dead = [c["id"] for c in copies
                if c["id"] != winner["id"] and c.get("enabled", 1)
                and c.get("last_check") is not None and not c.get("is_alive")]
        if losers:
            keys_affected += 1
            to_disable.update(losers)
            dead_disabled += len([i for i in losers if i in dead])
    if a.preview:
        return {"ok": True, "keys": keys_affected,
                "will_disable": len(to_disable), "dead": dead_disabled}
    ids = sorted(to_disable)
    db.set_channels_enabled(ids, False)
    db.record_dedup_disabled(ids)
    return {"ok": True, "keys": keys_affected, "disabled": len(ids), "dead": dead_disabled}


@app.post("/api/duplicates/undo-auto")
async def dup_undo_auto():
    ids = sorted(db.get_dedup_disabled())
    if not ids:
        return {"ok": True, "enabled": 0}
    channels = {c["id"] for c in db.get_channels()}
    existing = [i for i in ids if i in channels]
    db.set_channels_enabled(existing, True)
    db.unrecord_dedup_disabled()
    return {"ok": True, "enabled": len(existing)}


@app.post("/api/groups/reset-all")
async def reset_all_overrides():
    return {"ok": True, "affected": _reset_overrides_for_groups(None)}


@app.get("/api/access/stats")
async def access_stats(date_from: str | None = None, date_to: str | None = None):
    return db.get_access_stats(date_from=date_from, date_to=date_to)


@app.get("/api/access/log")
async def access_log(endpoint: str | None = None, limit: int = 10, offset: int = 0, date_from: str | None = None, date_to: str | None = None):
    return db.get_access_log(endpoint=endpoint, limit=limit, offset=offset, date_from=date_from, date_to=date_to)


@app.delete("/api/access/log")
async def delete_access_log(date_from: str | None = None, date_to: str | None = None):
    deleted = db.delete_access_log(date_from=date_from, date_to=date_to)
    return {"ok": True, "deleted": deleted}


@app.post("/api/check")
async def trigger_check():
    global check_in_progress, last_check_time
    if check_in_progress:
        raise HTTPException(409, detail="Check already in progress")
    threading.Thread(target=run_check_background, daemon=True).start()
    return {"ok": True, "message": "Check started"}


@app.get("/api/check/status")
async def check_status():
    return {
        "in_progress": check_in_progress,
        "last_check": last_check_time,
        "next_check": last_check_time + int(db.get_setting("check_interval", "3600")) if last_check_time else 0,
        "progress": check_progress,
    }


PLAYLIST_DIR = os.path.dirname(os.environ.get("PLAYLIST_PATH", "/playlist/rus_fixed.m3u")) or "/playlist"
DEFAULT_PLAYLIST_FILENAME = os.path.basename(os.environ.get("PLAYLIST_PATH", "/playlist/rus_fixed.m3u")) or "rus_fixed.m3u"
PLAYLIST_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}\.m3u8?$")
RESERVED_PLAYLIST_NAMES = {"epg.xml", "login", "static", "api", "p", "favicon.ico", "index.html"}
playlist_channel_count = 0
playlist_group_count = 0
epg_channel_count = 0


def playlist_filename():
    name = (db.get_setting("playlist_filename", DEFAULT_PLAYLIST_FILENAME) or "").strip()
    return name or DEFAULT_PLAYLIST_FILENAME


def playlist_path():
    return os.path.join(PLAYLIST_DIR, playlist_filename())


def valid_playlist_filename(name):
    name = (name or "").strip()
    if not PLAYLIST_NAME_RE.match(name):
        return False
    if name.lower() in RESERVED_PLAYLIST_NAMES:
        return False
    return True


def configured_base_url():
    """Явно заданный публичный адрес: настройка, env PUBLIC_BASE_URL,
    затем схема+хост из env *_SERVE_URL. Пусто, если ничего не задано."""
    base = (db.get_setting("public_base_url", "") or "").strip().rstrip("/")
    if base:
        return base
    env_base = (os.environ.get("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if env_base and re.match(r"^https?://[^/\s]+$", env_base):
        return env_base
    for env_value in (os.environ.get("PLAYLIST_SERVE_URL", ""), os.environ.get("EPG_SERVE_URL", "")):
        m = re.match(r"^(https?://[^/]+)", env_value or "")
        if m:
            return m.group(1)
    return ""


def request_base_url(request):
    """Базовый адрес из текущего запроса (с учётом X-Forwarded-*)."""
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if not proto:
        proto = request.url.scheme or "http"
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("host") or "").strip()
    if not host:
        host = request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def public_base_url():
    """Адрес для генерации файла плейлиста (фоновый процесс без запроса)."""
    return configured_base_url() or "http://localhost:9239"


def normalize_channel_name(name):
    return groups_module.norm_name(name)


def _canon_group(raw_group):
    return groups_module.canonicalize_import(
        raw_group,
        db.get_group_aliases(),
        auto_synonyms=db.get_setting("auto_group_synonyms", "1") == "1",
    )


def generate_playlist_file():
    global playlist_channel_count, playlist_group_count
    pl_fast = db.get_setting("playlist_fast", "1") == "1"
    pl_medium = db.get_setting("playlist_medium", "1") == "1"
    pl_slow = db.get_setting("playlist_slow", "1") == "1"
    min_avail = int(db.get_setting("min_availability", "0"))
    strategy = db.get_setting("dedup_strategy", "availability")
    excluded = set(db.get_excluded_groups().keys())

    channels = db.get_channels()

    # Единая логика отбора: фильтры + один победитель на имя
    # (та же, что показывает вкладка "Дубли")
    unique, _, _ = groups_module.select_playlist_channels(
        channels, pl_fast=pl_fast, pl_medium=pl_medium, pl_slow=pl_slow,
        min_avail=min_avail, excluded=excluded, strategy=strategy,
    )
    alive_channels = unique

    # Умное определение группы: ручные правки -> голосование дублей ->
    # группа источника -> категория EPG (fallback).
    final_groups = groups_module.resolve_output_groups(
        alive_channels,
        overrides=db.get_group_overrides(),
        epg_categories=db.get_epg_categories(),
        aliases=db.get_group_aliases(),
    )

    # Логотип за именем канала, независимо от победителя дедупликации:
    # приоритет источника (меньший source_id), затем меньший id канала;
    # если ни у одной копии логотипа нет - иконка из EPG.
    def _extract_logo(inf_line):
        m = re.search(r'tvg-logo="([^"]*)"', inf_line or "")
        return m.group(1) if m and m.group(1) else None

    logo_best = {}
    for c in channels:
        norm = normalize_channel_name(c.get("name"))
        if not norm:
            continue
        logo = _extract_logo(c.get("inf_line"))
        if not logo:
            continue
        key = (c.get("source_id") or 0, c.get("id") or 0)
        cur = logo_best.get(norm)
        if cur is None or key < cur[0]:
            logo_best[norm] = (key, logo)
    logo_by_norm = {k: v[1] for k, v in logo_best.items()}
    epg_icons = db.get_epg_icons()
    logos_added = 0
    logos_replaced = 0

    base_url = public_base_url()
    epg_url = f"{base_url}/epg.xml"
    lines = [f'#EXTM3U x-tvg-url="{epg_url}" url-tvg="{epg_url}"\n']
    snapshot = {}
    for ch in unique:
        norm = normalize_channel_name(ch["name"])
        group = final_groups.get(norm) or ch["group_title"] or "Другое"
        snapshot[norm] = group
        inf = ch["inf_line"]
        if inf:
            inf = re.sub(r'group-title="[^"]*"', f'group-title="{group}"', inf)
            if 'group-title' not in inf:
                inf = inf.replace("#EXTINF:", f'#EXTINF: group-title="{group}" ', 1)
        else:
            inf = f'#EXTINF:-1 group-title="{group}",{ch["name"]}'
        logo = logo_by_norm.get(norm) or epg_icons.get(norm)
        if logo:
            safe_logo = logo.replace('"', '%22')
            if 'tvg-logo=' in inf:
                new_inf = re.sub(r'tvg-logo="[^"]*"', f'tvg-logo="{safe_logo}"', inf, count=1)
                if new_inf != inf:
                    inf = new_inf
                    logos_replaced += 1
            else:
                idx = inf.rfind(',')
                if idx != -1:
                    inf = inf[:idx] + f' tvg-logo="{safe_logo}"' + inf[idx:]
                    logos_added += 1
        inf = re.sub(r',[^,]*$', f',{ch["name"]}', inf)
        lines.append(f'{inf}\n{ch["url"]}\n')

    content = "".join(lines)
    out_path = playlist_path()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    db.save_playlist_groups(snapshot)
    playlist_channel_count = len(unique)
    playlist_group_count = len(set(snapshot.values()))
    print(f"[scheduler] Playlist saved: {playlist_channel_count} channels, "
          f"{playlist_group_count} groups, "
          f"logos: +{logos_added} added, {logos_replaced} replaced -> {out_path}")
    return content


@app.get("/api/playlist/info")
async def playlist_info(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(401, detail="Unauthorized")
    filename = playlist_filename()
    base = configured_base_url() or request_base_url(request)
    return {
        "url": playlist_path(),
        "filename": filename,
        "public_url": f"{base}/{filename}",
        "channel_count": playlist_channel_count,
        "group_count": playlist_group_count,
        "epg_channel_count": epg_channel_count,
        "last_check": last_check_time,
        "last_epg_update": last_epg_update_time,
    }


@app.post("/api/playlist/generate")
async def regenerate_playlist(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(401, detail="Unauthorized")
    generate_playlist_file()
    return {
        "ok": True,
        "channel_count": playlist_channel_count,
    }


@app.get("/api/playlist")
async def generate_playlist(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(401, detail="Unauthorized")

    generate_playlist_file()
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    db.log_access("playlist", client_ip, user_agent)
    return FileResponse(playlist_path(), media_type="audio/x-mpegurl",
                        filename=playlist_filename())


# ---------------------------------------------------------------------------
# ВНИМАНИЕ: ниже catch-all маршруты плейлиста.
# Новые маршруты добавлять ТОЛЬКО выше этого блока, иначе они будут
# перехвачены шаблоном /{fname}.
# ---------------------------------------------------------------------------

async def _epg_response(request: Request, media_type="application/xml"):
    """Потоковая отдача EPG: готовая .gz для клиентов с Accept-Encoding: gzip,
    иначе сырой XML. Сжатие на лету не используется."""
    accepts_gzip = "gzip" in (request.headers.get("accept-encoding") or "").lower()
    gz_path = epg_module.EPG_PATH + ".gz"
    if accepts_gzip:
        if not os.path.exists(gz_path) or os.path.getmtime(gz_path) < os.path.getmtime(epg_module.EPG_PATH):
            await run_in_threadpool(epg_module.ensure_epg_gzip)
        if os.path.exists(gz_path):
            st = os.stat(gz_path)
            etag = f'"{int(st.st_mtime)}-{st.st_size}"'
            headers = {"ETag": etag, "Cache-Control": "no-cache", "Content-Encoding": "gzip"}
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
            return FileResponse(gz_path, media_type=media_type, headers=headers)
    st = os.stat(epg_module.EPG_PATH)
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return FileResponse(epg_module.EPG_PATH, media_type=media_type,
                        headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/epg.xml")
async def serve_epg_public(request: Request):
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    db.log_access("epg", client_ip, user_agent)
    if not os.path.exists(epg_module.EPG_PATH):
        return Response(content='<?xml version="1.0" encoding="UTF-8"?><tv/>',
                        media_type="application/xml")
    return await _epg_response(request)


@app.get("/epg.xml.gz")
async def serve_epg_gzip(request: Request):
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    db.log_access("epg", client_ip, user_agent)
    gz_path = epg_module.EPG_PATH + ".gz"
    if (not os.path.exists(gz_path) or os.path.getmtime(gz_path) < os.path.getmtime(epg_module.EPG_PATH)) and os.path.exists(epg_module.EPG_PATH):
        await run_in_threadpool(epg_module.ensure_epg_gzip)
    if not os.path.exists(gz_path):
        return Response(content="", status_code=404)
    st = os.stat(gz_path)
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return FileResponse(gz_path, media_type="application/gzip",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/p/{fname}")
@app.get("/{fname}")
async def serve_playlist_public(request: Request, fname: str):
    # Отдаём только текущее настроенное имя; старые имена недоступны.
    if not valid_playlist_filename(fname) or fname != playlist_filename():
        return Response(content="", status_code=404)
    client_ip = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    user_agent = request.headers.get("user-agent", "unknown")
    db.log_access("playlist", client_ip, user_agent)
    path = playlist_path()
    if not os.path.exists(path):
        return Response(content="", status_code=404)
    st = os.stat(path)
    etag = f'"{int(st.st_mtime)}-{st.st_size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return FileResponse(path, media_type="audio/x-mpegurl",
                        headers={"ETag": etag, "Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9239)
