"""Optional push of pending events to ntfy. Disabled until an ntfy topic URL is set."""
import logging
from urllib.parse import urlsplit

import httpx

from . import db

log = logging.getLogger("notify")


def _target(settings):
    url = db.cfg(settings, "ntfy_url", "NTFY_URL")
    if not url:
        return None
    parts = urlsplit(url.rstrip("/"))
    server, _, topic = f"{parts.scheme}://{parts.netloc}{parts.path}".rpartition("/")
    return (server, topic) if topic else None


async def send(client, settings, title: str, message: str, plant_id: int | None = None, high=False) -> None:
    server, topic = _target(settings)
    base = db.cfg(settings, "base_url", "BASE_URL").rstrip("/")
    body = {"topic": topic, "title": title, "message": message,
            "tags": ["potted_plant", "droplet"], "priority": 4 if high else 3}
    if base and plant_id:
        body["click"] = f"{base}/plants/{plant_id}"
        body["actions"] = [{
            "action": "http", "label": "Watered", "url": f"{base}/api/plants/{plant_id}/water",
            "method": "POST", "headers": {"Authorization": f"Bearer {settings['api_token']}"}, "clear": True,
        }]
    headers = {}
    tok = db.cfg(settings, "ntfy_token", "NTFY_TOKEN")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = await client.post(server, json=body, headers=headers)
    r.raise_for_status()


async def flush() -> None:
    s = db.get_settings()
    if not _target(s):
        return
    with db.tx() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT e.*, p.name FROM events e JOIN plants p ON p.id=e.plant_id WHERE e.status='pending'")]
    async with httpx.AsyncClient(timeout=15) as client:
        for e in rows:
            try:
                await send(client, s, "🌿 " + e["name"], e["message"], e["plant_id"], e["kind"] == "water_overdue")
            except Exception as ex:
                log.warning("ntfy send failed: %s", ex)
                return
            with db.tx() as c:
                c.execute("UPDATE events SET status='sent', notified_at=? WHERE id=?", (db.now_iso(), e["id"]))


async def test() -> str:
    s = db.get_settings()
    if not _target(s):
        return "Set an ntfy topic URL first."
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            await send(client, s, "🌿 Plant Tracker", "Test notification — it works!")
            return "Sent! Check your ntfy app."
        except Exception as e:
            return f"Failed: {e}"
