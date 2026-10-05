"""Proofgate ledger: per-turn obligations, tool evidence and completion gates.

Pure storage. No network. Keys are per turn (hash of the Hermes turn id), so
the same request text in two turns or two sessions never shares state.
Nothing leaves this machine: the file is the only store.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

try:
    from .rules import CHECKLISTS
except ImportError:  # imported as a plain module (tests)
    from rules import CHECKLISTS

_LOCK = threading.Lock()
_MIGRATED: set = set()
SCHEMA_VERSION = 2
PRUNE_DAYS = 14
EVIDENCE_LIMIT = 32
RESULT_TEXT_LIMIT = 2000
CANDIDATE_LIMIT = 12000
REASON_LIMIT = 2000
GATE_STATE_LIMIT = 32768
IDENTIFIER_LIMIT = 1024


def default_db() -> Path:
    override = os.environ.get("PROOFGATE_DB")
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        home = Path.home() / ".hermes"
    directory = home / "proofgate"
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
CREATE TABLE IF NOT EXISTS evidence (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  tool TEXT NOT NULL,
  result_text TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_turn
  ON evidence(session_id, turn_id, id);
CREATE TABLE IF NOT EXISTS gate_turns (
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT '{}',
  retry_count INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL,
  PRIMARY KEY (session_id, turn_id)
);
CREATE TABLE IF NOT EXISTS gate_reviews (
  session_id TEXT NOT NULL,
  turn_id TEXT NOT NULL,
  candidate TEXT NOT NULL,
  verdict TEXT NOT NULL,
  reason TEXT NOT NULL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (session_id, turn_id)
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
    if version < 1:
        # v0 keyed tasks by message text and never auto-closed; those checks
        # cannot be matched to a turn. v1 obligations remain valid in v2.
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


def _gate_key(session_id: str, turn_id: str) -> tuple:
    # Reject oversized keys rather than truncating them into another turn.
    if any(not isinstance(key, str) or len(key) > IDENTIFIER_LIMIT
           for key in (session_id, turn_id)):
        raise ValueError("gate identifiers must be bounded strings")
    return session_id, turn_id


def record_evidence(con, session_id: str, turn_id: str, tool: str,
                    result_text: str) -> int:
    """Store already-redacted tool output; redaction belongs to the caller."""
    key = _gate_key(session_id, turn_id)
    with _LOCK, con:
        cur = con.execute(
            "INSERT INTO evidence (session_id, turn_id, tool, result_text, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (*key, tool[:60], result_text[:RESULT_TEXT_LIMIT], time.time()))
        return cur.lastrowid


def read_evidence(con, session_id: str, turn_id: str) -> list:
    """Return the newest bounded result window in execution order."""
    key = _gate_key(session_id, turn_id)
    rows = con.execute(
        "SELECT id, session_id, turn_id, tool, result_text, created_at FROM evidence"
        " WHERE session_id=? AND turn_id=? ORDER BY id DESC LIMIT ?",
        (*key, EVIDENCE_LIMIT)).fetchall()
    fields = ("id", "session_id", "turn_id", "tool", "result_text", "created_at")
    return [dict(zip(fields, (*row[:4], row[4][:RESULT_TEXT_LIMIT], row[5])))
            for row in reversed(rows)]


def load_gate(con, session_id: str, turn_id: str) -> dict | None:
    """Read the gate payload, never mixing the durable retry counter into it."""
    row = con.execute(
        "SELECT state FROM gate_turns WHERE session_id=? AND turn_id=?",
        _gate_key(session_id, turn_id)).fetchone()
    return json.loads(row[0]) if row is not None else None


def save_gate(con, session_id: str, turn_id: str, state: dict) -> None:
    """Replace JSON state without resetting retries, even for a new candidate."""
    key = _gate_key(session_id, turn_id)
    if not isinstance(state, dict):
        raise ValueError("gate state must be a JSON object")
    encoded = json.dumps(state, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False)
    if len(encoded) > GATE_STATE_LIMIT:
        raise ValueError("gate state exceeds storage limit")
    with _LOCK, con:
        con.execute(
            "INSERT INTO gate_turns (session_id, turn_id, state, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(session_id, turn_id)"
            " DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at",
            (*key, encoded, time.time()))


def claim_retry(con, session_id: str, turn_id: str, limit: int) -> int | None:
    """Atomically reserve one retry; return its 1-based count or None at cap."""
    key = _gate_key(session_id, turn_id)
    if limit <= 0:
        return None
    with _LOCK, con:
        row = con.execute(
            "INSERT INTO gate_turns (session_id, turn_id, retry_count, updated_at)"
            " VALUES (?, ?, 1, ?) ON CONFLICT(session_id, turn_id) DO UPDATE"
            " SET retry_count=gate_turns.retry_count+1,"
            " updated_at=excluded.updated_at WHERE gate_turns.retry_count < ?"
            " RETURNING retry_count",
            (*key, time.time(), limit)).fetchone()
        return row[0] if row is not None else None


def record_review(con, session_id: str, turn_id: str, candidate: str,
                  verdict: str, reason: str) -> None:
    """Retain the latest bounded, caller-redacted review for this exact turn."""
    key = _gate_key(session_id, turn_id)
    with _LOCK, con:
        con.execute(
            "INSERT INTO gate_reviews"
            " (session_id, turn_id, candidate, verdict, reason, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(session_id, turn_id)"
            " DO UPDATE SET candidate=excluded.candidate, verdict=excluded.verdict,"
            " reason=excluded.reason, updated_at=excluded.updated_at",
            (*key, candidate[:CANDIDATE_LIMIT], verdict[:60],
             reason[:REASON_LIMIT], time.time()))


def prune(con, days: int = PRUNE_DAYS) -> int:
    """Drop orphan touches and expire evidence/gate data after fourteen days."""
    cutoff = time.time() - days * 86400
    with _LOCK:
        cur = con.execute(
            "DELETE FROM touches WHERE created_at < ? AND task_hash NOT IN "
            "(SELECT task_hash FROM tasks)", (cutoff,))
        count = cur.rowcount
        for table, column in (("evidence", "created_at"),
                              ("gate_turns", "updated_at"),
                              ("gate_reviews", "updated_at")):
            count += con.execute(
                f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)).rowcount
        con.commit()
        return count
