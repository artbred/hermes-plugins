"""Private, profile-scoped request examples. No network or host state writes.

Only request text is indexed. Outcome labels are operator assertions, never an
inference from a model response. Connections are operation-local, not shared
across the observer's background threads.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

OUTCOMES = frozenset({"unknown", "verified_success", "failed", "corrected"})
MAX_RECORD_CHARACTERS = 100000
MAX_HISTORY_PER_KIND = 32
EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})
PREDICTION_STATUSES = frozenset({"unknown", "captured", "queued", "pending", "predicted", "history_unknown", "invalid", "invalid_prediction", "invalid_response", "unsafe_confidence", "transport_error", "http_error", "missing_api_key", "failure_cooldown", "input_too_large", "low_confidence", "abstained", "technical_failure", "failed", "expired", "oversize", "skipped_oversize", "unavailable", "cooldown"})
IMPORT_COUNTERS = frozenset({"scanned", "imported", "duplicates", "excluded_internal", "excluded_unknown", "skipped_oversize", "skipped_invalid"})


def redact(text: str) -> str:
    """Always force the installed Hermes redaction boundary, even if disabled."""
    # A partial substitute cannot provide Hermes' credential boundary. Fail
    # closed if the offline CLI was launched outside the installed environment.
    from agent.redact import redact_sensitive_text
    text = redact_sensitive_text(text, force=True, redact_url_credentials=True)
    # Explicit assignments can be low-entropy secrets not recognized by the
    # host's credential-shape detectors. Preserve surrounding request text.
    return re.sub(r'''(?i)(\b(?:api[_-]?key|token|password|secret)\s*["']?\s*[:=]\s*["']?)[^\s"',}]+''', r"\1[REDACTED]", text)


def _redacted(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if value is None or type(value) in (int, float, bool):
        return value
    raise ValueError("invalid_record")


def _timestamp(value) -> float:
    if isinstance(value, bool):
        raise ValueError("invalid_timestamp")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError("invalid_timestamp")
    return value


def _context(value) -> list:
    if not isinstance(value, list):
        raise ValueError("invalid_context")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}:
            continue
        content = item.get("content")
        if not isinstance(content, str) or item.get("tool_calls") or item.get("display_kind") in {"internal_notification", "reasoning", "system", "tool"}:
            continue
        # Hidden assistant reasoning is removed; human quotations stay intact.
        if item["role"] == "assistant":
            content = re.sub(r"(?is)<(?:think|analysis|reasoning)>.*?</(?:think|analysis|reasoning)>", "", content)
            if re.search(r"(?is)<(?:think|analysis|reasoning)>", content):
                continue
        if content:
            result.append({"role": item["role"], "content": redact(content)})
    return result[-6:]


class WriteLifecycle:
    """Shared observer cancellation and the narrow durable-write boundary.

    SQL execution/lock waits must not hold ``gate``. Cancellation publishes its
    event immediately, then fences only file creation and an already-started
    COMMIT. Once cancel() returns, no operation can commit behind the observer.
    """
    def __init__(self):
        self.cancelled = threading.Event()
        self.gate = threading.RLock()

    def assert_active(self):
        if self.cancelled.is_set():
            raise RuntimeError("pool_cancelled")

    def cancel(self):
        self.cancelled.set()
        with self.gate:
            pass


class Pool:
    def __init__(self, home: Path, max_examples=2000, retention_days=90, lifecycle=None):
        self.lifecycle = lifecycle if lifecycle is not None else WriteLifecycle()
        self.lifecycle.assert_active()
        if type(max_examples) is not int or not 1 <= max_examples <= 2000:
            raise ValueError("invalid_max_examples")
        if type(retention_days) not in (int, float) or not 0 < retention_days <= 3650:
            raise ValueError("invalid_retention")
        self.max_examples = max_examples
        self.retention_days = retention_days
        self._lock = threading.RLock()
        self._closed = False
        self.lifecycle.assert_active()
        directory = Path(home).expanduser().resolve() / "reasoning-shadow"
        self.path = directory / "pool.db"
        # Fence unavoidable creation IO, not schema execution or transaction
        # lock waits. Recheck inside the gate: cancellation can win while this
        # worker is waiting to enter initialization.
        with self.lifecycle.gate:
            self.lifecycle.assert_active()
            if directory.is_symlink():
                raise ValueError("unsafe_pool_path")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.lifecycle.assert_active()
            os.chmod(directory, 0o700)
            for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                if path.is_symlink():
                    raise ValueError("unsafe_pool_path")
            self.lifecycle.assert_active()
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.close(fd)
            self.lifecycle.assert_active()
            os.chmod(self.path, 0o600)
        with self._connection(initialize=True) as db:
            schema = """
                CREATE TABLE IF NOT EXISTS examples (
                    id INTEGER PRIMARY KEY,
                    source_handle TEXT NOT NULL UNIQUE,
                    captured_at REAL NOT NULL,
                    source_at REAL NOT NULL,
                    request TEXT NOT NULL,
                    record TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT 'unknown',
                    outcome_at REAL,
                    outcome_source TEXT
                );
                CREATE INDEX IF NOT EXISTS examples_time ON examples(captured_at,source_at);
                CREATE VIRTUAL TABLE IF NOT EXISTS requests_fts USING fts5(request, tokenize='unicode61');
                CREATE TRIGGER IF NOT EXISTS examples_ai AFTER INSERT ON examples BEGIN
                    INSERT INTO requests_fts(rowid,request) VALUES (new.id,new.request);
                END;
                CREATE TRIGGER IF NOT EXISTS examples_ad AFTER DELETE ON examples BEGIN
                    DELETE FROM requests_fts WHERE rowid=old.id;
                END;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS annotation_history (
                    id INTEGER PRIMARY KEY,
                    example_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    annotated_at REAL NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS annotation_time ON annotation_history(example_id,kind,annotated_at,id);
                CREATE TRIGGER IF NOT EXISTS examples_annotations_ad AFTER DELETE ON examples BEGIN
                    DELETE FROM annotation_history WHERE example_id=old.id;
                END;
            """
            # executescript() commits its pending transaction before running.
            # complete_statement preserves trigger bodies (their inner ';'
            # are not statement boundaries) while keeping all DDL atomic.
            statement = ""
            for line in schema.splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self.lifecycle.assert_active()
                    db.execute(statement)
                    statement = ""
            if statement.strip():
                raise RuntimeError("invalid_pool_schema")
            # Preserve the only snapshots available in pools created before
            # history tracking. Never invent earlier values during migration.
            for row in db.execute("SELECT id,captured_at,record,outcome,outcome_at,outcome_source FROM examples").fetchall():
                data = json.loads(row["record"])
                if not db.execute("SELECT 1 FROM annotation_history WHERE example_id=? AND kind='status'", (row["id"],)).fetchone():
                    db.execute("INSERT INTO annotation_history(example_id,kind,status,annotated_at,source) VALUES(?,'status',?,?, 'observation')", (row["id"], str(data.get("status", "unknown")), data.get("metadata_at", row["captured_at"])))
                if row["outcome_at"] is not None and not db.execute("SELECT 1 FROM annotation_history WHERE example_id=? AND kind='outcome'", (row["id"],)).fetchone():
                    db.execute("INSERT INTO annotation_history(example_id,kind,status,annotated_at,source) VALUES(?,'outcome',?,?,?)", (row["id"], row["outcome"], row["outcome_at"], row["outcome_source"] or "operator"))
            self._prune(db)

    def _permissions(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self.path) + suffix)
            if path.is_symlink():
                raise ValueError("unsafe_pool_path")
            try:
                os.chmod(path, 0o600)
            except FileNotFoundError:
                pass

    def _initialize_journal(self, db):
        # journal_mode persists outside the schema transaction. Fence it too,
        # but never hold unload behind SQLite's ordinary ten-second busy wait.
        # A busy initialization fails closed; a future turn may retry it.
        with self.lifecycle.gate:
            self.lifecycle.assert_active()
            db.execute("PRAGMA busy_timeout=0")
            try:
                db.execute("PRAGMA journal_mode=WAL")
            finally:
                db.execute("PRAGMA busy_timeout=10000")

    @contextmanager
    def _connection(self, initialize=False):
        self.lifecycle.assert_active()
        with self._lock:
            self.lifecycle.assert_active()
            if self._closed:
                raise RuntimeError("pool_closed")
            # SQLite can create a missing database on connect; fence that IO
            # just like the initial private-directory/file creation above.
            with self.lifecycle.gate:
                self.lifecycle.assert_active()
                self._permissions()
                db = sqlite3.connect(str(self.path), timeout=10)
            db.row_factory = sqlite3.Row
            try:
                self.lifecycle.assert_active()
                if initialize:
                    # Journal mode is persistent setup before BEGIN; its
                    # helper uses a narrow zero-busy-timeout durable gate.
                    self._initialize_journal(db)
                with self.lifecycle.gate:
                    self.lifecycle.assert_active()
                    self._permissions()
                # Reserve the writer before reading: independent observer/CLI
                # pools must wait, not fail a deferred read-to-write upgrade.
                # The SQLite busy wait deliberately does not own the gate.
                db.execute("BEGIN IMMEDIATE")
                self.lifecycle.assert_active()
                yield db
                # Only COMMIT owns the gate. A worker paused in any statement
                # or busy wait observes cancellation here and rolls back all
                # example, FTS, history, metadata and schema writes together.
                with self.lifecycle.gate:
                    self.lifecycle.assert_active()
                    db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()
                with self.lifecycle.gate:
                    if not self.lifecycle.cancelled.is_set():
                        self._permissions()

    def _prune(self, db):
        oldest = time.time() - self.retention_days * 86400
        db.execute("DELETE FROM examples WHERE captured_at < ? OR source_at < ?", (oldest, oldest))
        db.execute("DELETE FROM examples WHERE id NOT IN (SELECT id FROM examples ORDER BY captured_at DESC,id DESC LIMIT ?)", (self.max_examples,))
        # Also bound histories from older versions, without moving timestamps
        # or fabricating earlier labels. Selection before retained history is
        # unknown, even when the example's latest metadata has a known label.
        db.execute("""DELETE FROM annotation_history WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY example_id,kind ORDER BY annotated_at DESC,id DESC
                ) AS position FROM annotation_history
            ) WHERE position > ?
        )""", (MAX_HISTORY_PER_KIND,))

    def _append_annotation(self, db, example_id, kind, status, stamp, source):
        latest = db.execute("""SELECT status,source FROM annotation_history
            WHERE example_id=? AND kind=? ORDER BY annotated_at DESC,id DESC LIMIT 1""",
            (example_id, kind)).fetchone()
        if latest is not None and latest["status"] == status and latest["source"] == source:
            return False
        db.execute("INSERT INTO annotation_history(example_id,kind,status,annotated_at,source) VALUES(?,?,?,?,?)",
                   (example_id, kind, status, stamp, source))
        db.execute("""DELETE FROM annotation_history WHERE example_id=? AND kind=? AND id NOT IN (
            SELECT id FROM annotation_history WHERE example_id=? AND kind=?
            ORDER BY annotated_at DESC,id DESC LIMIT ?
        )""", (example_id, kind, example_id, kind, MAX_HISTORY_PER_KIND))
        return True

    def insert(self, record: dict) -> bool:
        if not isinstance(record, dict):
            raise ValueError("invalid_record")
        handle = record.get("source_handle")
        request = record.get("request")
        if not isinstance(handle, str) or not re.fullmatch(r"[A-Za-z0-9_.:\-]{1,200}", handle) or not isinstance(request, str):
            raise ValueError("invalid_record")
        captured = _timestamp(record.get("captured_at", time.time()))
        source = _timestamp(record.get("source_at", captured))
        data = {k: _redacted(record[k]) for k in ("provenance", "synthetic", "sid_ref", "tid_ref", "status", "prediction", "actual") if k in record}
        request = redact(request)
        data.update(source_handle=handle, captured_at=captured, source_at=source,
                    generation_cutoff=_timestamp(record.get("generation_cutoff", source)),
                    request=request, recent_context=_context(record.get("recent_context", [])),
                    outcome="unknown", status=data.get("status", "unknown"))
        supplied_hash = record.get("request_hash")
        if supplied_hash is not None and (not isinstance(supplied_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied_hash)):
            raise ValueError("invalid_request_hash")
        # Oversize diagnostic records deliberately omit text, but must retain
        # the observer's original request digest rather than hashing empty text.
        data["request_hash"] = supplied_hash if supplied_hash is not None else hashlib.sha256(request.encode()).hexdigest()
        if "request_characters" in record:
            characters = record["request_characters"]
            if type(characters) is not int or characters < 0:
                raise ValueError("invalid_request_characters")
            data["request_characters"] = characters
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(payload) > MAX_RECORD_CHARACTERS:
            return False
        with self._connection() as db:
            self._prune(db)
            inserted = db.execute("INSERT OR IGNORE INTO examples(source_handle,captured_at,source_at,request,record) VALUES(?,?,?,?,?)", (handle, captured, source, data["request"], payload)).rowcount == 1
            if inserted:
                row = db.execute("SELECT id FROM examples WHERE source_handle=?", (handle,)).fetchone()
                self._append_annotation(db, row[0], "status", str(data["status"]), captured, "observation")
            self._prune(db)
            return inserted and db.execute("SELECT 1 FROM examples WHERE source_handle=?", (handle,)).fetchone() is not None

    def update(self, source_handle: str, metadata: dict) -> bool:
        # Outcomes and causal snapshots cannot be overwritten by observation.
        allowed = {"status", "prediction", "actual"}
        if not isinstance(metadata, dict) or set(metadata) - allowed:
            raise ValueError("invalid_update")
        clean = {key: _redacted(value) for key, value in metadata.items()}
        with self._connection() as db:
            row = db.execute("SELECT record,id FROM examples WHERE source_handle=?", (source_handle,)).fetchone()
            if row is None:
                return False
            data = json.loads(row[0])
            for key, value in clean.items():
                if key in {"actual", "prediction"} and isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value
            data["metadata_at"] = time.time()
            payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
            if len(payload) > MAX_RECORD_CHARACTERS:
                return False
            db.execute("UPDATE examples SET record=? WHERE source_handle=?", (payload, source_handle))
            if "status" in clean:
                self._append_annotation(db, row["id"], "status", str(clean["status"]), data["metadata_at"], "observation")
            return True

    def annotate(self, source_handle: str, status: str, source="operator") -> bool:
        if status not in OUTCOMES or source != "operator":
            raise ValueError("invalid_annotation")
        with self._connection() as db:
            row = db.execute("SELECT id FROM examples WHERE source_handle=?", (source_handle,)).fetchone()
            if row is None:
                return False
            stamp = time.time()
            if self._append_annotation(db, row[0], "outcome", status, stamp, source):
                db.execute("UPDATE examples SET outcome=?,outcome_at=?,outcome_source=? WHERE id=?", (status, stamp, source, row[0]))
            return True

    def similar(self, request: str, cutoff: float, exclude: str, limit=5, max_characters=12000, include_synthetic=False) -> list:
        cutoff = _timestamp(cutoff)
        if type(limit) is not int or not 1 <= limit <= 5 or type(max_characters) is not int or max_characters < 1 or type(include_synthetic) is not bool:
            raise ValueError("invalid_selection")
        # Quote each unicode token: arbitrary request text is not FTS syntax.
        tokens = list(dict.fromkeys(re.findall(r"[^\W_]+", redact(request).lower(), re.UNICODE)))[:64]
        if not tokens:
            return []
        query = " OR ".join('"' + token + '"' for token in tokens)
        with self._connection() as db:
            self._prune(db)
            rows = db.execute("""SELECT e.record,
                (SELECT h.status FROM annotation_history h WHERE h.example_id=e.id AND h.kind='status' AND h.annotated_at<=? ORDER BY h.annotated_at DESC,h.id DESC LIMIT 1) AS historical_status,
                (SELECT h.status FROM annotation_history h WHERE h.example_id=e.id AND h.kind='outcome' AND h.annotated_at<=? ORDER BY h.annotated_at DESC,h.id DESC LIMIT 1) AS historical_outcome,
                (SELECT h.source FROM annotation_history h WHERE h.example_id=e.id AND h.kind='outcome' AND h.annotated_at<=? ORDER BY h.annotated_at DESC,h.id DESC LIMIT 1) AS historical_source,
                bm25(requests_fts) AS score FROM requests_fts
                JOIN examples e ON e.id=requests_fts.rowid WHERE requests_fts MATCH ?
                AND e.captured_at < ? AND e.source_at < ? AND e.source_handle != ?
                ORDER BY score,e.captured_at DESC LIMIT 2000""", (cutoff, cutoff, cutoff, query, cutoff, cutoff, exclude or "")).fetchall()
        result, used = [], 0
        for row in rows:
            data = json.loads(row["record"])
            if not include_synthetic and data.get("synthetic") is True:
                continue
            # No generated output, later usage or predictions in prompt examples.
            example = {k: data[k] for k in ("source_handle", "request", "recent_context", "provenance", "synthetic", "source_at", "captured_at", "generation_cutoff", "request_hash") if k in data}
            example["status"] = row["historical_status"] or "unknown"
            example["outcome"] = row["historical_outcome"] or "unknown"
            if example["outcome"] != "unknown":
                example["outcome_source"] = row["historical_source"]
            size = len(json.dumps(example, ensure_ascii=False))
            if size > max_characters - used:
                continue  # Skip complete oversized examples; never truncate.
            result.append(example)
            used += size
            if len(result) == limit:
                break
        return result

    def report(self) -> dict:
        with self._connection() as db:
            self._prune(db)
            rows = db.execute("SELECT record,outcome FROM examples").fetchall()
            imports = db.execute("SELECT value FROM metadata WHERE key='import_counts'").fetchone()
        outcomes = {name: 0 for name in sorted(OUTCOMES)}
        statuses = {}
        actual_efforts, recommended_efforts = {}, {}
        comparison = {"matched": 0, "mismatched": 0, "unknown": 0}
        predictions = {"past": 0, "invalid": 0, "unknown": 0}
        measurements: dict[str, dict[str, int | float]] = {name: {"count": 0, "sum": 0} for name in ("duration_seconds", "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens")}
        for row in rows:
            outcomes[row["outcome"]] += 1
            data = json.loads(row["record"])
            status = data.get("status", "unknown")
            # Only fixed protocol labels may enter a metadata report.
            status = status if isinstance(status, str) and status in PREDICTION_STATUSES else "unknown"
            statuses[status] = statuses.get(status, 0) + 1
            prediction = data.get("prediction") if isinstance(data.get("prediction"), dict) else {}
            actual = data.get("actual") if isinstance(data.get("actual"), dict) else {}
            recommended = prediction.get("main_effort", prediction.get("recommended_effort"))
            recommended = recommended if isinstance(recommended, str) and recommended in EFFORTS else "unknown"
            wire = actual.get("wire_effort")
            wire = wire if isinstance(wire, str) and wire in EFFORTS else "unknown"
            recommended_efforts[recommended] = recommended_efforts.get(recommended, 0) + 1
            actual_efforts[wire] = actual_efforts.get(wire, 0) + 1
            comparable = status == "predicted" and recommended != "unknown" and wire != "unknown"
            category = ("matched" if recommended == wire else "mismatched") if comparable else "unknown"
            comparison[category] += 1
            if status == "history_unknown":
                predictions["past"] += 1
            elif status in {"invalid", "invalid_prediction", "invalid_response"}:
                predictions["invalid"] += 1
            elif not comparable:
                predictions["unknown"] += 1
            for name, aggregate in measurements.items():
                value = actual.get(name, actual.get("api_duration_seconds") if name == "duration_seconds" else None)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 1e18 and math.isfinite(value):
                    total = aggregate["sum"] + value
                    if math.isfinite(total):
                        aggregate["count"] += 1
                        aggregate["sum"] = total
        import_counts = json.loads(imports[0]) if imports else {}
        import_counts = {key: value for key, value in import_counts.items() if key in IMPORT_COUNTERS and type(value) is int and value >= 0}
        return {"total": len(rows), "by_outcome": outcomes, "by_prediction_status": statuses,
                "by_actual_wire_effort": actual_efforts, "by_recommended_effort": recommended_efforts,
                "prediction_comparison": comparison, "prediction_counts": predictions,
                "usage": measurements, "import_counts": import_counts}

    def record_import(self, counters: dict):
        with self._connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='import_counts'").fetchone()
            total = json.loads(row[0]) if row else {}
            for key, value in counters.items():
                if key in IMPORT_COUNTERS and type(value) is int and value >= 0:
                    total[key] = total.get(key, 0) + value
            db.execute("INSERT OR REPLACE INTO metadata VALUES('import_counts',?)", (json.dumps(total),))

    def failure_get(self) -> dict:
        with self._connection() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='failure'").fetchone()
            return json.loads(row[0]) if row else {}

    def failure_set(self, metadata: dict):
        if not isinstance(metadata, dict) or set(metadata) - {"count", "failures", "consecutive_failures", "until", "retry_at", "cooldown_until", "last_failure_at"} or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in metadata.values()):
            raise ValueError("invalid_failure_metadata")
        with self._connection() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('failure',?)", (json.dumps(metadata, allow_nan=False),))

    def close(self):
        with self._lock:
            self._closed = True
