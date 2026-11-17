"""Audit locale (SQLite): chi ha fatto cosa, indipendente da GLPI."""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

from .config import settings

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(settings.db_path) or ".", exist_ok=True)
    c = sqlite3.connect(settings.db_path)
    c.execute(
        "CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, username TEXT NOT NULL,"
        " display_name TEXT, action TEXT NOT NULL, detail TEXT NOT NULL)"
    )
    return c


def log(username: str, display_name: str, action: str, detail: dict) -> None:
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO audit (ts, username, display_name, action, detail) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), username, display_name, action,
             json.dumps(detail, ensure_ascii=False)),
        )


def recent(limit: int = 50) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT ts, username, display_name, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(ts=r[0], username=r[1], display_name=r[2], action=r[3], detail=json.loads(r[4])) for r in rows]
