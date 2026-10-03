"""Reminder ledger: per-turn tasks, obligations and the tool-touch log.

Pure storage. No network. Keys are per turn (hash of the Hermes turn id), so
the same request text in two turns or two sessions never shares state.
Graphiti holds the long-lived system map; this SQLite file is the working set.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

try:
    from .rules import CHECKLISTS
except ImportError:  # imported as a plain module (tests, flush.py)
    from rules import CHECKLISTS

_LOCK = threading.Lock()
_MIGRATED: set = set()
SCHEMA_VERSION = 1
PRUNE_DAYS = 14


def default_db() -> Path:
    override = os.environ.get("REMINDER_DB")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path.home() / ".hermes"
    directory = home / "reminder"
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    return directory / "ledger.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_hash TEXT PRIMARY KEY,
  summary TEXT NOT NULL,
  verbs TEXT NOT NULL,
  created_at REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS obligations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_hash TEXT NOT NULL,
  kind TEXT NOT NULL,
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  evidence TEXT DEFAULT '',
  created_at REAL NOT NULL,
  resolved_at REAL
);
CREATE TABLE IF NOT EXISTS touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_hash TEXT NOT NULL,
  tool TEXT NOT NULL,
  summary TEXT NOT NULL,
  ok INTEGER NOT NULL,
  created_at REAL NOT NULL
);
"""

_COLUMNS = {
    "tasks": [("session_id", "TEXT DEFAULT ''")],
    "touches": [("ran", "INTEGER NOT NULL DEFAULT 1"),
                ("sig", "TEXT DEFAULT ''"),
                ("result_hash", "TEXT DEFAULT ''")],
}


def _migrate(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version >= SCHEMA_VERSION:
        return
    for table, columns in _COLUMNS.items():
        present = {row[1] for row in con.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in present:
                con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # v0 keyed tasks by message text and never auto-closed; those checks can
    # no longer be matched to a turn, so retire them honestly.
    con.execute("UPDATE obligations SET status='expired' WHERE status='open'")
    con.execute("CREATE INDEX IF NOT EXISTS idx_touches_task ON touches(task_hash, id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_obl_task ON obligations(task_hash, status)")
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    con.commit()


def connect(path=None) -> sqlite3.Connection:
    target = Path(path) if path else default_db()
    con = sqlite3.connect(str(target), timeout=2.0)
    key = str(target)
    if key not in _MIGRATED:
        with _LOCK:
            con.executescript(SCHEMA)
            _migrate(con)
            _MIGRATED.add(key)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return con


def open_task(con, thash: str, session_id: str, summary: str, kinds) -> list:
    """Create the task row if needed and add obligations for new kinds."""
    now = time.time()
    added = []
    with _LOCK:
        row = con.execute("SELECT verbs FROM tasks WHERE task_hash=?",
                          (thash,)).fetchone()
        have = set(row[0].split(",")) if row and row[0] else set()
        new = [k for k in kinds if k not in have and k in CHECKLISTS]
        if row is None:
            con.execute(
                "INSERT INTO tasks (task_hash, summary, verbs, created_at, session_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (thash, summary[:200], ",".join(sorted(kinds)), now, session_id[:80]))
        elif new:
            con.execute("UPDATE tasks SET verbs=? WHERE task_hash=?",
                        (",".join(sorted(have | set(new))), thash))
        for kind in new:
            con.execute(
                "INSERT INTO obligations (task_hash, kind, detail, created_at)"
                " VALUES (?, ?, ?, ?)", (thash, kind, CHECKLISTS[kind], now))
            added.append(kind)
        con.commit()
    return added


def record_touch(con, thash: str, tool: str, summary: str, ok: bool,
                 ran: bool = True, sig: str = "", result_hash: str = "") -> int:
    with _LOCK:
        cur = con.execute(
            "INSERT INTO touches (task_hash, tool, summary, ok, created_at, ran, sig,"
            " result_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (thash, tool[:60], summary[:600], 1 if ok else 0, time.time(),
             1 if ran else 0, sig[:120], result_hash[:16]))
        con.commit()
        return cur.lastrowid


def touches(con, thash: str, limit: int = 400):
    return con.execute(
        "SELECT id, tool, summary, ran, ok FROM touches WHERE task_hash=?"
        " ORDER BY id LIMIT ?", (thash, limit)).fetchall()


def open_obligations(con, thash: str):
    return con.execute(
        "SELECT kind, detail FROM obligations WHERE task_hash=? AND status='open'"
        " ORDER BY id", (thash,)).fetchall()


def active_obligations(con, thash: str):
    """Open and done checks of a turn (done can reopen after a later change)."""
    return con.execute(
        "SELECT kind, detail, status FROM obligations WHERE task_hash=? AND "
        "status IN ('open', 'done') ORDER BY id", (thash,)).fetchall()


def reopen_obligation(con, thash: str, kind: str) -> None:
    with _LOCK:
        con.execute(
            "UPDATE obligations SET status='open', resolved_at=NULL "
            "WHERE task_hash=? AND kind=? AND status='done'", (thash, kind))
        con.commit()


def resolve_obligation(con, thash: str, kind: str, evidence: str) -> None:
    with _LOCK:
        con.execute(
            "UPDATE obligations SET status='done', evidence=?, resolved_at=? "
            "WHERE task_hash=? AND kind=? AND status='open'",
            (evidence[:300], time.time(), thash, kind))
        con.commit()


def prune(con, days: int = PRUNE_DAYS) -> int:
    """Drop old touches of turns that never opened a task."""
    cutoff = time.time() - days * 86400
    with _LOCK:
        cur = con.execute(
            "DELETE FROM touches WHERE created_at < ? AND task_hash NOT IN "
            "(SELECT task_hash FROM tasks)", (cutoff,))
        con.commit()
        return cur.rowcount
