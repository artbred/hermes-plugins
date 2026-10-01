"""Synthetic regression probes for cross-pool writes and bounded causal history."""
import concurrent.futures
import json
import sqlite3
import threading
import time

import pytest

from test_pool import Pool, cli, offline, pool, pool_module, record, rows, snapshot


@pytest.mark.parametrize("first_operation", ["update", "annotate"])
def test_independent_observer_and_cli_writes_serialize(tmp_path, monkeypatch, first_operation):
    observer = Pool(tmp_path / "home")
    operator = cli.Pool(tmp_path / "home")
    observer.insert(record())
    first_read, second_begin, second_read, release = (threading.Event() for _ in range(4))
    role = threading.local()
    original_connect = sqlite3.connect

    class PausingConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if role.name == "second" and statement.startswith("BEGIN"):
                second_begin.set()  # Operation entered, not a SELECT barrier.
            result = super().execute(statement, *args, **kwargs)
            if statement.startswith("SELECT"):
                if role.name == "first" and not first_read.is_set():
                    first_read.set()
                    assert release.wait(5)
                elif role.name == "second":
                    second_read.set()
            return result

    def connect(*args, **kwargs):
        return original_connect(*args, factory=PausingConnection, **kwargs)

    actions = {
        "update": lambda: observer.update("synthetic:one", {"status": "predicted", "actual": {"input_tokens": 17}}),
        "annotate": lambda: operator.annotate("synthetic:one", "verified_success"),
    }

    def run(name, operation):
        role.name = name
        return actions[operation]()

    monkeypatch.setattr(pool_module.sqlite3, "connect", connect)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run, "first", first_operation)
            try:
                assert first_read.wait(5)
                second = executor.submit(run, "second", "annotate" if first_operation == "update" else "update")
                assert second_begin.wait(5)
                # BEGIN IMMEDIATE blocks the second read until the first writer
                # completes; a deferred BEGIN would read now and fail upgrading.
                assert not second_read.wait(.1)
            finally:
                release.set()
            assert first.result(timeout=5)
            assert second.result(timeout=5)
        monkeypatch.setattr(pool_module.sqlite3, "connect", original_connect)
        stored = rows(observer)[0]
        assert stored["status"] == "predicted" and stored["actual"]["input_tokens"] == 17
        with original_connect(observer.path) as db:
            assert db.execute("SELECT outcome,outcome_source FROM examples").fetchone() == ("verified_success", "operator")
            assert db.execute("SELECT kind,status,source FROM annotation_history ORDER BY kind,status").fetchall() == [
                ("outcome", "verified_success", "operator"),
                ("status", "captured", "observation"),
                ("status", "predicted", "observation"),
            ]
    finally:
        observer.close()
        operator.close()


def test_cancel_during_begin_lock_wait_is_nonblocking_and_writes_nothing(tmp_path, monkeypatch):
    lifecycle = pool_module.WriteLifecycle()
    instance = Pool(tmp_path / "home", lifecycle=lifecycle)
    instance.insert(record())
    baseline = snapshot(instance.path)
    entered = threading.Event()
    original_connect = sqlite3.connect

    class WaitingConnection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            if statement.startswith("BEGIN"):
                entered.set()
            return super().execute(statement, *args, **kwargs)

    def connect(*args, **kwargs):
        return original_connect(*args, factory=WaitingConnection, **kwargs)

    lock = original_connect(instance.path)
    lock.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(pool_module.sqlite3, "connect", connect)
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            worker = executor.submit(instance.update, "synthetic:one", {"status": "predicted"})
            try:
                assert entered.wait(5)
                # The SQL lock wait must not own WriteLifecycle.gate.
                executor.submit(lifecycle.cancel).result(timeout=1)
                assert not worker.done()
            finally:
                lock.rollback()
            with pytest.raises(RuntimeError, match="^pool_cancelled$"):
                worker.result(timeout=5)
        monkeypatch.setattr(pool_module.sqlite3, "connect", original_connect)
        assert snapshot(instance.path) == baseline
    finally:
        lock.close()
        instance.close()


def test_300_redundant_annotations_coalesce_without_losing_metadata(pool, monkeypatch):
    pool.insert(record())
    now = time.time()
    monkeypatch.setattr(pool_module.time, "time", lambda: now)
    assert pool.annotate("synthetic:one", "failed")
    assert pool.update("synthetic:one", {"status": "predicted", "actual": {"wire_effort": "high"}, "prediction": {"main_effort": "low"}})
    for index in range(300):
        monkeypatch.setattr(pool_module.time, "time", lambda index=index: now + index + 1)
        assert pool.annotate("synthetic:one", "failed")
        assert pool.update("synthetic:one", {"status": "predicted", "actual": {"input_tokens": index}, "prediction": {"confidence": index}})
    # No-status observation also must not overwrite an operator label.
    assert pool.update("synthetic:one", {"actual": {"output_tokens": 10}})
    with sqlite3.connect(pool.path) as db:
        assert db.execute("SELECT kind,count(*) FROM annotation_history GROUP BY kind ORDER BY kind").fetchall() == [("outcome", 1), ("status", 2)]
        assert db.execute("SELECT outcome,outcome_at,outcome_source FROM examples").fetchone() == ("failed", now, "operator")
        assert db.execute("SELECT annotated_at FROM annotation_history WHERE kind='status' ORDER BY id DESC LIMIT 1").fetchone()[0] == now
    stored = rows(pool)[0]
    assert stored["actual"] == {"wire_effort": "high", "input_tokens": 299, "output_tokens": 10}
    assert stored["prediction"] == {"main_effort": "low", "confidence": 299}
    assert stored["metadata_at"] == now + 300
    historical = pool.similar("database", now, "", include_synthetic=True)[0]
    assert historical["outcome"] == "failed" and historical["status"] == "predicted"


def test_alternating_histories_are_bounded_and_old_cutoffs_unknown(pool, monkeypatch):
    pool.insert(record())
    now = time.time()
    for index in range(100):
        monkeypatch.setattr(pool_module.time, "time", lambda index=index: now + index)
        assert pool.annotate("synthetic:one", "failed" if index % 2 == 0 else "corrected")
        assert pool.update("synthetic:one", {"status": "predicted" if index % 2 == 0 else "expired"})
    with sqlite3.connect(pool.path) as db:
        assert db.execute("SELECT kind,count(*) FROM annotation_history GROUP BY kind ORDER BY kind").fetchall() == [("outcome", 32), ("status", 32)]
        assert db.execute("SELECT min(annotated_at) FROM annotation_history").fetchone()[0] == now + 68
        assert db.execute("SELECT outcome FROM examples").fetchone()[0] == "corrected"
    before = pool.similar("database", now + 67, "", include_synthetic=True)[0]
    assert before["status"] == before["outcome"] == "unknown"
    assert "outcome_source" not in before
    oldest = pool.similar("database", now + 68, "", include_synthetic=True)[0]
    assert oldest["status"] == "predicted" and oldest["outcome"] == "failed"
    middle = pool.similar("database", now + 69.5, "", include_synthetic=True)[0]
    assert middle["status"] == "expired" and middle["outcome"] == "corrected"
    latest = pool.similar("database", now + 100, "", include_synthetic=True)[0]
    assert latest["status"] == "expired" and latest["outcome"] == "corrected"
    reopened = Pool(pool.path.parent.parent)
    try:
        before = reopened.similar("database", now + 67, "", include_synthetic=True)[0]
        assert before["status"] == before["outcome"] == "unknown"
    finally:
        reopened.close()


@pytest.mark.parametrize("maintenance", ["initialize", "prune"])
def test_legacy_unbounded_history_capped_without_backfill(pool, monkeypatch, maintenance):
    pool.insert(record())
    now = time.time()
    # Simulate an old version's unbounded history using only a synthetic DB.
    with sqlite3.connect(pool.path) as db:
        example_id = db.execute("SELECT id FROM examples").fetchone()[0]
        db.execute("DELETE FROM annotation_history")
        for kind, status, source in [("status", "predicted", "observation"), ("outcome", "failed", "operator")]:
            db.executemany("INSERT INTO annotation_history(example_id,kind,status,annotated_at,source) VALUES(?,?,?,?,?)",
                           [(example_id, kind, status, now + index, source) for index in range(300)])
        data = rows(pool)[0]
        data.update(status="predicted", metadata_at=now + 299)
        db.execute("UPDATE examples SET record=?,outcome='failed',outcome_at=?,outcome_source='operator'", (json.dumps(data), now + 299))
    monkeypatch.setattr(pool_module.time, "time", lambda: now + 300)
    instance = Pool(pool.path.parent.parent) if maintenance == "initialize" else pool
    try:
        instance.report()
        with sqlite3.connect(pool.path) as db:
            assert db.execute("SELECT kind,count(*) FROM annotation_history GROUP BY kind ORDER BY kind").fetchall() == [("outcome", 32), ("status", 32)]
            assert db.execute("SELECT min(annotated_at) FROM annotation_history").fetchone()[0] == now + 268
        past = instance.similar("database", now + 267, "", include_synthetic=True)[0]
        assert past["status"] == past["outcome"] == "unknown"
        current = instance.similar("database", now + 300, "", include_synthetic=True)[0]
        assert current["status"] == "predicted" and current["outcome"] == "failed"
    finally:
        if instance is not pool:
            instance.close()


def test_retention_deletes_capped_history_and_fts_together(tmp_path, monkeypatch):
    instance = Pool(tmp_path / "home", max_examples=1, retention_days=1)
    now = time.time()
    instance.insert(record("synthetic:old", request="oldfixtureword", captured_at=now - 10, source_at=now - 10))
    for index in range(40):
        monkeypatch.setattr(pool_module.time, "time", lambda index=index: now + index)
        instance.annotate("synthetic:old", "failed" if index % 2 == 0 else "corrected")
    instance.insert(record("synthetic:new", request="newfixtureword", captured_at=now + 40, source_at=now + 40))
    with sqlite3.connect(instance.path) as db:
        assert db.execute("SELECT source_handle FROM examples").fetchall() == [("synthetic:new",)]
        assert db.execute("SELECT request FROM requests_fts").fetchall() == [("newfixtureword",)]
        assert db.execute("SELECT kind,status FROM annotation_history").fetchall() == [("status", "captured")]
    monkeypatch.setattr(pool_module.time, "time", lambda: now + 2 * 86400)
    assert instance.report()["total"] == 0
    with sqlite3.connect(instance.path) as db:
        for table in ("examples", "requests_fts", "annotation_history"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    instance.close()
