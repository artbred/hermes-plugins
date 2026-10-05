"""Synthetic storage/request-shape fixtures only; no Hermes or external calls.

Run: python3 plugins/proofgate/tests/test_enforcement_storage.py (or pytest).
"""

import copy
import importlib
import multiprocessing
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ledger = importlib.import_module("ledger")
rules = importlib.import_module("rules")


V1_SCHEMA = """
CREATE TABLE tasks (
  task_hash TEXT PRIMARY KEY, summary TEXT NOT NULL, verbs TEXT NOT NULL,
  created_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  session_id TEXT DEFAULT ''
);
CREATE TABLE obligations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_hash TEXT NOT NULL,
  kind TEXT NOT NULL, detail TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  evidence TEXT DEFAULT '', created_at REAL NOT NULL, resolved_at REAL
);
CREATE TABLE touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_hash TEXT NOT NULL,
  tool TEXT NOT NULL, summary TEXT NOT NULL, ok INTEGER NOT NULL,
  created_at REAL NOT NULL, ran INTEGER NOT NULL DEFAULT 1,
  sig TEXT DEFAULT '', result_hash TEXT DEFAULT ''
);
PRAGMA user_version=1;
"""


def test_v1_migration_preserves_obligations_and_tool_history():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "v1.db"
        con = sqlite3.connect(path)
        con.executescript(V1_SCHEMA)
        con.execute("INSERT INTO tasks VALUES ('turn', 'synthetic', 'remove', 1, 'open', 's')")
        con.execute("INSERT INTO obligations VALUES (1, 'turn', 'remove', 'check',"
                    " 'open', '', 1, NULL)")
        con.execute("INSERT INTO obligations VALUES (2, 'turn', 'install', 'check',"
                    " 'done', 'synthetic evidence', 1, 2)")
        con.execute("INSERT INTO touches VALUES (1, 'turn', 'terminal', 'synthetic',"
                    " 1, 1, 1, 'sig', 'hash')")
        con.commit()
        before = {table: con.execute(f"SELECT * FROM {table}").fetchall()
                  for table in ("tasks", "obligations", "touches")}
        con.close()
        con = ledger.connect(path)
        try:
            assert con.execute("PRAGMA user_version").fetchone()[0] == 2
            for table, rows in before.items():
                assert con.execute(f"SELECT * FROM {table}").fetchall() == rows
            assert ledger.open_obligations(con, "turn") == [("remove", "check")]
            ledger.record_evidence(con, "s", "turn", "synthetic", "[redacted]")
            ledger.save_gate(con, "s", "turn", {"active": True})
            assert ledger.claim_retry(con, "s", "turn", 3) == 1
        finally:
            con.close()


def test_evidence_bounds_order_and_session_turn_isolation():
    with tempfile.TemporaryDirectory() as directory:
        con = ledger.connect(Path(directory) / "evidence.db")
        try:
            for index in range(40):
                ledger.record_evidence(con, "s", "t", "x" * 100,
                                       f"synthetic-{index}:" + "R" * 3000)
            ledger.record_evidence(con, "other", "t", "fixture", "other session")
            ledger.record_evidence(con, "s", "other", "fixture", "other turn")
            rows = ledger.read_evidence(con, "s", "t")
            assert len(rows) == 32
            assert [row["result_text"].split(":")[0] for row in rows] == [
                f"synthetic-{index}" for index in range(8, 40)]
            assert all(len(row["result_text"]) == 2000 for row in rows)
            assert all(len(row["tool"]) == 60 for row in rows)
            assert all(row["session_id"] == "s" and row["turn_id"] == "t"
                       and isinstance(row["created_at"], float) for row in rows)
            assert con.execute("SELECT MAX(length(result_text)) FROM evidence").fetchone()[0] == 2000
            assert ledger.read_evidence(con, "missing", "t") == []
            assert ledger.read_evidence(con, "other", "t")[0]["result_text"] == "other session"
            assert ledger.read_evidence(con, "s", "other")[0]["result_text"] == "other turn"
        finally:
            con.close()


def test_gate_state_and_retry_count_persist_independently():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "gate.db"
        con = ledger.connect(path)
        try:
            assert ledger.load_gate(con, "s", "t") is None
            assert ledger.claim_retry(con, "s", "t", 0) is None
            assert ledger.load_gate(con, "s", "t") is None
            assert ledger.claim_retry(con, "s", "t", 3) == 1
            assert ledger.load_gate(con, "s", "t") == {}
            state = {"candidate": "synthetic first wording", "nested": {"active": True}}
            ledger.save_gate(con, "s", "t", state)
            assert ledger.load_gate(con, "s", "t") == state
            state["nested"]["active"] = False
            assert ledger.load_gate(con, "s", "t")["nested"]["active"] is True
            assert ledger.claim_retry(con, "s", "t", 3) == 2
        finally:
            con.close()
        con = ledger.connect(path)
        try:
            replacement = {"candidate": "synthetic rephrased claim", "retry_count": 0}
            ledger.save_gate(con, "s", "t", replacement)
            assert ledger.load_gate(con, "s", "t") == replacement
            assert ledger.claim_retry(con, "s", "t", 3) == 3
            ledger.save_gate(con, "s", "t", {})
            assert ledger.claim_retry(con, "s", "t", 3) is None
            assert ledger.claim_retry(con, "other", "t", 3) == 1
            assert ledger.claim_retry(con, "s", "other", 3) == 1
            assert ledger.load_gate(con, "unknown", "t") is None
            try:
                ledger.save_gate(con, "s", "t", {"huge": "x" * ledger.GATE_STATE_LIMIT})
            except ValueError:
                pass
            else:
                raise AssertionError("oversized JSON must not corrupt or truncate state")
            assert ledger.load_gate(con, "s", "t") == {}
            assert ledger.claim_retry(con, "s", "t", 3) is None
        finally:
            con.close()


def _claim_from_process(args):
    path, index = args
    con = ledger.connect(path)
    try:
        ledger.save_gate(con, "s", "t", {"candidate": f"synthetic wording {index}"})
        return ledger.claim_retry(con, "s", "t", 3)
    finally:
        con.close()


def test_retry_cap_is_atomic_across_process_connections():
    with tempfile.TemporaryDirectory() as directory:
        path = str(Path(directory) / "concurrent.db")
        ledger.connect(path).close()
        # Separate processes do not share ledger._LOCK: SQLite must enforce cap.
        with ProcessPoolExecutor(max_workers=4,
                                 mp_context=multiprocessing.get_context("spawn")) as pool:
            claims = list(pool.map(_claim_from_process, [(path, i) for i in range(16)]))
        assert sorted(value for value in claims if value is not None) == [1, 2, 3]
        assert claims.count(None) == 13
        con = ledger.connect(path)
        try:
            assert ledger.claim_retry(con, "s", "t", 3) is None
            assert con.execute("SELECT retry_count FROM gate_turns").fetchone()[0] == 3
            assert "candidate" in ledger.load_gate(con, "s", "t")
        finally:
            con.close()


def test_review_bounds_upsert_and_fourteen_day_pruning():
    with tempfile.TemporaryDirectory() as directory:
        con = ledger.connect(Path(directory) / "prune.db")
        try:
            for turn in ("old", "fresh"):
                ledger.record_evidence(con, "s", turn, "fixture", "[redacted]")
                ledger.save_gate(con, "s", turn, {"synthetic": True})
                ledger.record_review(con, "s", turn, "C" * 14000, "V" * 100, "R" * 3000)
            row = con.execute("SELECT candidate, verdict, reason FROM gate_reviews"
                              " WHERE turn_id='fresh'").fetchone()
            assert tuple(map(len, row)) == (12000, 60, 2000)
            ledger.record_review(con, "s", "fresh", "replacement", "incomplete", "synthetic")
            assert con.execute("SELECT COUNT(*) FROM gate_reviews").fetchone()[0] == 2
            row = con.execute("SELECT candidate, verdict, reason FROM gate_reviews"
                              " WHERE turn_id='fresh'").fetchone()
            assert row == ("replacement", "incomplete", "synthetic")
            cutoff = time.time() - 15 * 86400
            for table, column in (("evidence", "created_at"), ("gate_turns", "updated_at"),
                                  ("gate_reviews", "updated_at")):
                con.execute(f"UPDATE {table} SET {column}=? WHERE turn_id='old'", (cutoff,))
            con.commit()
            assert ledger.prune(con) == 3
            assert ledger.read_evidence(con, "s", "old") == []
            assert ledger.load_gate(con, "s", "old") is None
            assert len(ledger.read_evidence(con, "s", "fresh")) == 1
            assert ledger.load_gate(con, "s", "fresh") == {"synthetic": True}
            assert con.execute("SELECT turn_id FROM gate_reviews").fetchall() == [("fresh",)]
        finally:
            con.close()


def test_user_texts_cover_chat_anthropic_and_responses_without_tool_evidence():
    history = [
        {"role": "system", "content": "not user"},
        {"role": "user", "content": "synthetic original request"},
        {"role": "assistant", "content": "not user"},
        {"role": "tool", "content": "not user"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "synthetic-1", "content": "not user"},
            {"type": "text", "text": "synthetic latest"},
            {"type": "input_text", "text": "scope"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "[proofgate] excluded"}]},
        {"role": "user", "content": [{"type": "tool_result", "content": "not user"}]},
    ]
    for field in ("messages", "input"):
        request = {field: history}
        before = copy.deepcopy(request)
        assert rules.user_texts(request) == ["synthetic original request", "synthetic latest\nscope"]
        assert rules.latest_user_text(request) == "synthetic latest\nscope"
        assert request == before
    assert rules.user_texts({"input": "synthetic text"}) == ["synthetic text"]
    assert rules.user_texts({"input": [{"type": "input_text", "text": "synthetic direct"}]}) == [
        "synthetic direct"]
    assert rules.user_texts({"input": [{"type": "function_call_output", "output": "not user"}]}) == []
    for empty in (None, [], {}, {"input": " "}, {"messages": [{"role": "user", "content": None}]}):
        assert rules.user_texts(empty) == []
        assert rules.latest_user_text(empty) == ""


def test_responses_injection_preserves_instructions_reasoning_and_call_pairings():
    request = {
        "instructions": "synthetic original system instructions",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "synthetic request"}]},
            {"type": "reasoning", "id": "r1", "summary": [], "encrypted_content": "synthetic"},
            {"type": "function_call", "id": "fc1", "call_id": "c1", "name": "fixture", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "synthetic result"},
        ],
        "previous_response_id": "synthetic-response",
        "store": False,
    }
    before = copy.deepcopy(request)
    for mode in ("codex_responses", "responses"):
        out = rules.inject(request, "[proofgate] synthetic hint", mode)
        assert out is not request
        assert out["input"] is request["input"]
        assert out["instructions"] == request["instructions"] + "\n\n[proofgate] synthetic hint"
        assert {key: value for key, value in out.items() if key != "instructions"} == {
            key: value for key, value in request.items() if key != "instructions"}
        assert request == before
        assert rules.latest_user_text(out) == "synthetic request"
    assert rules.inject({"input": "synthetic string"}, "hint", "codex_responses") == {
        "input": "synthetic string", "instructions": "hint"}
    assert rules.inject({"input": []}, "hint", "codex_responses") is None


if __name__ == "__main__":
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
