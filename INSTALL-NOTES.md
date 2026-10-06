# Handoff notes (for the next Claude session)

The owner built this app with Claude on another PC and moved it here to install. They **don't want to write code; they want to use it.** Do the setup for them.

## Install

1. Docker and Docker Compose are required. Check with `docker --version` and `docker compose version`.
2. In this folder, set `TZ` in `docker-compose.yml` to the owner's timezone (ask if unknown).
3. Run `docker compose up -d --build`, then open `http://<host>:8080`.
4. The first visit shows **/setup** to create the single login. The **owner** creates it; don't pick their password.
5. Data is stored in `./data` (SQLite `plants.db` + `photos/`). Tell them to back it up.

## Decisions already made (don't re-ask)

- Stack: Python 3.12 / FastAPI / Jinja + HTMX / SQLite, in one container with one worker. The reminder loop runs in-process.
- Auth: the app's own login (one user, pbkdf2), plus an API bearer token for ntfy and scripts.
- Research order: iNaturalist → GBIF → Perenual → Trefle → Permapeople → Wikipedia, then **local Ollama fills only the blank fields**. OpenFarm is dead, so it was dropped.
- Photo ID: Pl@ntNet first, then an Ollama vision model if Pl@ntNet is unset, fails, or its top score is under 20%.
- Pot type, light and watering style are **global lists with multipliers** in Settings, and each plant picks one of each. Interval = base × pot × light × style × season × learned.
- Reminders: for now the app only records **events** (`/events` page, `GET /api/events`). ntfy push is built in but stays off until `NTFY_URL` is set. The owner plans to wire up ntfy later.

## Status as of 2026-10-05

- **Tested end to end in Docker:** setup/login, adding a plant, research with the no-key sources, photo upload (EXIF date, thumbnails), timeline and compare slider, watering maths, water/dry/wet actions, event creation and resolution, API token auth, identify → "add as new plant" with the photo carried over, and the mobile layout.
- **Ollama paths** (text gap-fill and vision ID) were tested only against a *fake* Ollama server. Not yet tested against a real one. Defaults are `llama3.1:8b` and `qwen2.5vl:7b`; set them to whatever models the owner has pulled.
- **Untested (no keys):** Perenual, Trefle, Permapeople, Pl@ntNet. The code follows their documented APIs, and all four endpoints answered (asking for auth) on 2026-10-05. Once the owner adds keys (Settings, or env vars in compose), run **Settings → Test research sources** and fix any field-mapping issues in `app/research.py` / `app/identify.py`.
- Without Perenual, Trefle, Permapeople or Ollama, the watering baseline falls back to the default of 7 days. Recommend at least a Perenual key or Ollama.

## Possible next steps the owner mentioned or may want

- Hook up ntfy (set `NTFY_URL` and `BASE_URL`, then use Settings → "Send test notification").
- Put it behind their reverse proxy for HTTPS if it will be reached off-LAN.
