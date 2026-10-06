import asyncio
import io
import itertools
import json
import logging
import re
import secrets
import shutil
import os
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, db, notify, photos, research, watering
from . import identify as ident

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
BASE = Path(__file__).parent
db.init()


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(watering.loop())
    yield
    task.cancel()


app = FastAPI(title="Plant Tracker", lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
              openapi_url="/api/openapi.json")
templates = Jinja2Templates(directory=str(BASE / "templates"))


# ---- formatting helpers ---------------------------------------------------

def _dt(v) -> datetime:
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime.combine(v, datetime.min.time())
    return datetime.fromisoformat(str(v))


def fdate(v) -> str:
    if not v:
        return ""
    d = _dt(v)
    return f"{d:%b} {d.day}, {d.year}"


def fdt(v) -> str:
    if not v:
        return ""
    d = _dt(v)
    return f"{fdate(d)} · {d:%I:%M %p}".replace("· 0", "· ")


def ago(v) -> str:
    if not v:
        return "never"
    days = (date.today() - _dt(v).date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 0:
        return f"in {-days} days"
    return f"{days} days ago"


def age_days(p: dict, at: date | None = None) -> int:
    at = at or date.today()
    acq = watering._date(p.get("acquired_on")) or watering._date(p.get("created_at")) or at
    return (at - acq).days + int(int(p.get("age_at_acquisition_months") or 0) * 30.44)


def age_text(p: dict, at: date | None = None) -> str:
    days = age_days(p, at)
    if days < 0:
        return ""
    if days < 14:
        return f"{days} day{'s' if days != 1 else ''}"
    if days < 60:
        return f"{days // 7} weeks"
    y, m = divmod(int(days / 30.44), 12)
    if y and m:
        return f"{y} yr {m} mo"
    return f"{y} yr" if y else f"{m} mo"


templates.env.filters.update(fdate=fdate, fdt=fdt, ago=ago)
templates.env.globals.update(LABELS=research.LABELS)


def redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def render(request: Request, name: str, status: int = 200, **ctx):
    user = request.session.get("user")
    open_events = 0
    if user:
        with db.tx() as c:
            open_events = c.execute("SELECT COUNT(*) FROM events WHERE status IN ('pending','sent')").fetchone()[0]
    return templates.TemplateResponse(request, name, {"user": user, "open_events": open_events, **ctx},
                                      status_code=status)


# ---- auth -------------------------------------------------------------------

PUBLIC = {"/login", "/setup", "/healthz"}
_has_user = False


@app.middleware("http")
async def require_login(request: Request, call_next):
    global _has_user
    path = request.url.path
    if path.startswith("/static/") or path in PUBLIC:
        return await call_next(request)
    if not _has_user:
        _has_user = db.has_user()
        if not _has_user:
            return redirect("/setup")
    if request.session.get("user"):
        return await call_next(request)
    if path.startswith("/api/"):
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip() \
            or request.query_params.get("token", "")
        if tok and secrets.compare_digest(tok, db.get_settings()["api_token"]):
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if request.headers.get("hx-request"):
        return Response(headers={"HX-Redirect": "/login"})
    return redirect("/login?next=" + quote(path))


app.add_middleware(SessionMiddleware, secret_key=db.get_settings()["session_secret"],
                   session_cookie="plants_session", max_age=60 * 60 * 24 * 30, same_site="lax")
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")
app.mount("/media", StaticFiles(directory=db.PHOTO_DIR), name="media")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    if db.has_user():
        return redirect("/login")
    return render(request, "login.html", setup=True)


@app.post("/setup")
async def setup_post(request: Request):
    global _has_user
    if db.has_user():
        return redirect("/login")
    f = await request.form()
    username, pw = (f.get("username") or "").strip(), f.get("password") or ""
    err = None
    if not username:
        err = "Pick a username."
    elif len(pw) < 8:
        err = "Password must be at least 8 characters."
    elif pw != f.get("confirm"):
        err = "Passwords don't match."
    if err:
        return render(request, "login.html", 400, setup=True, error=err, username=username)
    auth.create_user(username, pw)
    _has_user = True
    request.session["user"] = username
    return redirect("/")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if not db.has_user():
        return redirect("/setup")
    return render(request, "login.html", next=next)


@app.post("/login")
async def login_post(request: Request):
    f = await request.form()
    username = (f.get("username") or "").strip()
    nxt = f.get("next") or "/"
    if not nxt.startswith("/") or nxt.startswith("//"):
        nxt = "/"
    if auth.check_login(username, f.get("password") or ""):
        request.session["user"] = username
        return redirect(nxt)
    await asyncio.sleep(1)
    return render(request, "login.html", 401, error="Wrong username or password.", username=username, next=nxt)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return redirect("/login")


# ---- plant grid -------------------------------------------------------------

PAGE = 24
SORTS = {"urgency": "Needs water first", "name": "Name", "newest": "Recently added", "oldest": "Oldest plant"}


def build_card(c, p: dict, s: dict) -> dict:
    care = db.care_profile(c, p["id"])["data"]
    sch = watering.schedule(p, care, s, db.last_watered(c, p["id"]))
    sub = p.get("species") if (p.get("species") or "").lower() != p["name"].lower() else care.get("common_name")
    return {"p": p, "s": sch, "cover": db.cover_url(c, p) or care.get("image_url"),
            "age": age_text(p), "sub": sub}


def list_cards(q: str, sort: str) -> list:
    s = db.get_settings()
    cards = []
    with db.tx() as c:
        for r in c.execute("SELECT * FROM plants WHERE archived=0").fetchall():
            p = dict(r)
            hay = " ".join(str(p.get(k) or "") for k in ("name", "species", "location")).lower()
            if q and q.lower() not in hay:
                continue
            cards.append(build_card(c, p, s))
    keys = {
        "urgency": lambda x: (x["s"]["days_left"], x["p"]["name"].lower()),
        "name": lambda x: x["p"]["name"].lower(),
        "newest": lambda x: x["p"]["created_at"],
        "oldest": lambda x: -age_days(x["p"]),
    }
    cards.sort(key=keys.get(sort, keys["urgency"]), reverse=(sort == "newest"))
    return cards


def page_ctx(cards, page, q, sort):
    start = (page - 1) * PAGE
    return dict(cards=cards[start:start + PAGE], page=page, has_more=len(cards) > start + PAGE, q=q, sort=sort)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", sort: str = "urgency"):
    cards = list_cards(q, sort)
    return render(request, "index.html", total=len(cards), sorts=SORTS, **page_ctx(cards, 1, q, sort))


@app.get("/tiles", response_class=HTMLResponse)
def tiles(request: Request, q: str = "", sort: str = "urgency", page: int = 1):
    return render(request, "_tiles.html", **page_ctx(list_cards(q, sort), max(1, page), q, sort))


# ---- plant create / edit ------------------------------------------------------

def options_ctx() -> dict:
    s = db.get_settings()
    return {"pot_types": s["pot_types"], "light_levels": s["light_levels"], "water_prefs": s["water_prefs"]}


def plant_fields(f) -> dict:
    try:
        months = max(0, int(f.get("age_months") or 0))
    except ValueError:
        months = 0
    return {
        "name": (f.get("name") or "").strip()[:120],
        "species": (f.get("species") or "").strip()[:160],
        "location": (f.get("location") or "").strip()[:120],
        "acquired_on": f.get("acquired_on") or None,
        "age_at_acquisition_months": months,
        "pot_type": f.get("pot_type") or None,
        "light_level": f.get("light_level") or None,
        "water_pref": f.get("water_pref") or "auto",
        "notes": (f.get("notes") or "").strip(),
    }


def parse_when(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v + ("T12:00" if len(v) == 10 else ""))
    except ValueError:
        return None


async def save_uploads(pid: int, uploads, taken_at=None, note=None) -> int:
    n = 0
    for up in uploads:
        if not getattr(up, "filename", None):
            continue
        data = await up.read()
        if not data:
            continue
        try:
            await asyncio.to_thread(photos.save, pid, data, taken_at, note)
            n += 1
        except Exception as e:
            logging.warning("could not read upload %s: %s", up.filename, e)
    return n


@app.get("/plants/new", response_class=HTMLResponse)
def new_plant(request: Request, species: str = "", name: str = "", tmp: str = ""):
    p = {"name": name, "species": species, "water_pref": "auto", "pot_type": "plastic", "light_level": "medium"}
    return render(request, "form.html", p=p, tmp=tmp if tmp.isalnum() else "", **options_ctx())


@app.post("/plants/new")
async def create_plant(request: Request):
    f = await request.form()
    p = plant_fields(f)
    if not p["name"]:
        return render(request, "form.html", 400, p=p, error="Give your plant a name.", tmp=f.get("tmp", ""),
                      **options_ctx())
    with db.tx() as c:
        pid = c.execute(
            "INSERT INTO plants(name, species, location, acquired_on, age_at_acquisition_months, pot_type, "
            "light_level, water_pref, notes, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (*[p[k] for k in ("name", "species", "location", "acquired_on", "age_at_acquisition_months",
                              "pot_type", "light_level", "water_pref", "notes")], db.now_iso())).lastrowid
        lw = parse_when(f.get("last_watered"))
        if lw:
            c.execute("INSERT INTO care_log(plant_id, kind, at) VALUES (?,?,?)", (pid, "watered", lw.isoformat()))
    stashed = photos.unstash(f.get("tmp", ""))
    if stashed:
        await asyncio.to_thread(photos.save, pid, stashed)
    await save_uploads(pid, f.getlist("photos"))
    with db.tx() as c:
        c.execute("UPDATE plants SET research_status='running' WHERE id=?", (pid,))
    research.start(pid)
    return redirect(f"/plants/{pid}")


@app.get("/plants/{pid}/edit", response_class=HTMLResponse)
def edit_plant(request: Request, pid: int):
    with db.tx() as c:
        p = db.get_plant(c, pid)
    if not p:
        raise HTTPException(404)
    return render(request, "form.html", p=p, editing=True, **options_ctx())


@app.post("/plants/{pid}/edit")
async def update_plant(request: Request, pid: int):
    f = await request.form()
    p = plant_fields(f)
    with db.tx() as c:
        old = db.get_plant(c, pid)
        if not old:
            raise HTTPException(404)
        if not p["name"]:
            return render(request, "form.html", 400, p={**old, **p}, editing=True, error="Name is required.",
                          **options_ctx())
        c.execute("UPDATE plants SET name=?, species=?, location=?, acquired_on=?, age_at_acquisition_months=?, "
                  "pot_type=?, light_level=?, water_pref=?, notes=? WHERE id=?",
                  (*[p[k] for k in ("name", "species", "location", "acquired_on", "age_at_acquisition_months",
                                    "pot_type", "light_level", "water_pref", "notes")], pid))
        if f.get("reset_learning"):
            c.execute("UPDATE plants SET watering_adjust=1.0, snooze_until=NULL WHERE id=?", (pid,))
        species_changed = (old.get("species") or "") != p["species"]
    if species_changed:
        with db.tx() as c:
            c.execute("UPDATE plants SET research_status='running' WHERE id=?", (pid,))
        research.start(pid)
    return redirect(f"/plants/{pid}")


@app.post("/plants/{pid}/delete")
def delete_plant(pid: int):
    with db.tx() as c:
        c.execute("DELETE FROM plants WHERE id=?", (pid,))
    shutil.rmtree(os.path.join(db.PHOTO_DIR, str(pid)), ignore_errors=True)
    return redirect("/")


# ---- plant detail -----------------------------------------------------------

def detail_ctx(pid: int) -> dict:
    s = db.get_settings()
    with db.tx() as c:
        p = db.get_plant(c, pid)
        if not p:
            raise HTTPException(404)
        care = db.care_profile(c, pid)
        sch = watering.schedule(p, care["data"], s, db.last_watered(c, pid))
        pics = [dict(r) for r in c.execute("SELECT * FROM photos WHERE plant_id=? ORDER BY taken_at DESC", (pid,))]
        log = [dict(r) for r in c.execute(
            "SELECT * FROM care_log WHERE plant_id=? ORDER BY at DESC LIMIT 15", (pid,))]
        cover = db.cover_url(c, p) or care["data"].get("image_url")
    for ph in pics:
        ph["age"] = age_text(p, date.fromisoformat(ph["taken_at"][:10]))
    groups = [(datetime.strptime(k, "%Y-%m").strftime("%B %Y"), list(g))
              for k, g in itertools.groupby(pics, key=lambda x: x["taken_at"][:7])]
    labels = {
        "pot": watering.option(s["pot_types"], p.get("pot_type"))[1],
        "light": watering.option(s["light_levels"], p.get("light_level"))[1],
        "water": watering.option(s["water_prefs"], p.get("water_pref"))[1],
    }
    return dict(p=p, care=care, fields=research.FIELDS, sch=sch, photos=pics, groups=groups, log=log,
                cover=cover, age=age_text(p), labels=labels, now_local=datetime.now().strftime("%Y-%m-%dT%H:%M"))


@app.get("/plants/{pid}", response_class=HTMLResponse)
def plant_detail(request: Request, pid: int):
    return render(request, "detail.html", **detail_ctx(pid))


@app.get("/plants/{pid}/care", response_class=HTMLResponse)
def care_partial(request: Request, pid: int):
    return render(request, "_care.html", **detail_ctx(pid))


@app.post("/plants/{pid}/research", response_class=HTMLResponse)
async def rerun_research(request: Request, pid: int):
    with db.tx() as c:
        if not db.get_plant(c, pid):
            raise HTTPException(404)
        c.execute("UPDATE plants SET research_status='running' WHERE id=?", (pid,))
    research.start(pid)
    return render(request, "_care.html", **detail_ctx(pid))


@app.get("/plants/{pid}/care/edit", response_class=HTMLResponse)
def edit_care(request: Request, pid: int):
    return render(request, "care_form.html", **detail_ctx(pid))


@app.post("/plants/{pid}/care/edit")
async def save_care(request: Request, pid: int):
    f = await request.form()
    with db.tx() as c:
        care = db.care_profile(c, pid)
        overrides = {}
        for k in research.FIELD_KEYS:
            v = (f.get(k) or "").strip()
            if not v:
                continue
            if k == "watering_days":
                try:
                    v = max(1, int(float(v)))
                except ValueError:
                    continue
            if str(care["researched"].get(k, "")) != str(v):
                overrides[k] = v
        c.execute("INSERT INTO care_profiles(plant_id, overrides) VALUES (?, ?) "
                  "ON CONFLICT(plant_id) DO UPDATE SET overrides=excluded.overrides", (pid, json.dumps(overrides)))
    return redirect(f"/plants/{pid}#care")


@app.post("/plants/{pid}/water")
async def water(request: Request, pid: int):
    f = await request.form()
    kind = f.get("kind") or "watered"
    if kind not in ("watered", "dry", "wet", "fertilized", "repotted", "note"):
        raise HTTPException(400)
    watering.apply_action(pid, kind, parse_when(f.get("when")), (f.get("note") or "").strip() or None)
    if f.get("ret") == "tile":
        with db.tx() as c:
            p = db.get_plant(c, pid)
            card = build_card(c, p, db.get_settings())
        return render(request, "_tile.html", c=card)
    return redirect(f"/plants/{pid}#water")


# ---- photos -------------------------------------------------------------------

@app.post("/plants/{pid}/photos")
async def upload_photos(request: Request, pid: int):
    f = await request.form()
    with db.tx() as c:
        if not db.get_plant(c, pid):
            raise HTTPException(404)
    await save_uploads(pid, f.getlist("photos"), parse_when(f.get("taken_at")), (f.get("note") or "").strip())
    return redirect(f"/plants/{pid}#timeline")


def _photo(c, photo_id: int) -> dict:
    r = c.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return dict(r)


@app.post("/photos/{photo_id}/cover")
def set_cover(photo_id: int):
    with db.tx() as c:
        ph = _photo(c, photo_id)
        c.execute("UPDATE plants SET cover_photo_id=? WHERE id=?", (photo_id, ph["plant_id"]))
    return redirect(f"/plants/{ph['plant_id']}#timeline")


@app.post("/photos/{photo_id}/edit")
async def edit_photo(request: Request, photo_id: int):
    f = await request.form()
    with db.tx() as c:
        ph = _photo(c, photo_id)
        when = parse_when(f.get("taken_at"))
        c.execute("UPDATE photos SET taken_at=?, note=? WHERE id=?",
                  ((when.isoformat() if when else ph["taken_at"]), (f.get("note") or "").strip() or None, photo_id))
    return redirect(f"/plants/{ph['plant_id']}#timeline")


@app.post("/photos/{photo_id}/delete")
def delete_photo(photo_id: int):
    with db.tx() as c:
        ph = _photo(c, photo_id)
        c.execute("DELETE FROM photos WHERE id=?", (photo_id,))
        c.execute("UPDATE plants SET cover_photo_id=NULL WHERE cover_photo_id=?", (photo_id,))
    photos.delete_files(ph)
    return redirect(f"/plants/{ph['plant_id']}#timeline")


# ---- identify by photo --------------------------------------------------------

@app.get("/identify", response_class=HTMLResponse)
def identify_page(request: Request):
    return render(request, "identify.html")


@app.get("/tmp-photo/{token}")
def tmp_photo(token: str):
    path = os.path.join(db.TMP_DIR, token)
    if not token.isalnum() or not os.path.exists(path):
        raise HTTPException(404)
    with open(path, "rb") as fh:
        img, _ = photos.load(fh.read())
    return Response(photos._jpeg(img, 900, 82), media_type="image/jpeg")


@app.post("/identify", response_class=HTMLResponse)
async def identify_upload(request: Request):
    f = await request.form()
    up = f.get("photo")
    data = await up.read() if getattr(up, "filename", None) else b""
    if not data:
        return render(request, "identify.html", 400, error="Choose a photo first.")
    try:
        jpeg = await asyncio.to_thread(photos.for_identification, data)
    except Exception:
        return render(request, "identify.html", 400, error="That file doesn't look like an image I can read.")
    token = photos.stash(data)
    result = await ident.identify(jpeg)
    return render(request, "identify.html", result=result, preview=photos.stash_url(token), tmp=token)


@app.post("/photos/{photo_id}/identify", response_class=HTMLResponse)
async def identify_existing(request: Request, photo_id: int):
    with db.tx() as c:
        ph = _photo(c, photo_id)
        p = db.get_plant(c, ph["plant_id"])
    with open(os.path.join(db.PHOTO_DIR, ph["filename"]), "rb") as fh:
        jpeg = await asyncio.to_thread(photos.for_identification, fh.read())
    result = await ident.identify(jpeg)
    return render(request, "identify.html", result=result, preview=f"/media/{ph['thumb']}", plant=p)


@app.post("/plants/{pid}/species")
async def set_species(request: Request, pid: int):
    f = await request.form()
    species = (f.get("species") or "").strip()[:160]
    with db.tx() as c:
        if not db.get_plant(c, pid):
            raise HTTPException(404)
        c.execute("UPDATE plants SET species=?, research_status='running' WHERE id=?", (species, pid))
    research.start(pid)
    return redirect(f"/plants/{pid}")


# ---- events -----------------------------------------------------------------

def events_list(status: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT e.*, p.name AS plant_name FROM events e LEFT JOIN plants p ON p.id=e.plant_id"
    args = []
    if status == "open":
        sql += " WHERE e.status IN ('pending','sent')"
    elif status:
        sql += " WHERE e.status=?"
        args.append(status)
    sql += " ORDER BY e.created_at DESC, e.id DESC LIMIT ?"
    args.append(limit)
    with db.tx() as c:
        return [dict(r) for r in c.execute(sql, args)]


@app.get("/events", response_class=HTMLResponse)
def events_page(request: Request):
    return render(request, "events.html", open=events_list("open"), recent=events_list(None, 60))


@app.post("/events/check")
def events_check():
    watering.check_once()
    return redirect("/events")


@app.post("/events/{eid}/ack")
def ack_event(eid: int):
    with db.tx() as c:
        c.execute("UPDATE events SET status='acked' WHERE id=?", (eid,))
    return redirect("/events")


# ---- settings -----------------------------------------------------------------

LISTS = [("pot_types", "Pot types", "Terracotta dries fast (< 1), self-watering slow (> 1)."),
         ("light_levels", "Light conditions", "More light = faster drying = smaller factor."),
         ("water_prefs", "Watering styles", "Per-plant preference applied on top of the researched baseline.")]

INTEGRATIONS = [
    ("Plant databases", [
        ("perenual_key", "Perenual API key", "PERENUAL_API_KEY", "https://perenual.com/docs/api"),
        ("trefle_token", "Trefle access token", "TREFLE_TOKEN", "https://trefle.io"),
        ("permapeople_key_id", "Permapeople key ID", "PERMAPEOPLE_KEY_ID", "https://permapeople.org/knowledgebase/api-docs.html"),
        ("permapeople_key_secret", "Permapeople key secret", "PERMAPEOPLE_KEY_SECRET", ""),
    ]),
    ("Photo identification", [
        ("plantnet_key", "Pl@ntNet API key", "PLANTNET_API_KEY", "https://my.plantnet.org"),
    ]),
    ("Local AI (Ollama)", [
        ("ollama_url", "Ollama URL", "OLLAMA_URL", "e.g. http://ollama:11434"),
        ("ollama_model", "Text model", "OLLAMA_MODEL", "default llama3.1:8b"),
        ("ollama_vision_model", "Vision model", "OLLAMA_VISION_MODEL", "default qwen2.5vl:7b"),
    ]),
    ("Notifications (ntfy)", [
        ("ntfy_url", "ntfy topic URL", "NTFY_URL", "e.g. http://ntfy.lan/plants — leave blank to only log events"),
        ("ntfy_token", "ntfy access token", "NTFY_TOKEN", "only if your topic needs auth"),
        ("base_url", "This app's URL", "BASE_URL", "e.g. http://plants.lan:8080 — for tap-to-open and the Watered button"),
    ]),
]
SECRET_KEYS = {"perenual_key", "trefle_token", "permapeople_key_secret", "plantnet_key", "ntfy_token"}


def settings_ctx(**extra):
    s = db.get_settings()
    return dict(s=s, lists=LISTS, integrations=INTEGRATIONS, secret_keys=SECRET_KEYS, env=os.environ, **extra)


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    return render(request, "settings.html", **settings_ctx(msg=msg))


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_") or "item"


@app.post("/settings/lists")
async def save_lists(request: Request):
    f = await request.form()
    for name, _, _ in LISTS:
        items, seen = [], set()
        for k, label, factor in zip(f.getlist(f"{name}.key"), f.getlist(f"{name}.label"),
                                    f.getlist(f"{name}.factor")):
            label = label.strip()
            if not label:
                continue
            k = k or _slug(label)
            while k in seen:
                k += "_2"
            seen.add(k)
            try:
                factor = min(5.0, max(0.1, float(factor)))
            except ValueError:
                factor = 1.0
            items.append({"key": k, "label": label[:80], "factor": round(factor, 3)})
        if items:
            db.set_setting(name, items)
    return redirect("/settings?msg=Saved#lists")


@app.post("/settings/general")
async def save_general(request: Request):
    f = await request.form()

    def num(k, lo, hi, cast=float, default=1.0):
        try:
            return min(hi, max(lo, cast(f.get(k))))
        except (TypeError, ValueError):
            return default

    db.set_setting("seasons", {"hemisphere": "south" if f.get("hemisphere") == "south" else "north",
                               **{x: num(x, 0.1, 5) for x in ("winter", "spring", "summer", "fall")}})
    db.set_setting("default_watering_days", num("default_watering_days", 1, 60, int, 7))
    db.set_setting("reminder_hour", num("reminder_hour", 0, 23, int, 9))
    db.set_setting("overdue_after_days", num("overdue_after_days", 1, 30, int, 2))
    db.set_setting("still_wet_snooze_days", num("still_wet_snooze_days", 1, 14, int, 2))
    return redirect("/settings?msg=Saved#general")


@app.post("/settings/integrations")
async def save_integrations(request: Request):
    f = await request.form()
    for _, items in INTEGRATIONS:
        for key, *_ in items:
            if key in f:
                db.set_setting(key, (f.get(key) or "").strip())
    return redirect("/settings?msg=Saved#integrations")


@app.post("/settings/test-research", response_class=HTMLResponse)
async def test_research(request: Request):
    f = await request.form()
    q = (f.get("q") or "Monstera deliciosa").strip()
    result = await research.run(q, db.get_settings())
    return render(request, "_research_test.html", q=q, result=result)


@app.post("/settings/test-ntfy", response_class=HTMLResponse)
async def test_ntfy():
    return HTMLResponse(f"<span>{await notify.test()}</span>")


@app.post("/settings/token")
def regen_token():
    db.set_setting("api_token", secrets.token_urlsafe(32))
    return redirect("/settings?msg=New+API+token+generated#api")


@app.post("/settings/password")
async def change_password(request: Request):
    f = await request.form()
    user = request.session.get("user")
    if not auth.check_login(user, f.get("current") or ""):
        return redirect("/settings?msg=Current+password+is+wrong#account")
    if len(f.get("new") or "") < 8 or f.get("new") != f.get("confirm"):
        return redirect("/settings?msg=New+passwords+must+match+and+be+8%2B+characters#account")
    auth.set_password(user, f.get("new"))
    return redirect("/settings?msg=Password+changed#account")


# ---- JSON API -----------------------------------------------------------------

def plant_json(c, p, s) -> dict:
    card = build_card(c, p, s)
    sch = card["s"]
    return {"id": p["id"], "name": p["name"], "species": p["species"], "location": p["location"],
            "age": card["age"], "next_water": sch["due"].isoformat(), "days_left": sch["days_left"],
            "status": sch["status"], "interval_days": sch["interval"],
            "last_watered": sch["last"].isoformat() if sch["last"] else None}


@app.get("/api/plants")
def api_plants():
    s = db.get_settings()
    with db.tx() as c:
        return [plant_json(c, dict(r), s) for r in c.execute("SELECT * FROM plants WHERE archived=0")]


@app.get("/api/plants/{pid}")
def api_plant(pid: int):
    with db.tx() as c:
        p = db.get_plant(c, pid)
        if not p:
            raise HTTPException(404)
        return {**plant_json(c, p, db.get_settings()), "care": db.care_profile(c, pid)["data"]}


@app.post("/api/plants/{pid}/water")
def api_water(pid: int, kind: str = "watered"):
    if kind not in ("watered", "dry", "wet"):
        raise HTTPException(400)
    watering.apply_action(pid, kind)
    return {"ok": True}


@app.get("/api/events")
def api_events(status: str | None = "open", limit: int = 100):
    return events_list(status if status != "all" else None, min(limit, 500))


@app.post("/api/events/{eid}/ack")
def api_ack(eid: int):
    with db.tx() as c:
        c.execute("UPDATE events SET status='acked' WHERE id=?", (eid,))
    return {"ok": True}
