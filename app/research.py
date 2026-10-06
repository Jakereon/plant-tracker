"""Plant care research.

Every live source is queried in order; for each care field the first source that
has a value wins. Whatever is still blank afterwards is filled by a local Ollama
model, which is given everything the databases found as context.
"""
import asyncio
import json
import logging
import re
from urllib.parse import quote

import httpx

from . import db

log = logging.getLogger("research")
UA = {"User-Agent": "PlantTracker/1.0 (self-hosted homelab app)"}

FIELDS = [
    ("common_name", "Common name"),
    ("scientific_name", "Scientific name"),
    ("family", "Family"),
    ("description", "About"),
    ("watering_days", "Base watering interval (days)"),
    ("watering", "Watering"),
    ("light", "Light"),
    ("humidity", "Humidity"),
    ("temperature", "Temperature"),
    ("soil", "Soil"),
    ("fertilizer", "Fertilizer"),
    ("repotting", "Repotting"),
    ("toxicity", "Toxicity"),
    ("growth_rate", "Growth rate"),
    ("mature_size", "Mature size"),
    ("care_level", "Care difficulty"),
    ("propagation", "Propagation"),
    ("pruning", "Pruning"),
    ("pests", "Pests & problems"),
    ("hardiness", "Hardiness"),
    ("native_range", "Native range"),
    ("cycle", "Life cycle"),
    ("cultivation", "Cultivation notes"),
    ("image_url", "Reference image"),
    ("wikipedia_url", "Wikipedia"),
]
FIELD_KEYS = [k for k, _ in FIELDS]
LABELS = dict(FIELDS)

# What the LLM is told each field means.
HINTS = {
    "common_name": "most widely used English common name",
    "scientific_name": "binomial scientific name",
    "family": "botanical family",
    "description": "2-3 sentence overview of the plant",
    "watering_days": "typical number of days between waterings for this plant indoors, in a standard plastic pot with drainage, in spring/summer (integer)",
    "watering": "how and when to water, signs of over/under-watering",
    "light": "light requirements",
    "humidity": "humidity preferences",
    "temperature": "ideal and minimum temperature range in °C and °F",
    "soil": "recommended soil mix and pH",
    "fertilizer": "what fertilizer to use, how often and when",
    "repotting": "how often to repot and signs it needs it",
    "toxicity": "toxicity to cats, dogs and humans",
    "growth_rate": "slow / moderate / fast",
    "mature_size": "typical mature height and spread indoors",
    "care_level": "easy / moderate / difficult, with a short reason",
    "propagation": "propagation methods",
    "pruning": "pruning advice",
    "pests": "common pests, diseases and problems with fixes",
    "hardiness": "USDA hardiness zones",
    "native_range": "native region",
    "cycle": "perennial / annual / biennial",
    "cultivation": "other practical growing tips",
}
LLM_FIELDS = [k for k in FIELD_KEYS if k in HINTS]


class Skip(Exception):
    """Provider not configured."""


class Ctx:
    def __init__(self, query: str):
        self.query = query
        self.sci: str | None = None
        self.wiki_title: str | None = None
        self.wiki_text = ""

    @property
    def name(self) -> str:
        return self.sci or self.query


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, (list, tuple)):
        return ", ".join(t for t in (_txt(x) for x in v) if t)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {t}" for k, t in ((k, _txt(x)) for k, x in v.items()) if t)
    s = str(v).strip()
    # Perenual's free tier replaces premium fields with an upsell string.
    if "upgrade plan" in s.lower() or "subscription-api-pricing" in s:
        return ""
    return s


def _normalize(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in FIELD_KEYS:
            continue
        if k == "watering_days":
            try:
                n = int(round(float(v)))
            except (TypeError, ValueError):
                continue
            if 1 <= n <= 60:
                out[k] = n
            continue
        t = _txt(v)
        if t:
            out[k] = t
    return out


def _pick(items: list, name: str, key) -> dict:
    """Prefer the result whose scientific name matches ours."""
    n = name.lower()
    for it in items:
        sn = key(it)
        sn = " ".join(sn) if isinstance(sn, list) else (sn or "")
        if sn.lower().startswith(n) or n.startswith(sn.lower() or "\0"):
            return it
    return items[0]


def _days_from_text(text: str) -> int | None:
    t = (text or "").lower()
    table = [("wet", 3), ("frequent", 4), ("moist", 5), ("average", 7), ("moderate", 7), ("medium", 7),
             ("minimum", 14), ("dry", 14), ("none", 21)]
    hits = [d for w, d in table if w in t]
    return round(sum(hits) / len(hits)) if hits else None


# ---- providers -----------------------------------------------------------

async def inaturalist(client, ctx, cfg):
    r = await client.get("https://api.inaturalist.org/v1/taxa",
                         params={"q": ctx.query, "per_page": 10, "is_active": "true"})
    r.raise_for_status()
    res = [t for t in r.json().get("results", []) if t.get("iconic_taxon_name") == "Plantae"]
    if not res:
        return {}
    t = res[0]
    ctx.sci = t.get("name")
    if t.get("wikipedia_url"):
        ctx.wiki_title = t["wikipedia_url"].rstrip("/").rsplit("/", 1)[-1]
    common = t.get("preferred_common_name") or ""
    return {
        "scientific_name": t.get("name"),
        "common_name": common[:1].upper() + common[1:],
        "image_url": (t.get("default_photo") or {}).get("medium_url"),
        "wikipedia_url": t.get("wikipedia_url"),
    }


async def gbif(client, ctx, cfg):
    r = await client.get("https://api.gbif.org/v1/species/match", params={"name": ctx.name, "kingdom": "Plantae"})
    r.raise_for_status()
    j = r.json()
    if j.get("matchType") in (None, "NONE") or j.get("kingdom") != "Plantae":
        return {}
    if not ctx.sci and j.get("canonicalName"):
        ctx.sci = j["canonicalName"]
    return {"scientific_name": j.get("canonicalName"), "family": j.get("family")}


async def perenual(client, ctx, cfg):
    key = cfg("perenual_key", "PERENUAL_API_KEY")
    if not key:
        raise Skip("no API key")
    r = await client.get("https://perenual.com/api/v2/species-list", params={"key": key, "q": ctx.name})
    r.raise_for_status()
    items = r.json().get("data") or []
    if not items:
        return {}
    best = _pick(items, ctx.name, lambda it: it.get("scientific_name"))
    sid = best["id"]
    r = await client.get(f"https://perenual.com/api/v2/species/details/{sid}", params={"key": key})
    r.raise_for_status()
    d = r.json()

    bench = d.get("watering_general_benchmark") or {}
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", _txt(bench.get("value")))]
    days = None
    if nums:
        days = sum(nums) / len(nums) * (7 if _txt(bench.get("unit")).startswith("week") else 1)
    watering = _txt(d.get("watering"))
    if not days:
        days = _days_from_text(watering)

    tox = []
    for who, k in (("pets", "poisonous_to_pets"), ("humans", "poisonous_to_humans")):
        v = d.get(k)
        if v in (0, 1, True, False):
            tox.append(f"{'Toxic' if v else 'Non-toxic'} to {who}")
    hard = d.get("hardiness") or {}
    dims = d.get("dimensions")
    dims = dims if isinstance(dims, list) else [dims] if dims else []
    size = "; ".join(
        f"{x.get('type') or 'Size'}: {x.get('min_value')}–{x.get('max_value')} {x.get('unit') or ''}".strip()
        for x in dims if isinstance(x, dict) and x.get("max_value"))

    fields = {
        "common_name": d.get("common_name"),
        "family": d.get("family"),
        "description": d.get("description"),
        "watering_days": days,
        "watering": f"{watering} (about every {_txt(bench.get('value')).strip(chr(34))} {_txt(bench.get('unit'))})"
                    if watering and bench.get("value") else watering,
        "light": _txt(d.get("sunlight")),
        "soil": _txt(d.get("soil")),
        "toxicity": "; ".join(tox),
        "growth_rate": d.get("growth_rate"),
        "care_level": d.get("care_level"),
        "propagation": _txt(d.get("propagation")),
        "pruning": ("Prune in: " + _txt(d.get("pruning_month"))) if _txt(d.get("pruning_month")) else "",
        "pests": _txt(d.get("pest_susceptibility")),
        "hardiness": f"USDA zones {hard.get('min')}–{hard.get('max')}" if hard.get("min") else "",
        "native_range": _txt(d.get("origin")),
        "cycle": d.get("cycle"),
        "mature_size": size,
        "image_url": (d.get("default_image") or {}).get("regular_url"),
    }

    # The care guide has proper prose for watering / sunlight / pruning.
    try:
        g = await client.get("https://perenual.com/api/species-care-guide-list",
                             params={"key": key, "species_id": sid})
        if g.status_code == 200:
            sections = ((g.json().get("data") or [{}])[0] or {}).get("section") or []
            guide = {s.get("type"): _txt(s.get("description")) for s in sections}
            if guide.get("watering"):
                fields["watering"] = guide["watering"]
            if guide.get("sunlight"):
                fields["light"] = (fields["light"] + ". " if fields["light"] else "") + guide["sunlight"]
            if guide.get("pruning"):
                fields["pruning"] = guide["pruning"]
    except httpx.HTTPError:
        pass
    return fields


async def trefle(client, ctx, cfg):
    tok = cfg("trefle_token", "TREFLE_TOKEN")
    if not tok:
        raise Skip("no API token")
    r = await client.get("https://trefle.io/api/v1/species/search", params={"token": tok, "q": ctx.name})
    r.raise_for_status()
    items = r.json().get("data") or []
    if not items:
        return {}
    best = _pick(items, ctx.name, lambda it: it.get("scientific_name"))
    r = await client.get(f"https://trefle.io/api/v1/species/{best['id']}", params={"token": tok})
    r.raise_for_status()
    d = r.json().get("data") or {}
    g = d.get("growth") or {}
    sp = d.get("specifications") or {}

    def scale(v, low, mid, high):
        if v is None:
            return ""
        return f"{low if v <= 3 else mid if v <= 6 else high} ({v}/10)"

    sh = g.get("soil_humidity")
    tmin = (g.get("minimum_temperature") or {}).get("deg_c")
    tmax = (g.get("maximum_temperature") or {}).get("deg_c")
    ph = f"pH {g.get('ph_minimum')}–{g.get('ph_maximum')}" if g.get("ph_minimum") else ""
    height = (sp.get("maximum_height") or {}).get("cm") or (sp.get("average_height") or {}).get("cm")
    return {
        "common_name": d.get("common_name"),
        "family": d.get("family"),
        "light": scale(g.get("light"), "Shade / low light", "Partial sun / bright indirect", "Full sun"),
        "humidity": scale(g.get("atmospheric_humidity"), "Low humidity", "Moderate humidity", "High humidity"),
        "watering": scale(sh, "Likes to dry out", "Moderately moist soil", "Consistently moist soil"),
        "watering_days": max(2, round(14 - 1.1 * sh)) if sh is not None else None,
        "temperature": f"{tmin}°C to {tmax}°C" if tmin is not None and tmax is not None else "",
        "soil": ph,
        "toxicity": sp.get("toxicity"),
        "growth_rate": sp.get("growth_rate"),
        "mature_size": f"Up to {height} cm tall" if height else "",
        "native_range": _txt((d.get("distribution") or {}).get("native"))[:400],
        "image_url": d.get("image_url"),
    }


async def permapeople(client, ctx, cfg):
    kid = cfg("permapeople_key_id", "PERMAPEOPLE_KEY_ID")
    ksec = cfg("permapeople_key_secret", "PERMAPEOPLE_KEY_SECRET")
    if not (kid and ksec):
        raise Skip("no API key id/secret")
    r = await client.post("https://permapeople.org/api/search", json={"q": ctx.name},
                          headers={"x-permapeople-key-id": kid, "x-permapeople-key-secret": ksec})
    r.raise_for_status()
    plants = r.json().get("plants") or []
    if not plants:
        return {}
    p = _pick(plants, ctx.name, lambda it: it.get("scientific_name"))
    kv = {str(x.get("key", "")).lower(): _txt(x.get("value")) for x in p.get("data") or []}

    def g(*names):
        return next((kv[n] for n in names if kv.get(n)), "")

    water = g("water requirement", "water requirements")
    soil = ", ".join(x for x in (g("soil type"), g("soil ph") and f"pH {g('soil ph')}") if x)
    return {
        "common_name": p.get("name"),
        "description": p.get("description"),
        "watering": water,
        "watering_days": _days_from_text(water),
        "light": g("light requirement", "light requirements"),
        "soil": soil,
        "hardiness": g("usda hardiness zone") and f"USDA zones {g('usda hardiness zone')}",
        "growth_rate": g("growth"),
        "cycle": g("life cycle"),
        "mature_size": g("height"),
        "propagation": g("propagation method", "propagation"),
        "native_range": g("native to"),
        "toxicity": g("warning", "toxicity"),
        "family": g("family"),
    }


def _section(text: str, wanted: tuple) -> str:
    heads = [(m.start(), m.end(), len(m.group(1)), m.group(2).strip())
             for m in re.finditer(r"^(==+)\s*(.+?)\s*==+\s*$", text, re.M)]
    for i, (_, end, level, title) in enumerate(heads):
        if any(title.lower().startswith(w) for w in wanted):
            stop = next((s for s, _, lv, _ in heads[i + 1:] if lv <= level), len(text))
            body = re.sub(r"^==+.*?==+\s*$", "", text[end:stop], flags=re.M)
            body = re.sub(r"\n{2,}", "\n", body).strip()
            if len(body) > 1500:
                body = body[:1500]
                body = body[:body.rfind(". ") + 1] or body
            if body:
                return body
    return ""


async def wikipedia(client, ctx, cfg):
    title = ctx.wiki_title or ctx.name.replace(" ", "_")
    r = await client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote(title)}")
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    j = r.json()
    if j.get("type") == "disambiguation":
        return {}
    r2 = await client.get("https://en.wikipedia.org/w/api.php", params={
        "action": "query", "prop": "extracts", "explaintext": 1, "redirects": 1,
        "titles": j.get("title") or title, "format": "json"})
    text = ""
    if r2.status_code == 200:
        pages = (r2.json().get("query") or {}).get("pages") or {}
        text = next(iter(pages.values()), {}).get("extract", "") if pages else ""
    ctx.wiki_text = text[:8000]
    return {
        "description": j.get("extract"),
        "cultivation": _section(text, ("cultivation", "care", "growing", "horticultur")),
        "wikipedia_url": ((j.get("content_urls") or {}).get("desktop") or {}).get("page"),
        "image_url": (j.get("thumbnail") or {}).get("source"),
    }


PROVIDERS = [
    ("iNaturalist", inaturalist),
    ("GBIF", gbif),
    ("Perenual", perenual),
    ("Trefle", trefle),
    ("Permapeople", permapeople),
    ("Wikipedia", wikipedia),
]


async def ollama_fill(client, ctx, cfg, merged: dict) -> tuple[dict, str]:
    url = cfg("ollama_url", "OLLAMA_URL")
    if not url:
        raise Skip("OLLAMA_URL not set")
    model = cfg("ollama_model", "OLLAMA_MODEL") or "llama3.1:8b"
    missing = [k for k in LLM_FIELDS if not merged.get(k)]
    if not missing:
        return {}, model
    schema = {
        "type": "object",
        "properties": {k: {"type": "integer" if k == "watering_days" else "string"} for k in missing},
        "required": missing,
    }
    known = {LABELS[k]: v for k, v in merged.items() if k in HINTS}
    prompt = (
        "You are a horticulturist writing care notes for a home plant-tracking app.\n"
        f"Plant: {ctx.name}" + (f" (searched as \"{ctx.query}\")" if ctx.query != ctx.name else "") + "\n\n"
        "Facts already gathered from plant databases (treat as correct, do not contradict):\n"
        f"{json.dumps(known, indent=1, ensure_ascii=False)}\n\n"
        + (f"Wikipedia article excerpt:\n{ctx.wiki_text[:3500]}\n\n" if ctx.wiki_text else "")
        + "Fill in ONLY these fields, concisely and practically for someone growing it at home "
          "(as a houseplant if it is commonly kept indoors). If unsure, say so briefly rather than inventing.\n"
        + "\n".join(f"- {k}: {HINTS[k]}" for k in missing)
    )
    r = await client.post(url.rstrip("/") + "/api/chat", timeout=600, json={
        "model": model, "stream": False, "format": schema, "options": {"temperature": 0.2},
        "messages": [{"role": "user", "content": prompt}],
    })
    r.raise_for_status()
    return json.loads(r.json()["message"]["content"]), model


async def run(query: str, settings: dict) -> dict:
    """Research a plant. Returns {data, sources, log}."""
    cfg = lambda k, env: db.cfg(settings, k, env)  # noqa: E731
    ctx = Ctx(query.strip())
    merged, sources, logs = {}, {}, []

    def absorb(name, fields):
        used = []
        for k, v in _normalize(fields).items():
            if not merged.get(k):
                merged[k] = v
                sources[k] = name
                used.append(k)
        return used

    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=True) as client:
        for name, fn in PROVIDERS:
            try:
                fields = await fn(client, ctx, cfg)
                got = _normalize(fields or {})
                used = absorb(name, got)
                logs.append({"provider": name, "status": "ok" if got else "no match",
                             "detail": f"{len(got)} fields found, {len(used)} used" if got else "",
                             "fields": used})
            except Skip as e:
                logs.append({"provider": name, "status": "skipped", "detail": str(e), "fields": []})
            except Exception as e:  # one broken source must not stop the rest
                log.warning("%s failed: %s", name, e)
                logs.append({"provider": name, "status": "error", "detail": f"{type(e).__name__}: {e}"[:240],
                             "fields": []})
        try:
            fields, model = await ollama_fill(client, ctx, cfg, merged)
            used = absorb(f"Ollama ({model})", fields)
            logs.append({"provider": f"Ollama ({model})", "status": "ok" if fields else "not needed",
                         "detail": f"filled {len(used)} missing fields" if fields else "databases covered everything",
                         "fields": used})
        except Skip as e:
            logs.append({"provider": "Ollama", "status": "skipped", "detail": str(e), "fields": []})
        except Exception as e:
            log.warning("Ollama failed: %s", e)
            logs.append({"provider": "Ollama", "status": "error", "detail": f"{type(e).__name__}: {e}"[:240],
                         "fields": []})
    return {"data": merged, "sources": sources, "log": logs}


async def research_plant(pid: int) -> None:
    with db.tx() as c:
        p = db.get_plant(c, pid)
        if not p:
            return
        c.execute("UPDATE plants SET research_status='running' WHERE id=?", (pid,))
    try:
        result = await run(p["species"] or p["name"], db.get_settings())
        status = "done" if result["data"] else "error"
    except Exception as e:
        log.exception("research failed")
        result = {"data": {}, "sources": {}, "log": [{"provider": "research", "status": "error",
                                                     "detail": str(e)[:240], "fields": []}]}
        status = "error"
    with db.tx() as c:
        c.execute(
            "INSERT INTO care_profiles(plant_id, data, sources, providers_log, researched_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(plant_id) DO UPDATE SET data=excluded.data, sources=excluded.sources, "
            "providers_log=excluded.providers_log, researched_at=excluded.researched_at",
            (pid, json.dumps(result["data"]), json.dumps(result["sources"]), json.dumps(result["log"]), db.now_iso()))
        c.execute("UPDATE plants SET research_status=? WHERE id=?", (status, pid))
        sci = result["data"].get("scientific_name")
        if sci:
            c.execute("UPDATE plants SET species=? WHERE id=? AND (species IS NULL OR species='')", (sci, pid))


_tasks: set = set()


def start(pid: int) -> None:
    """Fire-and-forget research in the app's event loop."""
    t = asyncio.get_running_loop().create_task(research_plant(pid))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)
