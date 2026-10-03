"""Reminder ledger: obligations, touches, and verb checklists.

Pure storage + rules. No network, no Jev calls — fast enough to run inside
middleware on every turn. Graphiti holds the long-lived system map; this
SQLite ledger is the working set (open obligations, recent touches).
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
from pathlib import Path

_LOCK = threading.Lock()


def default_db() -> Path:
    home = Path.home() / ".hermes" / "reminder"
    home.mkdir(parents=True, exist_ok=True)
    return home / "ledger.db"


VERBS = {
    "remove": re.compile(
        r"\b(remove|delete|uninstall|retire|drop|purge|wipe|clean ?up)\b",
        re.IGNORECASE),
    "install": re.compile(
        r"\b(install|add|create|enable|set ?up|deploy|configure)\b",
        re.IGNORECASE),
    "secret": re.compile(
        r"\b(credential|secret|api[-_ ]?key|token|password|auth)\b",
        re.IGNORECASE),
    "schedule": re.compile(
        r"\b(cron|schedule|timer|heartbeat|monitor|watch)\b",
        re.IGNORECASE),
    "update": re.compile(
        r"\b(update|upgrade|migrate|bump)\b",
        re.IGNORECASE),
}

CHECKLISTS = {
    "remove": ("Removal leftovers check: still registered (plugin list)? "
               "still scheduled (cron)? files remain on disk? config "
               "references left? Verify absence, do not just claim it."),
    "install": ("Install wiring check: installed AND enabled AND configured "
                "AND verified working (version/health command output shown)?"),
    "secret": ("Secret hygiene: redacted in outputs? gitignored, never "
               "committed? file permissions private? staged diffs scanned?"),
    "schedule": ("Schedule check: installed in the right scheduler? logging "
                "to a known file? first tick verified, not just assumed?"),
    "update": ("Update check: version before/after shown? dependents still "
              "healthy? rollback path known?"),
}

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


def connect(path=None) -> sqlite3.Connection:
    con = sqlite3.connect(str(path or default_db()))
    con.executescript(SCHEMA)
    return con


def task_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def detect_verbs(text: str):
    return sorted(kind for kind, rx in VERBS.items() if rx.search(text))


def ensure_task(con, text: str, summary: str = ""):
    """Create the task + verb obligations once; return (hash, is_new)."""
    verbs = detect_verbs(text)
    if not verbs:
        return None, False
    thash = task_hash(text)
    with _LOCK:
        row = con.execute("SELECT task_hash FROM tasks WHERE task_hash=?",
                          (thash,)).fetchone()
        if row:
            return thash, False
        now = time.time()
        con.execute(
            "INSERT INTO tasks (task_hash, summary, verbs, created_at) "
            "VALUES (?, ?, ?, ?)",
            (thash, summary[:200] or text[:200], ",".join(verbs), now))
        for verb in verbs:
            con.execute(
                "INSERT INTO obligations (task_hash, kind, detail, created_at)"
                " VALUES (?, ?, ?, ?)",
                (thash, verb, CHECKLISTS[verb], now))
        con.commit()
    return thash, True


def record_touch(con, thash: str, tool: str, summary: str, ok: bool):
    with _LOCK:
        con.execute(
            "INSERT INTO touches (task_hash, tool, summary, ok, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (thash, tool, summary[:300], 1 if ok else 0, time.time()))
        con.commit()


def open_obligations(con, thash: str):
    return con.execute(
        "SELECT kind, detail FROM obligations WHERE task_hash=? AND status='open'",
        (thash,)).fetchall()


def resolve_obligation(con, thash: str, kind: str, evidence: str):
    with _LOCK:
        con.execute(
            "UPDATE obligations SET status='done', evidence=?, resolved_at=? "
            "WHERE task_hash=? AND kind=? AND status='open'",
            (evidence[:300], time.time(), thash, kind))
        con.commit()


def latest_open_task(con):
    row = con.execute(
        "SELECT task_hash FROM tasks WHERE status='open' "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    return row[0] if row else None


def reminder_block(con, thash: str, max_lines: int = 8) -> str:
    items = open_obligations(con, thash)
    if not items:
        return ""
    lines = ["[reminder] Open checks for this task — verify with evidence, "
             "do not just claim:"]
    for kind, detail in items[:max_lines]:
        lines.append(f"- ({kind}) {detail}")
    return "\n".join(lines)
