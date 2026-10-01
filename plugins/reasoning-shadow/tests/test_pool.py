"""All histories, requests, IDs and credentials here are synthetic fixtures."""
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pool_module = load(ROOT / "pool.py", "pool_test_module")
cli = load(ROOT / "scripts" / "pool_cli.py", "pool_cli_test_module")
Pool = pool_module.Pool


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "active-home"))
    def reject(*args, **kwargs):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(socket, "create_connection", reject)


@pytest.fixture
def pool(tmp_path):
    instance = Pool(tmp_path / "home")
    yield instance
    instance.close()


def record(handle="synthetic:one", request="synthetic repair database request", **updates):
    stamp = time.time() - 10
    result = {"source_handle": handle, "request": request, "captured_at": stamp,
              "source_at": stamp, "generation_cutoff": stamp,
              "synthetic": True, "provenance": "authenticated_human:cli",
              "recent_context": [], "status": "captured", "prediction": {}, "actual": {}}
    result.update(updates)
    return result


def rows(pool):
    with sqlite3.connect(pool.path) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT record FROM examples ORDER BY id")]


def test_private_modes_wal_and_dedupe(pool):
    assert pool.insert(record())
    assert not pool.insert(record(request="do not replace live observation"))
    assert rows(pool)[0]["request"] == "synthetic repair database request"
    assert stat.S_IMODE(pool.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(pool.path.stat().st_mode) == 0o600
    with pool._connection() as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(pool.path) + suffix)
            assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert pool.report()["total"] == 1


def test_insert_never_asserts_outcome(pool):
    assert pool.insert(record(outcome="verified_success", outcome_at=time.time() - 100))
    assert pool.report()["by_outcome"]["unknown"] == 1
    assert pool.similar("database", time.time(), "", include_synthetic=True)[0]["outcome"] == "unknown"
    with pytest.raises(ValueError):
        pool.update("synthetic:one", {"outcome": "verified_success"})
    with pytest.raises(ValueError):
        pool.annotate("synthetic:one", "verified_success", source="model")
    with pytest.raises(ValueError):
        pool.annotate("synthetic:one", "probably_success")
    assert not pool.annotate("nonexistent", "failed")


@pytest.mark.parametrize("status", ["verified_success", "failed", "corrected", "unknown"])
def test_annotation_as_of_cutoff(pool, status, monkeypatch):
    pool.insert(record())
    before = time.time()
    monkeypatch.setattr(pool_module.time, "time", lambda: before + 10)
    assert pool.annotate("synthetic:one", status)
    assert pool.similar("database", before, "", include_synthetic=True)[0]["outcome"] == "unknown"
    assert pool.similar("database", before + 10, "", include_synthetic=True)[0]["outcome"] == status


def test_causal_cutoffs_current_exclusion_and_no_generated_fields(pool):
    cutoff = time.time()
    pool.insert(record("synthetic:past", actual={"output": "future output"}, prediction={"reasoning": "future secret reasoning"}))
    pool.insert(record("synthetic:source_future", source_at=cutoff))
    pool.insert(record("synthetic:capture_future", captured_at=cutoff))
    pool.insert(record("synthetic:current"))
    found = pool.similar("database", cutoff, "synthetic:current", include_synthetic=True)
    assert [item["source_handle"] for item in found] == ["synthetic:past"]
    assert "actual" not in found[0] and "prediction" not in found[0]
    assert "future output" not in json.dumps(found)


def test_updated_status_not_visible_before_update(pool):
    pool.insert(record())
    before = time.time()
    assert pool.update("synthetic:one", {"status": "predicted", "actual": {"tokens": 22}})
    assert pool.similar("database", before, "", include_synthetic=True)[0]["status"] == "captured"
    assert not pool.update("missing", {"status": "predicted"})


def test_failures_corrections_and_unknown_are_all_selected(pool):
    for i, outcome in enumerate(("failed", "corrected", "verified_success", "unknown")):
        handle = f"synthetic:{i}"
        pool.insert(record(handle))
        if outcome != "unknown":
            pool.annotate(handle, outcome)
    assert {x["outcome"] for x in pool.similar("database", time.time() + 1, "", include_synthetic=True)} == pool_module.OUTCOMES


def test_unicode_fts_indexes_only_request(pool):
    pool.insert(record(request="Почини базу данных и SQLite", recent_context=[
        {"role": "system", "content": "unfindable_system"},
        {"role": "tool", "content": "unfindable_tool"},
        {"role": "assistant", "content": "<think>unfindable_reasoning</think>visible prior reply"},
        {"role": "user", "content": "unfindable_context"}]))
    assert len(pool.similar("базу", time.time(), "", include_synthetic=True)) == 1
    assert len(pool.similar("sqlite", time.time(), "", include_synthetic=True)) == 1
    assert pool.similar('" OR database NOT * : ^', time.time(), "", include_synthetic=True) == []
    for word in ("unfindable_system", "unfindable_tool", "unfindable_reasoning", "unfindable_context"):
        assert pool.similar(word, time.time(), "", include_synthetic=True) == []
    assert "unfindable_reasoning" not in json.dumps(rows(pool))
    assert rows(pool)[0]["recent_context"][0]["content"] == "visible prior reply"


def test_full_request_redaction_and_no_truncation(pool):
    secret = "sk-" + "SyntheticTestCredential" * 3
    text = "Beginning " + "private note " * 500 + "END " + secret
    assert pool.insert(record(request=text))
    stored = rows(pool)[0]["request"]
    assert secret not in stored
    assert stored.startswith("Beginning ") and "END " in stored
    assert stored.count("private note") == 500
    assert not pool.insert(record("synthetic:too_big", request="z" * 101000))
    assert pool.report()["total"] == 1


def test_selection_skips_oversize_whole_not_truncate(pool):
    pool.insert(record("synthetic:big", request="database " * 400))
    pool.insert(record("synthetic:small", request="database small"))
    found = pool.similar("database", time.time(), "", max_characters=1200, include_synthetic=True)
    assert len(found) == 1 and found[0]["request"] == "database small"


def test_retention_and_fts_delete_atomic(tmp_path):
    pool = Pool(tmp_path / "limited", max_examples=2, retention_days=1)
    now = time.time()
    assert not pool.insert(record("synthetic:old", request="oldword", captured_at=now - 100000, source_at=now - 100000))
    for i in range(3):
        pool.insert(record(f"synthetic:{i}", request=f"fixtureword{i}", captured_at=now - 5 + i))
    assert pool.report()["total"] == 2
    assert pool.similar("fixtureword0", now + 1, "", include_synthetic=True) == []
    with sqlite3.connect(pool.path) as db:
        assert db.execute("SELECT count(*) FROM requests_fts").fetchone()[0] == 2
    pool.close()


def test_metadata_report_no_content(pool):
    pool.insert(record(request="PRIVATE_SYNTHETIC_CONTENT", status="private-status-is-not-code"))
    pool.record_import({"imported": 2, "duplicates": 1})
    report = pool.report()
    assert "PRIVATE_SYNTHETIC_CONTENT" not in json.dumps(report)
    assert "synthetic:one" not in json.dumps(report)
    assert report["by_prediction_status"] == {"unknown": 1}
    assert report["import_counts"] == {"imported": 2, "duplicates": 1}


def test_failure_reload_profile_isolation_and_metadata_validation(tmp_path):
    a = Pool(tmp_path / "a")
    a.failure_set({"count": 3, "until": time.time() + 60})
    a.close()
    a = Pool(tmp_path / "a")
    b = Pool(tmp_path / "b")
    assert a.failure_get()["count"] == 3
    assert b.failure_get() == {}
    with pytest.raises(ValueError):
        a.failure_set({"error": "private raw provider error"})
    a.close()
    b.close()


def test_thread_local_connections(pool):
    def insert(i):
        return pool.insert(record(f"synthetic:{i}"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(insert, range(40)))
    assert pool.report()["total"] == 40
    pool.close()
    with pytest.raises(RuntimeError):
        pool.report()


def test_symlink_storage_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / "reasoning-shadow").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        Pool(home)


def history_db(tmp_path, actual_schema=False):
    source = tmp_path / "synthetic-state.db"
    db = sqlite3.connect(source)
    if actual_schema:
        from hermes_state_common import SCHEMA_SQL
        db.executescript(SCHEMA_SQL)
    else:
        db.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,source TEXT,parent_session_id TEXT,origin_json TEXT,profile_name TEXT,model_config TEXT);
            CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,role TEXT,content TEXT,timestamp REAL,
                active INTEGER DEFAULT 1,display_kind TEXT,display_metadata TEXT,tool_calls TEXT,tool_call_id TEXT,reasoning TEXT,_compressed_summary INTEGER DEFAULT 0);
        """)
    db.execute("INSERT INTO sessions(id,source) VALUES('synthetic-human-session','telegram')") if not actual_schema else db.execute("INSERT INTO sessions(id,source,started_at) VALUES('synthetic-human-session','telegram',?)", (time.time() - 100,))
    return source, db


def add_message(db, i, role, content, stamp, sid="synthetic-human-session", **metadata):
    values = {"id": i, "session_id": sid, "role": role, "content": content, "timestamp": stamp, **metadata}
    db.execute(f"INSERT INTO messages({','.join(values)}) VALUES({','.join('?' for _ in values)})", tuple(values.values()))


@pytest.mark.parametrize("actual_schema", [False, True])
def test_history_readonly_unknown_outcomes_context_and_dedupe(tmp_path, actual_schema):
    source, db = history_db(tmp_path, actual_schema)
    now = time.time()
    add_message(db, 1, "system", "hidden system", now - 90)
    add_message(db, 2, "user", "original synthetic question", now - 80)
    add_message(db, 3, "assistant", "<think>hidden reasoning</think>visible previous answer", now - 70, reasoning="never export this")
    add_message(db, 4, "tool", "private tool output", now - 60)
    add_message(db, 5, "user", "[internal notification] A human quoting machine-looking text", now - 50)
    add_message(db, 6, "assistant", "FUTURE CURRENT ANSWER", now - 40)
    db.commit()
    db.close()
    original = source.read_bytes()
    mtime = source.stat().st_mtime_ns
    home = tmp_path / "private-home"
    result = cli.import_history(home, source)
    assert result["imported"] == 2 and result["scanned"] == 2
    assert source.read_bytes() == original and source.stat().st_mtime_ns == mtime
    pool = Pool(home)
    request = next(x for x in rows(pool) if x["source_at"] == now - 50)
    assert request["outcome"] == "unknown"
    assert request["request"].startswith("[internal notification]")
    context = json.dumps(request["recent_context"])
    assert "visible previous answer" in context and "original synthetic question" in context
    assert all(word not in context for word in ("hidden", "private tool", "FUTURE"))
    assert "synthetic-human-session" not in request["source_handle"]
    again = cli.import_history(home, source)
    assert again["duplicates"] == 2 and again["imported"] == 0
    assert pool.report()["total"] == 2
    pool.close()


def test_history_authorization_metadata_and_strict_timestamp(tmp_path):
    source, db = history_db(tmp_path)
    now = time.time()
    for sid, kind, parent, origin in [
        ("child", "telegram", "synthetic-human-session", None),
        ("cron", "cron", None, None), ("weak", None, None, None),
        ("internal-origin", "telegram", None, '{"internal_event":true}'),
        ("unknown", "unproven_api_source", None, None),
    ]:
        db.execute("INSERT INTO sessions(id,source,parent_session_id,origin_json) VALUES(?,?,?,?)", (sid, kind, parent, origin))
    for i, sid in enumerate(("child", "cron", "weak", "internal-origin", "unknown"), 1):
        add_message(db, i, "user", "excluded synthetic request", now - 5, sid=sid)
    add_message(db, 6, "user", "internal notification", now - 4, display_kind="internal_notification")
    add_message(db, 7, "user", "flag internal", now - 3, display_metadata='{"is_internal":true}')
    add_message(db, 8, "assistant", "SAME_TIMESTAMP_NOT_CONTEXT", now - 2)
    add_message(db, 9, "user", "human quotation of cron and subagent", now - 2)
    add_message(db, 10, "user", "FUTURE_SOURCE", now + 100)
    db.commit()
    db.close()
    result = cli.import_history(tmp_path / "home", source)
    assert result["imported"] == 1
    assert result["excluded_internal"] == 4
    # Unknown/non-Telegram and future sources are filtered in SQL, before
    # loading content or consuming the bounded Telegram candidate budget.
    assert result["excluded_unknown"] == 0
    assert result["skipped_invalid"] == 0
    pool = Pool(tmp_path / "home")
    assert rows(pool)[0]["recent_context"] == []
    pool.close()


def test_history_limits_defaults_and_no_cross_profile_discovery(tmp_path, monkeypatch):
    home = tmp_path / "active"
    home.mkdir()
    source, db = history_db(tmp_path)
    for i in range(1, 4):
        add_message(db, i, "user", f"synthetic request {i}", time.time() - 10 + i)
    db.commit()
    db.close()
    source.rename(home / "state.db")
    other = home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "state.db").write_bytes(b"not a valid sqlite file")
    assert cli.import_history(home, limit=2)["imported"] == 2
    assert not (other / "reasoning-shadow").exists()
    for limit in (0, 1501, True):
        with pytest.raises(ValueError):
            cli.import_history(home, limit=limit)


def test_cli_report_annotation_import_and_fixed_errors(tmp_path, capsys):
    home = tmp_path / "home"
    pool = Pool(home)
    pool.insert(record())
    pool.close()
    assert cli.main(["--home", str(home), "report"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["total"] == 1
    assert cli.main(["annotate", "synthetic:one", "--outcome", "corrected", "--home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "outcome": "corrected"}
    pool = Pool(home)
    assert pool.report()["by_outcome"]["corrected"] == 1
    pool.close()
    assert cli.main(["import-history", "--home", str(home), "--source", str(tmp_path / "PRIVATE_SOURCE_PATH")]) == 1
    output = capsys.readouterr().out
    assert "PRIVATE_SOURCE_PATH" not in output
    assert json.loads(output)["error"] == "pool_operation_failed"


def test_history_connection_is_readonly_and_closed(tmp_path, monkeypatch):
    source, db = history_db(tmp_path)
    add_message(db, 1, "user", "synthetic import request", time.time() - 10)
    db.commit()
    db.close()
    original_connect = sqlite3.connect
    source_connections = []
    def connect(database, *args, **kwargs):
        result = original_connect(database, *args, **kwargs)
        if str(database).startswith("file:"):
            assert "mode=ro" in str(database) and kwargs["uri"] is True
            with pytest.raises(sqlite3.OperationalError):
                result.execute("INSERT INTO sessions(id,source) VALUES('forbidden','cli')")
            result.rollback()
            source_connections.append(result)
        return result
    monkeypatch.setattr(cli.sqlite3, "connect", connect)
    assert cli.import_history(tmp_path / "home", source)["imported"] == 1
    assert len(source_connections) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        source_connections[0].execute("SELECT 1")


def test_cli_argument_errors_never_echo_raw_input(tmp_path):
    private = "PRIVATE_SYNTHETIC_ARGUMENT"
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "pool_cli.py"), "--home", str(tmp_path / "home"), "import-history", "--limit", private], capture_output=True, text=True)
    assert result.returncode == 2
    assert private not in result.stdout + result.stderr
    assert json.loads(result.stdout)["error"] == "pool_arguments_invalid"


def test_context_last_six_visible_messages_preserves_human_quotation(pool):
    context = [{"role": "user", "content": f"visible {i}"} for i in range(8)]
    context += [{"role": "tool", "content": "tool output"}] * 6
    context += [{"role": "user", "content": "A human quotes <think>not secret reasoning</think>"}]
    assert pool.insert(record(recent_context=context))
    stored = rows(pool)[0]["recent_context"]
    assert len(stored) == 6
    assert stored[0]["content"] == "visible 3"
    assert stored[-1]["content"] == context[-1]["content"]


def test_standalone_cli_entrypoint_does_not_import_plugin(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "pool_cli.py"), "--home", str(tmp_path / "home"), "report"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["total"] == 0


def test_reannotations_preserve_historical_failure_and_status(pool, monkeypatch):
    pool.insert(record())
    now = time.time()
    monkeypatch.setattr(pool_module.time, "time", lambda: now)
    assert pool.annotate("synthetic:one", "failed")
    assert pool.update("synthetic:one", {"status": "predicted"})
    monkeypatch.setattr(pool_module.time, "time", lambda: now + 10)
    assert pool.annotate("synthetic:one", "corrected")
    assert pool.update("synthetic:one", {"status": "expired"})
    monkeypatch.setattr(pool_module.time, "time", lambda: now + 20)
    assert pool.annotate("synthetic:one", "verified_success")
    assert pool.similar("database", now + 5, "", include_synthetic=True)[0]["outcome"] == "failed"
    assert pool.similar("database", now + 5, "", include_synthetic=True)[0]["status"] == "predicted"
    assert pool.similar("database", now + 15, "", include_synthetic=True)[0]["outcome"] == "corrected"
    assert pool.similar("database", now + 25, "", include_synthetic=True)[0]["outcome"] == "verified_success"
    reopened = Pool(pool.path.parent.parent)
    assert reopened.similar("database", now + 5, "", include_synthetic=True)[0]["outcome"] == "failed"
    reopened.close()


def test_annotation_history_pruned_with_examples(tmp_path):
    pool = Pool(tmp_path / "home", max_examples=1)
    pool.insert(record("synthetic:old"))
    pool.annotate("synthetic:old", "failed")
    pool.insert(record("synthetic:new", captured_at=time.time() - 1))
    with pool._connection() as db:
        assert db.execute("SELECT count(*) FROM annotation_history").fetchone()[0] == 1
    pool.close()


def test_diagnostic_hash_and_character_count_preserved(pool):
    digest = hashlib.sha256(b"original oversized synthetic request").hexdigest()
    assert pool.insert(record(request="", request_hash=digest, request_characters=101000, status="oversize"))
    stored = rows(pool)[0]
    assert stored["request_hash"] == digest and stored["request_characters"] == 101000
    assert digest != hashlib.sha256(b"").hexdigest()
    for field, bad in (("request_hash", "malformed"), ("request_hash", digest.upper()), ("request_characters", True), ("request_characters", -1)):
        invalid = record("synthetic:bad")
        invalid[field] = bad
        with pytest.raises(ValueError):
            pool.insert(invalid)


def test_report_effort_comparison_aggregates_are_content_free(pool):
    pool.insert(record("synthetic:match", status="predicted", prediction={"main_effort": "high", "model": "PRIVATE_MODEL"}, actual={"wire_effort": "high", "model_ref": "PRIVATE_HANDLE", "duration_seconds": 1.5, "input_tokens": 100, "output_tokens": 30}))
    pool.insert(record("synthetic:mismatch", status="predicted", prediction={"main_effort": "low"}, actual={"wire_effort": "high", "duration_seconds": 2.5, "input_tokens": 200}))
    pool.insert(record("synthetic:past", status="history_unknown"))
    pool.insert(record("synthetic:invalid", status="invalid_prediction", prediction={"main_effort": "PRIVATE_EFFORT"}))
    pool.insert(record("synthetic:unknown", status="PRIVATE_STATUS", actual={"wire_effort": "PRIVATE_WIRE", "input_tokens": True, "output_tokens": -1}))
    report = pool.report()
    assert report["by_actual_wire_effort"] == {"high": 2, "unknown": 3}
    assert report["by_recommended_effort"] == {"high": 1, "low": 1, "unknown": 3}
    assert report["prediction_comparison"] == {"matched": 1, "mismatched": 1, "unknown": 3}
    assert report["prediction_counts"] == {"past": 1, "invalid": 1, "unknown": 1}
    assert report["usage"]["duration_seconds"] == {"count": 2, "sum": 4.0}
    assert report["usage"]["input_tokens"] == {"count": 2, "sum": 300}
    assert report["by_outcome"]["unknown"] == 5
    assert "PRIVATE" not in json.dumps(report) and "synthetic:" not in json.dumps(report)
    pool.record_import({"private_model": 5, "imported": 2})
    assert pool.report()["import_counts"] == {"imported": 2}


def test_history_never_reads_hidden_reasoning_or_model_config(tmp_path, monkeypatch):
    source, db = history_db(tmp_path)
    db.execute("UPDATE sessions SET model_config=?", ('{"api_key":"SYNTHETIC_FORBIDDEN"}',))
    now = time.time()
    add_message(db, 1, "assistant", "visible reply", now - 20, reasoning="FORBIDDEN_REASONING")
    add_message(db, 2, "user", "safe synthetic request", now - 10, reasoning="FORBIDDEN_REASONING")
    db.commit()
    db.close()
    original_connect = sqlite3.connect
    reads = []
    def connect(database, *args, **kwargs):
        result = original_connect(database, *args, **kwargs)
        if str(database).startswith("file:"):
            def authorize(action, table, column, *unused):
                if action == sqlite3.SQLITE_READ:
                    reads.append((table, column))
                    if column in {"reasoning", "reasoning_content", "model_config"}:
                        return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            result.set_authorizer(authorize)
        return result
    monkeypatch.setattr(cli.sqlite3, "connect", connect)
    assert cli.import_history(tmp_path / "home", source)["imported"] == 1
    assert reads and not any(column in {"reasoning", "model_config"} for _, column in reads)


def test_history_default_telegram_primary_profile_and_age_before_limit(tmp_path):
    source, db = history_db(tmp_path)
    now = time.time()
    sessions = (("default-profile", "telegram", "default"), ("other-profile", "telegram", "other"), ("cli-session", "cli", "default"))
    for sid, kind, profile in sessions:
        db.execute("INSERT INTO sessions(id,source,profile_name) VALUES(?,?,?)", (sid, kind, profile))
    add_message(db, 1, "user", "original inactive telegram", now - 10, active=0)
    add_message(db, 2, "user", "default telegram", now - 9, sid="default-profile")
    add_message(db, 3, "user", "other profile excluded", now - 1, sid="other-profile")
    add_message(db, 4, "user", "cli opt-in only", now - 1, sid="cli-session")
    add_message(db, 5, "user", "expired excluded", now - 91 * 86400)
    add_message(db, 6, "user", "future excluded", now + 100)
    db.commit()
    db.close()
    home = tmp_path / "home"
    assert cli.import_history(home, source, limit=2)["imported"] == 2
    pool = Pool(home)
    assert {item["request"] for item in rows(pool)} == {"original inactive telegram", "default telegram"}
    pool.close()
    assert cli.import_history(tmp_path / "cli-home", source, sources=("cli",))["imported"] == 1
    named = tmp_path / "profiles" / "other"
    assert cli.import_history(named, source)["imported"] == 1
    pool = Pool(named)
    assert rows(pool)[0]["request"] == "other profile excluded"
    pool.close()


@pytest.mark.parametrize("prefix", cli.LEGACY_GENERATED_PREFIXES)
def test_legacy_envelope_requires_unknown_provenance(prefix):
    message = {"role": "user", "content": prefix + "] synthetic envelope", "display_kind": None}
    assert not cli._message_allowed(message)
    assert cli._message_allowed({**message, "display_metadata": '{"gateway_input_owner":"human"}'})
    assert cli._message_allowed({**message, "display_kind": "user"})
    assert cli._message_allowed({**message, "content": "Human quotes " + message["content"]})
    assert cli._message_allowed({**message, "content": message["content"].lower()})


@pytest.mark.parametrize("flag", ["_verification_nudge", "_pre_verify_hook_continue"])
def test_verifier_generated_flags_are_excluded(flag):
    assert not cli._message_allowed({"role": "user", "content": "generated continuation", "display_metadata": json.dumps({flag: True})})


def test_context_last_six_visible_beyond_many_internal_rows(tmp_path):
    source, db = history_db(tmp_path)
    now = time.time()
    for i in range(1, 9):
        add_message(db, i, "assistant", f"prior visible {i}", now - 1000 + i, active=0)
    for i in range(9, 160):
        add_message(db, i, "assistant", "internal continuation", now - 900 + i, display_kind="internal_notification")
    add_message(db, 160, "user", "same session request", now - 10)
    add_message(db, 161, "assistant", "future reply", now - 5)
    db.commit()
    db.close()
    home = tmp_path / "home"
    assert cli.import_history(home, source)["imported"] == 1
    pool = Pool(home)
    assert [item["content"] for item in rows(pool)[0]["recent_context"]] == [f"prior visible {i}" for i in range(3, 9)]
    pool.close()


def test_redactor_fails_closed_without_installed_boundary(monkeypatch):
    monkeypatch.setitem(sys.modules, "agent.redact", None)
    with pytest.raises(ImportError):
        pool_module.redact("arbitrary credential-shaped text")


def test_redactor_forces_host_secret_and_url_credential_boundary(monkeypatch):
    import agent.redact
    calls = []
    def redact_sensitive_text(text, **options):
        calls.append(options)
        return "redacted synthetic content"
    monkeypatch.setattr(agent.redact, "redact_sensitive_text", redact_sensitive_text)
    assert pool_module.redact("synthetic content") == "redacted synthetic content"
    assert calls == [{"force": True, "redact_url_credentials": True}]


def test_legacy_latest_only_migration_does_not_invent_past_labels(pool):
    pool.insert(record())
    now = time.time()
    pool.annotate("synthetic:one", "failed")
    with pool._connection() as db:
        data = json.loads(db.execute("SELECT record FROM examples").fetchone()[0])
        data["status"] = "predicted"
        data["metadata_at"] = now + 10
        db.execute("UPDATE examples SET record=?,outcome_at=?", (json.dumps(data), now + 10))
        db.execute("DELETE FROM annotation_history")
    reopened = Pool(pool.path.parent.parent)
    assert reopened.similar("database", now + 5, "", include_synthetic=True)[0]["status"] == "unknown"
    assert reopened.similar("database", now + 5, "", include_synthetic=True)[0]["outcome"] == "unknown"
    assert reopened.similar("database", now + 15, "", include_synthetic=True)[0]["status"] == "predicted"
    assert reopened.similar("database", now + 15, "", include_synthetic=True)[0]["outcome"] == "failed"
    reopened.close()


def test_cli_explicit_source_opt_in(tmp_path, capsys):
    source, db = history_db(tmp_path)
    db.execute("UPDATE sessions SET source='cli'")
    add_message(db, 1, "user", "synthetic cli only request", time.time() - 10)
    db.commit()
    db.close()
    arguments = ["import-history", "--home", str(tmp_path / "home"), "--source", str(source)]
    assert cli.main(arguments) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 0
    assert cli.main(arguments + ["--sources", "cli"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == 1


def test_similarity_excludes_synthetic_by_default(pool):
    pool.insert(record("synthetic:fixture"))
    pool.insert(record("human:fixture", synthetic=False))
    stamp = time.time()
    assert [item["source_handle"] for item in pool.similar("database", stamp, "")] == ["human:fixture"]
    assert {item["source_handle"] for item in pool.similar("database", stamp, "", include_synthetic=True)} == {"human:fixture", "synthetic:fixture"}
    assert pool.similar("database", stamp, "human:fixture", include_synthetic=False) == []
    for invalid in (1, None, "true"):
        with pytest.raises(ValueError, match="invalid_selection"):
            pool.similar("database", stamp, "", include_synthetic=invalid)


def test_cancelled_initialization_never_creates_storage(tmp_path):
    lifecycle = pool_module.WriteLifecycle()
    lifecycle.cancel()
    home = tmp_path / "must-not-be-created"
    with pytest.raises(RuntimeError, match="^pool_cancelled$"):
        Pool(home, lifecycle=lifecycle)
    assert not home.exists()


def snapshot(path):
    with sqlite3.connect(path) as db:
        return {table: db.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
                for table in ("examples", "requests_fts", "annotation_history", "metadata")}


@pytest.mark.parametrize("operation, sql", [
    ("insert", "INSERT INTO annotation_history"),
    ("update", "INSERT INTO annotation_history"),
    ("annotate", "INSERT INTO annotation_history"),
    ("failure_set", "INSERT OR REPLACE INTO metadata"),
    ("record_import", "INSERT OR REPLACE INTO metadata"),
    ("report", "DELETE FROM examples WHERE id NOT IN"),
    ("similar", "DELETE FROM examples WHERE id NOT IN"),
])
def test_cancel_rolls_back_blocked_mutation_without_waiting(tmp_path, monkeypatch, operation, sql):
    lifecycle = pool_module.WriteLifecycle()
    instance = Pool(tmp_path / "home", lifecycle=lifecycle)
    instance.insert(record())
    instance.failure_set({"count": 1})
    instance.record_import({"imported": 1})
    baseline = snapshot(instance.path)
    record_stamp = rows(instance)[0]["captured_at"]
    if operation in {"report", "similar"}:
        # Force pruning to delete both the record and its FTS/history rows.
        monkeypatch.setattr(pool_module.time, "time", lambda: record_stamp + 100 * 86400)
    paused, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original_connect = sqlite3.connect

    class PausingConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement.lstrip().startswith(sql):
                paused.set()
                assert release.wait(5), "transaction was never released"
            return result

    def connect(*args, **kwargs):
        return original_connect(*args, factory=PausingConnection, **kwargs)

    monkeypatch.setattr(pool_module.sqlite3, "connect", connect)
    actions = {
        "insert": lambda: instance.insert(record("synthetic:new")),
        "update": lambda: instance.update("synthetic:one", {"status": "predicted"}),
        "annotate": lambda: instance.annotate("synthetic:one", "failed"),
        "failure_set": lambda: instance.failure_set({"count": 2}),
        "record_import": lambda: instance.record_import({"imported": 2}),
        "report": instance.report,
        "similar": lambda: instance.similar("database", time.time(), "", include_synthetic=True),
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(actions[operation])
        try:
            assert paused.wait(5)
            def cancel():
                lifecycle.cancel()
                cancelled.set()
            closer = executor.submit(cancel)
            assert cancelled.wait(1), "cancel waited on the blocked transaction"
            closer.result(timeout=1)
            assert lifecycle.cancelled.is_set()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="^pool_cancelled$"):
            worker.result(timeout=5)
    assert snapshot(instance.path) == baseline
    with pytest.raises(RuntimeError, match="^pool_cancelled$"):
        instance.failure_set({"count": 3})
    instance.close()


def test_cancel_rolls_back_schema_initialization(tmp_path, monkeypatch):
    lifecycle = pool_module.WriteLifecycle()
    paused, release = threading.Event(), threading.Event()
    original_connect = sqlite3.connect

    class PausingConnection(sqlite3.Connection):
        def executescript(self, *args, **kwargs) -> sqlite3.Cursor:
            raise AssertionError("executescript implicitly commits and cannot initialize the pool")

        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement.lstrip().startswith("CREATE INDEX IF NOT EXISTS annotation_time"):
                paused.set()
                assert release.wait(5)
            return result

    def connect(*args, **kwargs):
        return original_connect(*args, factory=PausingConnection, **kwargs)

    monkeypatch.setattr(pool_module.sqlite3, "connect", connect)
    home = tmp_path / "home"
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(Pool, home, lifecycle=lifecycle)
        try:
            assert paused.wait(5)
            executor.submit(lifecycle.cancel).result(timeout=1)
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="^pool_cancelled$"):
            worker.result(timeout=5)
    with original_connect(home / "reasoning-shadow" / "pool.db") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall() == []


def test_cancellation_wins_initialization_before_creation(tmp_path, monkeypatch):
    lifecycle = pool_module.WriteLifecycle()
    paused, release = threading.Event(), threading.Event()
    home = tmp_path / "not-created"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        result = original_resolve(path, *args, **kwargs)
        if path == home:
            paused.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(Path, "resolve", resolve)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        worker = executor.submit(Pool, home, lifecycle=lifecycle)
        try:
            assert paused.wait(5)
            lifecycle.cancel()
        finally:
            release.set()
        with pytest.raises(RuntimeError, match="^pool_cancelled$"):
            worker.result(timeout=5)
    assert not home.exists()


def test_cancel_fences_an_already_entered_commit(tmp_path, monkeypatch):
    lifecycle = pool_module.WriteLifecycle()
    instance = Pool(tmp_path / "home", lifecycle=lifecycle)
    paused, release, published, returned = (threading.Event() for _ in range(4))
    original_connect = sqlite3.connect
    original_set = lifecycle.cancelled.set

    def publish():
        original_set()
        published.set()

    monkeypatch.setattr(lifecycle.cancelled, "set", publish)

    class PausingConnection(sqlite3.Connection):
        def commit(self):
            paused.set()
            assert release.wait(5)
            return super().commit()

    def connect(*args, **kwargs):
        return original_connect(*args, factory=PausingConnection, **kwargs)

    monkeypatch.setattr(pool_module.sqlite3, "connect", connect)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        worker = executor.submit(instance.insert, record())
        try:
            assert paused.wait(5)
            def cancel():
                lifecycle.cancel()
                returned.set()
            closer = executor.submit(cancel)
            assert published.wait(1), "cancellation was not published before fencing COMMIT"
            assert not returned.is_set(), "cancel returned before the in-progress COMMIT was fenced"
        finally:
            release.set()
        assert worker.result(timeout=5)
        closer.result(timeout=5)
    baseline = snapshot(instance.path)
    with pytest.raises(RuntimeError, match="^pool_cancelled$"):
        instance.update("synthetic:one", {"status": "predicted"})
    assert snapshot(instance.path) == baseline
    instance.close()
