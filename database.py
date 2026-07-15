"""
AquaX AI — persistence layer.

A small SQLite database (single file, zero setup) backing real accounts,
sessions, and irrigation-advice history — replacing the previous in-memory
JS arrays that reset on every page reload.

Deliberately uses only the Python standard library (sqlite3, hashlib,
secrets) so no new pip dependency is introduced.
"""
import sqlite3
import hashlib
import secrets
import os
from datetime import datetime, timezone
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aquax.db")

# Default admin account created once, the first time the database file is
# created. Change this password after first login — it's only meant to get
# the admin panel usable immediately for a hackathon demo.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Password hashing — PBKDF2-HMAC-SHA256 with a per-user random salt.
# Stdlib-only (no bcrypt/passlib dependency needed).
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"{salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return secrets.compare_digest(check.hex(), digest_hex)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signin_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                event TEXT NOT NULL,
                result TEXT NOT NULL,
                at TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS advice_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query_type TEXT NOT NULL,
                query_text TEXT,
                lat REAL,
                lon REAL,
                ai_advice TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            )
        """)

        # Seed a default admin account the very first time the DB is created.
        existing_admin = conn.execute("SELECT id FROM users WHERE role = 'admin' LIMIT 1").fetchone()
        if not existing_admin:
            conn.execute(
                "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                ("Admin", DEFAULT_ADMIN_USERNAME, hash_password(DEFAULT_ADMIN_PASSWORD), "admin", _now())
            )
            print(f"🔐 Seeded default admin account — username: '{DEFAULT_ADMIN_USERNAME}', "
                  f"password: '{DEFAULT_ADMIN_PASSWORD}'. Change this after first login.")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def create_user(name: str, email: str, password: str, role: str = "user"):
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO users (name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, email, hash_password(password), role, _now())
        )
        return cur.lastrowid


def get_user_by_email(email: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def list_users():
    with get_db() as conn:
        rows = conn.execute("SELECT id, name, email, role, created_at FROM users ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Sessions (simple opaque bearer tokens — no expiry, matches "simple & fast")
# ---------------------------------------------------------------------------
def create_session(user_id: int) -> str:
    token = secrets.token_hex(32)
    with get_db() as conn:
        conn.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, user_id, _now()))
    return token


def get_user_by_token(token: str):
    if not token:
        return None
    with get_db() as conn:
        row = conn.execute("""
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
        """, (token,)).fetchone()
        return dict(row) if row else None


def delete_session(token: str):
    with get_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


# ---------------------------------------------------------------------------
# Sign-in activity log (mirrors the previous "sign-in database" admin table)
# ---------------------------------------------------------------------------
def log_signin_event(email: str, event: str, result: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO signin_log (email, event, result, at) VALUES (?, ?, ?, ?)",
            (email, event, result, _now())
        )


def list_signin_log(limit: int = 200):
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM signin_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Advice history — best-effort logging, called from main.py after a
# recommendation/voice response is already built. Never allowed to break
# the response if it fails (callers wrap this in try/except).
# ---------------------------------------------------------------------------
def log_advice(user_id: int, query_type: str, query_text: str, lat, lon, ai_advice: str):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO advice_history (user_id, query_type, query_text, lat, lon, ai_advice, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, query_type, query_text, lat, lon, ai_advice, _now())
        )


def list_user_history(user_id: int, limit: int = 50):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM advice_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_history(limit: int = 200):
    with get_db() as conn:
        rows = conn.execute("""
            SELECT advice_history.*, users.name AS user_name, users.email AS user_email
            FROM advice_history
            JOIN users ON users.id = advice_history.user_id
            ORDER BY advice_history.id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
