"""Watering schedule maths and the background event engine.

interval = researched base days × pot × light × water preference × season × learned adjustment
"""
import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta

from . import db, notify, photos

log = logging.getLogger("watering")
CHECK_SECONDS = int(os.environ.get("EVENT_CHECK_SECONDS", "900"))
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "fall", 10: "fall", 11: "fall"}
FLIP = {"winter": "summer", "summer": "winter", "spring": "fall", "fall": "spring"}


def option(items: list, key) -> tuple[float, str]:
    for it in items:
        if it["key"] == key:
            return float(it["factor"]), it["label"]
    return 1.0, "not set"


def season(d: date, hemisphere: str) -> str:
    s = SEASONS[d.month]
    return FLIP[s] if hemisphere == "south" else s


def _date(s) -> date | None:
    try:
        return date.fromisoformat(str(s)[:10]) if s else None
    except ValueError:
        return None


def schedule(p: dict, care: dict, settings: dict, last: datetime | None, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    try:
        base = float(care.get("watering_days") or 0)
    except (TypeError, ValueError):
        base = 0
    base_src = "researched"
    if base <= 0:
        base, base_src = float(settings.get("default_watering_days", 7)), "default"

    seas = settings.get("seasons", {})
    sname = season(now.date(), seas.get("hemisphere", "north"))
    parts = [
        ("Pot", *option(settings["pot_types"], p.get("pot_type"))),
        ("Light", *option(settings["light_levels"], p.get("light_level"))),
        ("Water", *option(settings["water_prefs"], p.get("water_pref"))),
        ("Season", float(seas.get(sname, 1.0)), sname.capitalize()),
        ("Learned", float(p.get("watering_adjust") or 1.0), "from your feedback"),
    ]
    raw = base
    for _, f, _ in parts:
        raw *= f
    interval = max(1, round(raw))

    anchor = last.date() if last else (_date(p.get("created_at")) or now.date())
    due = datetime.combine(anchor + timedelta(days=interval), time(int(settings.get("reminder_hour", 9))))
    if not last:
        due = min(due, datetime.combine(now.date(), time(int(settings.get("reminder_hour", 9)))))
    snooze = p.get("snooze_until")
    if snooze and datetime.fromisoformat(snooze) > due:
        due = datetime.fromisoformat(snooze)

    days_left = (due.date() - now.date()).days
    if days_left < 0:
        status, label = "overdue", f"{-days_left}d overdue"
    elif days_left == 0:
        status, label = "today", "Water today"
    elif days_left <= 2:
        status, label = "soon", f"Water in {days_left}d"
    else:
        status, label = "ok", f"Water in {days_left}d"
    if not last:
        label = "Never watered" if days_left <= 0 else label
    return {"interval": interval, "base": base, "base_src": base_src, "parts": parts, "due": due,
            "days_left": days_left, "status": status, "label": label, "last": last}


def apply_action(pid: int, kind: str, when: datetime | None = None, note: str | None = None) -> None:
    """kind: watered | dry (watered, it was already bone dry) | wet (checked, still wet) | fertilized | repotted"""
    s = db.get_settings()
    when = (when or datetime.now()).replace(microsecond=0)
    with db.tx() as c:
        p = db.get_plant(c, pid)
        if not p:
            return
        adj = float(p["watering_adjust"] or 1.0)
        if kind in ("watered", "dry"):
            c.execute("INSERT INTO care_log(plant_id, kind, at, note) VALUES (?,?,?,?)",
                      (pid, "watered", when.isoformat(), note or ("was already bone dry" if kind == "dry" else None)))
            if kind == "dry":
                adj = max(0.4, adj * 0.92)
            c.execute("UPDATE plants SET snooze_until=NULL, watering_adjust=? WHERE id=?", (round(adj, 3), pid))
            resolve(c, pid)
        elif kind == "wet":
            days = int(s.get("still_wet_snooze_days", 2))
            snooze = datetime.combine(when.date() + timedelta(days=days), time(int(s.get("reminder_hour", 9))))
            c.execute("INSERT INTO care_log(plant_id, kind, at, note) VALUES (?,?,?,?)",
                      (pid, "still_wet", when.isoformat(), note))
            c.execute("UPDATE plants SET snooze_until=?, watering_adjust=? WHERE id=?",
                      (snooze.isoformat(), round(min(2.5, adj * 1.08), 3), pid))
            resolve(c, pid)
        elif kind in ("fertilized", "repotted", "note"):
            c.execute("INSERT INTO care_log(plant_id, kind, at, note) VALUES (?,?,?,?)",
                      (pid, kind, when.isoformat(), note))


def resolve(c, pid: int) -> None:
    c.execute("UPDATE events SET status='resolved' WHERE plant_id=? AND status IN ('pending','sent')", (pid,))


def check_once(now: datetime | None = None) -> int:
    """Create water_due / water_overdue events for plants that need them. Returns number created."""
    now = now or datetime.now()
    s = db.get_settings()
    overdue_after = int(s.get("overdue_after_days", 2))
    created = 0
    with db.tx() as c:
        for row in c.execute("SELECT * FROM plants WHERE archived=0").fetchall():
            p = dict(row)
            sch = schedule(p, db.care_profile(c, p["id"])["data"], s, db.last_watered(c, p["id"]), now)
            wanted = []
            if now >= sch["due"]:
                wanted.append(("water_due", f"{p['name']} needs water"))
            if now >= sch["due"] + timedelta(days=overdue_after):
                wanted.append(("water_overdue", f"{p['name']} is {-sch['days_left']} days overdue for water"))
            for kind, msg in wanted:
                open_ = c.execute("SELECT 1 FROM events WHERE plant_id=? AND kind=? AND status IN ('pending','sent')",
                                  (p["id"], kind)).fetchone()
                if not open_:
                    c.execute("INSERT INTO events(plant_id, kind, due_at, created_at, message) VALUES (?,?,?,?,?)",
                              (p["id"], kind, sch["due"].isoformat(), db.now_iso(), msg))
                    created += 1
    return created


async def loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            n = await asyncio.to_thread(check_once)
            if n:
                log.info("created %d event(s)", n)
            await notify.flush()
            await asyncio.to_thread(photos.clean_stash)
        except Exception:
            log.exception("event check failed")
        await asyncio.sleep(CHECK_SECONDS)
