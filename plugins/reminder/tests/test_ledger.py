"""Tests for the reminder ledger (pure storage + rules, no Hermes needed)."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ledger import (connect, detect_verbs, ensure_task, latest_open_task,
                    open_obligations, record_touch, reminder_block,
                    resolve_obligation)


def _db():
    path = Path(tempfile.mkdtemp()) / "test.db"
    return connect(path)


def test_detect_verbs():
    assert detect_verbs("Remove the graffiti plugin") == ["remove"]
    assert set(detect_verbs("Install the API key cron")) == {
        "install", "schedule", "secret"}
    assert detect_verbs("What time is it?") == []


def test_task_creates_obligations_once():
    con = _db()
    thash, new = ensure_task(con, "Remove the graffiti plugin")
    assert thash
    assert new and thash
    assert len(open_obligations(con, thash)) == 1
    _, new2 = ensure_task(con, "Remove the graffiti plugin")
    assert not new2
    assert len(open_obligations(con, thash)) == 1


def test_reminder_block_lists_open_checks():
    con = _db()
    thash, _ = ensure_task(con, "Remove the graffiti plugin")
    assert thash
    block = reminder_block(con, thash)
    assert "[reminder]" in block and "leftovers" in block


def test_resolve_clears_block():
    con = _db()
    thash, _ = ensure_task(con, "Remove the graffiti plugin")
    assert thash
    resolve_obligation(con, thash, "remove", "plugin list no longer shows it")
    assert reminder_block(con, thash) == ""
    assert latest_open_task(con) == thash  # task row stays, checks are done


def test_touches_recorded():
    con = _db()
    thash, _ = ensure_task(con, "Remove the graffiti plugin")
    assert thash
    record_touch(con, thash, "terminal", "rm -rf x", True)
    rows = con.execute("SELECT tool, ok FROM touches").fetchall()
    assert rows == [("terminal", 1)]
