import json
import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "state.db"


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS allowed_users(
                telegram_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS user_state(
                telegram_id INTEGER PRIMARY KEY,
                flow TEXT NOT NULL,
                step TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS pending_match(
                token TEXT PRIMARY KEY,
                telegram_id INTEGER NOT NULL,
                target TEXT NOT NULL,
                tracking_code TEXT NOT NULL,
                source_row TEXT NOT NULL,
                choices TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )


def get_config(key, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_config(key, value):
    with connect() as conn:
        conn.execute(
            "INSERT INTO config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


def del_config(key):
    with connect() as conn:
        conn.execute("DELETE FROM config WHERE key=?", (key,))


def is_allowed(telegram_id):
    owner = get_config("owner")
    if owner and owner == str(telegram_id):
        return True
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM allowed_users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
    return row is not None


def claim_owner(telegram_id):
    if get_config("owner"):
        return False
    set_config("owner", telegram_id)
    return True


def add_user(telegram_id):
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO allowed_users(telegram_id) VALUES(?)",
            (int(telegram_id),),
        )


def remove_user(telegram_id):
    with connect() as conn:
        conn.execute("DELETE FROM allowed_users WHERE telegram_id=?", (int(telegram_id),))


def list_users():
    with connect() as conn:
        rows = conn.execute("SELECT telegram_id FROM allowed_users ORDER BY telegram_id").fetchall()
    return [int(row["telegram_id"]) for row in rows]


def set_state(telegram_id, flow, step, payload=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO user_state(telegram_id,flow,step,payload) VALUES(?,?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
              flow=excluded.flow, step=excluded.step, payload=excluded.payload
            """,
            (int(telegram_id), flow, step, json.dumps(payload or {}, ensure_ascii=False)),
        )


def get_state(telegram_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM user_state WHERE telegram_id=?", (int(telegram_id),)
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.get("payload") or "{}")
    return data


def clear_state(telegram_id):
    with connect() as conn:
        conn.execute("DELETE FROM user_state WHERE telegram_id=?", (int(telegram_id),))


def save_pending(token, telegram_id, target, tracking_code, source_row, choices):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO pending_match(token,telegram_id,target,tracking_code,source_row,choices)
            VALUES(?,?,?,?,?,?)
            """,
            (
                token,
                int(telegram_id),
                target,
                str(tracking_code),
                json.dumps(source_row, ensure_ascii=False),
                json.dumps(choices, ensure_ascii=False),
            ),
        )


def get_pending(token):
    with connect() as conn:
        row = conn.execute("SELECT * FROM pending_match WHERE token=?", (token,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["source_row"] = json.loads(data["source_row"])
    data["choices"] = json.loads(data["choices"])
    return data


def finish_pending(token, status="done"):
    with connect() as conn:
        conn.execute("UPDATE pending_match SET status=? WHERE token=?", (status, token))


init_db()
