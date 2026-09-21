# TANgle - IPTV Checker

Smart web interface for IPTV playlist management.
TANgle automatically builds a single M3U playlist from multiple sources, checks channel availability and response time, removes duplicates, and generates an up-to-date TV Guide (EPG) for active channels only.

Developed with [MiMo Code](https://github.com/XiaoMi/MiMo) — AI coding assistant by Xiaomi.

[Русская версия](README.md)

## Features

- **IPTV Channel Checking** — parallel availability testing from M3U/M3U8 playlists with response time measurement.
- **Unified Playlist** — merge multiple playlists; configurable playlist filename and public server URL for links in the playlist header.
- **Smart Deduplication** — one channel per name; winner is picked by availability then speed (or "fastest" mode), with a Duplicates tab: copies, winner selection, auto-trim of losers and dead copies.
- **Group Management** — automatic group canonicalization (EN→RU), merge suggestions with confirmation, rename, merge, undo merge, bulk operations and excluding groups from the playlist.
- **Channel Management** — enable/disable channels, search, filters (alive/dead/duplicates/disabled/by group and source), sorting, chunked rendering with infinite scroll.
- **Playlist Sources: URL and File** — upload an `.m3u` file from the browser (up to 20 MB, UTF-8/Windows-1251), instant import and file replacement.
- **Channel Logos** — taken from sources; missing ones filled from EPG icons; deterministic choice (source priority) independent of the dedup winner.
- **TV Guide (EPG)** — XMLTV download and merge; rebuild only on changes or schedule, streaming delivery of `epg.xml` and pre-built `epg.xml.gz` (zero CPU for gzip-aware clients).
- **Connection Statistics** — tracking IPs, devices, access history with period filtering and deletion.
- **Responsive Web UI** — works on desktop, tablets, and smartphones.
- **7 Themes** — Dark, Light, Monokai, Dracula, Nord, Solarized, GitHub.
- **Multilingual** — instant EN/RU language switching.
- **Automation** — built-in scheduler for periodic playlist checks and EPG updates.
- **Easy Deployment** — Docker image, one command to run.

## Screenshots
<img width="1228" height="914" alt="s1" src="https://github.com/user-attachments/assets/4a0d0bf2-2e30-4b7c-93ca-bc893e75cab2" />
<img width="1217" height="569" alt="s2" src="https://github.com/user-attachments/assets/bbe46017-ed14-4971-a83a-d5b33bd933cc" />
<img width="1211" height="653" alt="s3" src="https://github.com/user-attachments/assets/afbd1dc9-ab97-48ce-b190-e528a14edebc" />

## Quick Start

```bash
git clone https://github.com/tanweber/TANgle-iptv-checker.git
cd TANgle-iptv-checker
docker compose up -d
```

Open in browser: `http://localhost:9239`

Default credentials: `admin` / `admin`

## Project Structure

```
tangle/
├── app.py              # FastAPI server
├── database.py         # SQLite operations
├── checker_core.py     # Channel checker
├── groups.py           # Smart group merging and editing
├── epg.py              # EPG processor
├── static/
│   ├── index.html      # Main UI
│   ├── login.html      # Login page
│   └── translations.js # EN/RU translations
├── Dockerfile
├── docker-compose.yml
└── nginx.conf
```

## API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Web UI |
| GET | `/<name>.m3u` | Download playlist (filename is configurable in settings) |
| GET | `/p/<name>.m3u` | Same, for reverse proxy (used by nginx.conf) |
| GET | `/epg.xml` | Download TV Guide (pre-built `.gz` for gzip-aware clients) |
| GET | `/epg.xml.gz` | Download compressed TV Guide |
| POST | `/api/login` | Login |
| GET | `/api/stats` | Channel statistics |
| GET | `/api/channels` | Channel list (with duplicate flags) |
| PUT | `/api/channels/{id}/toggle` | Toggle channel |
| PUT | `/api/channels/{id}/group` | Change group (applies to all duplicates of the name) |
| DELETE | `/api/channels/{id}/override` | Reset manual group override |
| GET | `/api/channels/{id}/duplicates` | Channel duplicates |
| PUT | `/api/channels/bulk-group` | Bulk group assignment |
| GET | `/api/sources` | Playlist sources |
| POST | `/api/sources` | Add source by URL |
| POST | `/api/sources/upload` | Upload playlist file (.m3u/.m3u8) |
| POST | `/api/sources/{id}/file` | Replace source file |
| GET | `/api/groups` | Groups, merges, counters |
| GET | `/api/groups/suggestions` | Merge suggestions |
| POST | `/api/groups/merge` | Merge groups |
| PUT | `/api/groups/rename` | Rename group |
| POST | `/api/groups/unmerge` | Undo merge |
| PUT | `/api/groups/exclude` | Exclude/restore group |
| PUT | `/api/groups/exclude-bulk` | Bulk exclude/restore groups |
| POST | `/api/groups/reset`, `/api/groups/reset-bulk`, `/api/groups/reset-all` | Reset manual group edits |
| GET | `/api/duplicates` | Duplicates by channel name (paged) |
| PUT | `/api/duplicates/choose`, `/keep-best`, `/enable-all` | Winner management |
| POST | `/api/duplicates/auto`, `/undo-auto` | Auto-trim losers and undo |
| GET | `/api/epg-sources` | EPG sources |
| POST | `/api/epg/rebuild` | Rebuild EPG (background) |
| GET | `/api/settings` | Settings |
| POST | `/api/check` | Trigger check |
| GET | `/api/access/log` | Access log |
| DELETE | `/api/access/log` | Delete log by period |

## Settings

- Channel check interval, timeout and worker count
- EPG update interval and availability calculation period
- Playlist rules (by speed and availability)
- Duplicate winner rule (availability → speed, or fastest)
- Automatic merging of similar groups (synonyms on import)
- Playlist filename and public server URL
- Playlist sources (URL or file) and EPG sources
- Authorization

## Requirements

- Docker and Docker Compose
- 768 MB RAM (container limit in `docker-compose.yml`), 1 CPU
- 1+ GB disk space (EPG cache may take more with many sources)

## License

MIT
