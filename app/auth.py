import hashlib
import hmac
import secrets

from . import db

ITERATIONS = 240_000


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, ITERATIONS)
    return f"pbkdf2${ITERATIONS}${salt.hex()}${h.hex()}"


def verify(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


def create_user(username: str, pw: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO users(username, pw_hash) VALUES (?, ?)", (username, hash_password(pw)))


def check_login(username: str, pw: str) -> bool:
    with db.tx() as c:
        r = c.execute("SELECT pw_hash FROM users WHERE username=?", (username,)).fetchone()
    return bool(r) and verify(pw, r["pw_hash"])


def set_password(username: str, pw: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET pw_hash=? WHERE username=?", (hash_password(pw), username))
