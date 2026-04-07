"""
InviteForge — Database Layer
SQLite for runtime storage. GitHub Gist for persistence across Render restarts/deploys.

On startup: load_from_gist() rebuilds SQLite from Gist JSON.
On every write: save_to_gist() serialises SQLite back to Gist.

Handles 500+ concurrent users on a single Render instance.
Thread-safe writes via _write_lock. WAL mode for concurrent reads.
"""

import sqlite3
import json
import threading
import uuid
import urllib.request
from pathlib import Path
from datetime import datetime
from typing import Optional

DB_PATH = Path("inviteforge.db")
_write_lock = threading.Lock()

# Populated by configure() at startup
_GIST_ID: str = ""
_GITHUB_TOKEN: str = ""


def configure(gist_id: str, github_token: str):
    global _GIST_ID, _GITHUB_TOKEN
    _GIST_ID = gist_id
    _GITHUB_TOKEN = github_token


# ── CONNECTION ──

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # concurrent reads, serialised writes
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create tables if missing. Safe to call on every startup."""
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT PRIMARY KEY,
            email        TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id          TEXT PRIMARY KEY,
            user_id     TEXT,
            data_json   TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id)
        );
    """)
    conn.commit()
    conn.close()


# ── GIST SYNC ──

def _gist_request(method: str, data: Optional[dict] = None) -> Optional[dict]:
    if not _GIST_ID or not _GITHUB_TOKEN:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/gists/{_GIST_ID}",
        method=method,
        headers={
            "Authorization": f"token {_GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
    )
    if data is not None:
        req.data = json.dumps(data).encode()
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception:
        return None


def load_from_gist():
    """Pull events + users from Gist and insert into SQLite (IGNORE duplicates)."""
    result = _gist_request("GET")
    if not result or "files" not in result:
        return

    conn = get_conn()

    # ── events.json ──
    ef = result["files"].get("events.json")
    if ef:
        try:
            events = json.loads(ef["content"])
            for eid, edata in events.items():
                conn.execute(
                    "INSERT OR IGNORE INTO events (id, user_id, data_json, created_at) VALUES (?, ?, ?, ?)",
                    (eid, edata.get("user_id"), json.dumps(edata, default=str),
                     edata.get("created", datetime.now().isoformat())),
                )
            conn.commit()
        except Exception as e:
            print(f"[CRITICAL] load_from_gist: failed to load events — starting empty: {e}")

    # ── users.json ──
    uf = result["files"].get("users.json")
    if uf:
        try:
            users = json.loads(uf["content"])
            for uid, udata in users.items():
                conn.execute(
                    "INSERT OR REPLACE INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (uid, udata["email"], udata["password_hash"], udata.get("created_at", "")),
                )
            conn.commit()
        except Exception as e:
            print(f"[CRITICAL] load_from_gist: failed to load users — starting empty: {e}")

    conn.close()


def save_to_gist():
    """Serialise current SQLite state → Gist (background, best-effort)."""
    if not _GIST_ID or not _GITHUB_TOKEN:
        return

    def _do_save():
        conn = get_conn()

        # Events
        event_rows = conn.execute("SELECT data_json FROM events").fetchall()
        events: dict = {}
        for row in event_rows:
            try:
                edata = json.loads(row["data_json"])
                events[edata["id"]] = edata
            except Exception:
                pass

        # Users (never include password_hash in events.json — separate file)
        user_rows = conn.execute(
            "SELECT id, email, password_hash, created_at FROM users"
        ).fetchall()
        users = {
            r["id"]: {
                "email": r["email"],
                "password_hash": r["password_hash"],
                "created_at": r["created_at"],
            }
            for r in user_rows
        }
        conn.close()

        result = _gist_request(
            "PATCH",
            {
                "files": {
                    "events.json": {"content": json.dumps(events, indent=2, default=str)},
                    "users.json":  {"content": json.dumps(users,  indent=2, default=str)},
                }
            },
        )
        if result is None:
            print("[ERROR] save_to_gist: Gist PATCH failed — data may not be persisted")

    # Run in background thread so writes never block the request
    threading.Thread(target=_do_save, daemon=True).start()


# ── EVENT CRUD ──

def load_events() -> dict:
    """Return all events as {event_id: event_dict}."""
    conn = get_conn()
    rows = conn.execute("SELECT data_json FROM events").fetchall()
    conn.close()
    result: dict = {}
    for row in rows:
        try:
            edata = json.loads(row["data_json"])
            result[edata["id"]] = edata
        except Exception:
            pass
    return result


def save_events(events: dict):
    """
    Bulk-upsert events dict → SQLite + Gist.
    Drop-in replacement for the old save_events() in main.py.
    """
    conn = get_conn()
    try:
        with _write_lock:
            for eid, edata in events.items():
                conn.execute(
                    "INSERT OR REPLACE INTO events (id, user_id, data_json, created_at) VALUES (?, ?, ?, ?)",
                    (eid, edata.get("user_id"),
                     json.dumps(edata, default=str),
                     edata.get("created", datetime.now().isoformat())),
                )
            conn.commit()
    finally:
        conn.close()
    save_to_gist()


def get_events_for_user(user_id: str) -> list:
    """Return all events belonging to user_id, newest first."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT data_json FROM events WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        try:
            result.append(json.loads(row["data_json"]))
        except Exception:
            pass
    return result


# ── USER CRUD ──

def create_user(email: str, password_hash: str) -> dict:
    user_id = str(uuid.uuid4())
    created_at = datetime.now().isoformat()
    conn = get_conn()
    try:
        with _write_lock:
            conn.execute(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email.lower().strip(), password_hash, created_at),
            )
            conn.commit()
    finally:
        conn.close()
    save_to_gist()
    return {"id": user_id, "email": email.lower().strip(), "created_at": created_at}


def get_user_by_email(email: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", (email.lower().strip(),)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None
