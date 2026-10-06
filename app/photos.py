"""Photo storage: normalize to JPEG (handles iPhone HEIC + EXIF rotation), keep a thumbnail, read EXIF date."""
import io
import os
import time
import uuid
from datetime import datetime

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover
    pass

from . import db

FULL_MAX = 2560
THUMB_MAX = 720


def _exif_date(img: Image.Image) -> datetime | None:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(36867) or exif.get(306)
        if raw:
            return datetime.strptime(str(raw).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _jpeg(img: Image.Image, max_side: int, quality: int) -> bytes:
    im = img.copy()
    im.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def load(data: bytes) -> tuple[Image.Image, datetime | None]:
    img = Image.open(io.BytesIO(data))
    taken = _exif_date(img)
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img, taken


def for_identification(data: bytes) -> bytes:
    img, _ = load(data)
    return _jpeg(img, 1280, 85)


def save(plant_id: int, data: bytes, taken_at: datetime | None = None, note: str | None = None) -> int:
    img, exif_taken = load(data)
    name = uuid.uuid4().hex
    folder = os.path.join(db.PHOTO_DIR, str(plant_id))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"{name}.jpg"), "wb") as f:
        f.write(_jpeg(img, FULL_MAX, 88))
    with open(os.path.join(folder, f"{name}_t.jpg"), "wb") as f:
        f.write(_jpeg(img, THUMB_MAX, 80))
    when = taken_at or exif_taken or datetime.now()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO photos(plant_id, filename, thumb, taken_at, uploaded_at, note) VALUES (?,?,?,?,?,?)",
            (plant_id, f"{plant_id}/{name}.jpg", f"{plant_id}/{name}_t.jpg",
             when.replace(microsecond=0).isoformat(), db.now_iso(), note or None))
        return cur.lastrowid


def delete_files(row) -> None:
    for rel in (row["filename"], row["thumb"]):
        try:
            os.remove(os.path.join(db.PHOTO_DIR, rel))
        except FileNotFoundError:
            pass


# ---- temporary uploads (identify -> "add as new plant") ------------------

def stash(data: bytes) -> str:
    token = uuid.uuid4().hex
    with open(os.path.join(db.TMP_DIR, token), "wb") as f:
        f.write(data)
    return token


def unstash(token: str) -> bytes | None:
    if not token or not token.isalnum():
        return None
    path = os.path.join(db.TMP_DIR, token)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as f:
        data = f.read()
    os.remove(path)
    return data


def stash_url(token: str) -> str:
    return f"/tmp-photo/{token}"


def clean_stash(max_age_hours: int = 24) -> None:
    cutoff = time.time() - max_age_hours * 3600
    for n in os.listdir(db.TMP_DIR):
        p = os.path.join(db.TMP_DIR, n)
        if os.path.getmtime(p) < cutoff:
            os.remove(p)
