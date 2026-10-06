"""SQLite storage. One file at $DATA_DIR/plants.db; photos live next to it on disk."""
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PHOTO_DIR = os.path.join(DATA_DIR, "photos")
TMP_DIR = os.path.join(DATA_DIR, "tmp")
DB_PATH = os.path.join(DATA_DIR, "plants.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plants (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  species TEXT,
  location TEXT,
  acquired_on TEXT,
  age_at_acquisition_months INTEGER DEFAULT 0,
  pot_type TEXT,
  light_level TEXT,
  water_pref TEXT,
  notes TEXT,
  cover_photo_id INTEGER,
  watering_adjust REAL DEFAULT 1.0,
  snooze_until TEXT,
  research_status TEXT DEFAULT 'pending',
  created_at TEXT NOT NULL,
  archived INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS care_profiles (
  plant_id INTEGER PRIMARY KEY REFERENCES plants(id) ON DELETE CASCADE,
  data TEXT NOT NULL DEFAULT '{}',
  sources TEXT NOT NULL DEFAULT '{}',
  overrides TEXT NOT NULL DEFAULT '{}',
  providers_log TEXT NOT NULL DEFAULT '[]',
  researched_at TEXT
);
CREATE TABLE IF NOT EXISTS photos (
  id INTEGER PRIMARY KEY,
  plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
  filename TEXT NOT NULL,
  thumb TEXT NOT NULL,
  taken_at TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS photos_plant ON photos(plant_id, taken_at);
CREATE TABLE IF NOT EXISTS care_log (
  id INTEGER PRIMARY KEY,
  plant_id INTEGER NOT NULL REFERENCES plants(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  at TEXT NOT NULL,
  note TEXT
);
CREATE INDEX IF NOT EXISTS care_log_plant ON care_log(plant_id, kind, at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  plant_id INTEGER REFERENCES plants(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,
  due_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  message TEXT,
  notified_at TEXT
);
CREATE INDEX IF NOT EXISTS events_status ON events(status, plant_id);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw_hash TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "pot_types": [
        {"key": "plastic", "label": "Plastic / nursery pot", "factor": 1.0},
        {"key": "terracotta", "label": "Terracotta / unglazed clay", "factor": 0.8},
        {"key": "ceramic", "label": "Glazed ceramic", "factor": 1.05},
        {"key": "self_watering", "label": "Self-watering", "factor": 1.6},
        {"key": "fabric", "label": "Fabric grow bag", "factor": 0.75},
        {"key": "no_drainage", "label": "No drainage / cachepot", "factor": 1.25},
        {"key": "semi_hydro", "label": "Semi-hydro (LECA / pon)", "factor": 1.4},
    ],
    "light_levels": [
        {"key": "low", "label": "Low (north window / interior)", "factor": 1.4},
        {"key": "medium", "label": "Medium indirect", "factor": 1.0},
        {"key": "bright_indirect", "label": "Bright indirect", "factor": 0.9},
        {"key": "direct", "label": "Direct sun", "factor": 0.75},
        {"key": "grow_light", "label": "Grow light", "factor": 0.85},
    ],
    "water_prefs": [
        {"key": "auto", "label": "Use researched needs", "factor": 1.0},
        {"key": "keep_moist", "label": "Keep evenly moist", "factor": 0.7},
        {"key": "top_inch_dry", "label": "Water when top inch is dry", "factor": 1.0},
        {"key": "dry_out", "label": "Let dry out fully (succulent-style)", "factor": 1.6},
    ],
    "seasons": {"hemisphere": "north", "winter": 1.35, "spring": 1.0, "summer": 0.85, "fall": 1.1},
    "default_watering_days": 7,
    "reminder_hour": 9,
    "overdue_after_days": 2,
    "still_wet_snooze_days": 2,
    # Integrations (blank = fall back to the matching environment variable)
    "perenual_key": "",
    "trefle_token": "",
    "permapeople_key_id": "",
    "permapeople_key_secret": "",
    "plantnet_key": "",
    "ollama_url": "",
    "ollama_model": "",
    "ollama_vision_model": "",
    "ntfy_url": "",
    "ntfy_token": "",
    "base_url": "",
}


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    os.makedirs(PHOTO_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)
    with tx() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, json.dumps(v)))
        for k in ("session_secret", "api_token"):
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
                      (k, json.dumps(secrets.token_urlsafe(32))))
        # A research run interrupted by a restart should not stay "running" forever.
        c.execute("UPDATE plants SET research_status='error' WHERE research_status='running'")


def get_settings() -> dict:
    with tx() as c:
        return {r["key"]: json.loads(r["value"]) for r in c.execute("SELECT key, value FROM settings")}


def set_setting(key: str, value) -> None:
    with tx() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))


def cfg(settings: dict, key: str, env: str) -> str:
    """A setting from the UI, falling back to an environment variable."""
    return (settings.get(key) or os.environ.get(env) or "").strip()


# ---- plant helpers -------------------------------------------------------

def get_plant(c, pid: int) -> dict | None:
    r = c.execute("SELECT * FROM plants WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def care_profile(c, pid: int) -> dict:
    """Returns {data (merged + overrides), sources, researched, overrides, log, researched_at}."""
    r = c.execute("SELECT * FROM care_profiles WHERE plant_id=?", (pid,)).fetchone()
    if not r:
        return {"data": {}, "sources": {}, "researched": {}, "overrides": {}, "log": [], "researched_at": None}
    researched = json.loads(r["data"])
    overrides = json.loads(r["overrides"])
    sources = json.loads(r["sources"])
    data = dict(researched)
    for k, v in overrides.items():
        data[k] = v
        sources[k] = "You"
    return {"data": data, "sources": sources, "researched": researched, "overrides": overrides,
            "log": json.loads(r["providers_log"]), "researched_at": r["researched_at"]}


def last_watered(c, pid: int) -> datetime | None:
    r = c.execute("SELECT MAX(at) AS at FROM care_log WHERE plant_id=? AND kind='watered'", (pid,)).fetchone()
    return datetime.fromisoformat(r["at"]) if r and r["at"] else None


def cover_url(c, p: dict) -> str | None:
    row = None
    if p.get("cover_photo_id"):
        row = c.execute("SELECT thumb FROM photos WHERE id=? AND plant_id=?", (p["cover_photo_id"], p["id"])).fetchone()
    if not row:
        row = c.execute("SELECT thumb FROM photos WHERE plant_id=? ORDER BY taken_at DESC LIMIT 1", (p["id"],)).fetchone()
    return f"/media/{row['thumb']}" if row else None


def has_user() -> bool:
    with tx() as c:
        return c.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
