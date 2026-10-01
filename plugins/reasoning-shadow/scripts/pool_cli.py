#!/usr/bin/env python3
"""Offline administration for the private reasoning-shadow example pool.

History import never loads SessionDB (whose constructor can migrate/write).
A --source path explicitly authorizes that source; there is no profile scanning.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import time

_POOL_PATH = Path(__file__).resolve().parents[1] / "pool.py"
_spec = importlib.util.spec_from_file_location("reasoning_shadow_pool_cli", _POOL_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError("pool_module_unavailable")
_pool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_pool)
Pool = _pool.Pool

HUMAN_SOURCES = frozenset({"cli", "telegram", "discord", "slack", "whatsapp", "signal", "matrix", "email", "sms", "mattermost", "feishu", "lark", "dingtalk", "wecom", "weixin", "wechat", "bluebubbles", "api", "api_server", "webui", "openwebui", "tui", "desktop"})
INTERNAL_KINDS = frozenset({"internal_notification", "internal", "system", "tool", "reasoning", "analysis", "subagent", "cron", "webhook", "background_completion", "compressed_summary"})
LEGACY_GENERATED_PREFIXES = ("[ASYNC DELEGATION", "[CONTEXT COMPACTION", "[INTERNAL NOTIFICATION", "[Your active task list")
MESSAGE_FIELDS = ("id", "session_id", "role", "content", "timestamp", "display_kind", "display_metadata", "_compressed_summary", "tool_calls", "tool_call_id")
SESSION_FIELDS = ("id", "source", "model", "parent_session_id", "profile_name", "origin_json")


def _object(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _internal(metadata: dict) -> bool:
    if metadata.get("display_kind") in INTERNAL_KINDS or metadata.get("kind") in INTERNAL_KINDS:
        return True
    return any(metadata.get(key) not in (None, False, 0, "", "false") for key in
               ("internal", "is_internal", "internal_event", "is_internal_event", "is_subagent", "subagent", "synthetic", "is_synthetic", "is_notification", "notification", "parent_session_id", "_verification_nudge", "_pre_verify_hook_continue"))


def _message_allowed(message) -> bool:
    message = dict(message)
    if message.get("role") not in {"user", "assistant"} or message.get("_compressed_summary", 0) or message.get("tool_calls") or message.get("tool_call_id"):
        return False
    kind = message.get("display_kind")
    # Unknown explicitly marked kinds are not trusted as human provenance.
    if kind not in (None, "", "message", "user", "assistant", "human", "normal"):
        return False
    metadata = _object(message.get("display_metadata"))
    if _internal(message) or _internal(metadata):
        return False
    # Only exact anchored legacy envelopes with unknown provenance are
    # machine-generated. Explicit human ownership and embedded quotes survive.
    text = _text(message.get("content"))
    if kind is None and metadata.get("gateway_input_owner") != "human" and text is not None and text.startswith(LEGACY_GENERATED_PREFIXES):
        return False
    return True


def _text(content) -> str | None:
    if isinstance(content, str):
        return content
    # Do not serialize images, API content, reasoning blocks or tool metadata.
    if isinstance(content, list) and all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content):
        return "\n".join(item["text"] for item in content)
    return None


def import_history(home: Path, source: Path | None = None, limit=1000, sources=("telegram",)) -> dict:
    """Import bounded, human-source user turns. Return metadata counters only."""
    if type(limit) is not int or not 1 <= limit <= 1500:
        raise ValueError("invalid_import_limit")
    if not isinstance(sources, (tuple, list)) or not sources or any(not isinstance(kind, str) or kind not in HUMAN_SOURCES for kind in sources):
        raise ValueError("invalid_import_sources")
    home = Path(home).expanduser().resolve()
    source = Path(source).expanduser().resolve() if source is not None else home / "state.db"
    if not source.is_file():
        raise ValueError("history_source_unavailable")
    counters = {"scanned": 0, "imported": 0, "duplicates": 0, "excluded_internal": 0, "excluded_unknown": 0, "skipped_oversize": 0, "skipped_invalid": 0}
    captured = time.time()
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        session_columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
        message_columns = {r[1] for r in db.execute("PRAGMA table_info(messages)")}
        if not {"id"} <= session_columns or not {"id", "session_id", "role", "content", "timestamp"} <= message_columns:
            raise ValueError("history_schema_unsupported")
        if "source" not in session_columns:
            raise ValueError("history_schema_unsupported")
        # Never load hidden reasoning or model configuration, even temporarily.
        message_projection = ",".join("m." + name for name in MESSAGE_FIELDS if name in message_columns)
        session_projection = ",".join(name for name in SESSION_FIELDS if name in session_columns)
        profile = home.name if home.parent.name == "profiles" else "default"
        profile_filter = ""
        profile_params = []
        if "profile_name" in session_columns:
            profile_filter = " AND (s.profile_name=?" + (" OR s.profile_name IS NULL)" if profile == "default" else ")")
            profile_params = [profile]
        oldest = captured - 90 * 86400
        messages = db.execute(f"""SELECT {message_projection} FROM messages m JOIN sessions s ON s.id=m.session_id
            WHERE m.role='user' AND m.timestamp>=? AND m.timestamp<=?
            AND s.source IN ({','.join('?' for _ in sources)}){profile_filter}
            ORDER BY m.timestamp DESC,m.id DESC LIMIT ?""", (oldest, captured, *sources, *profile_params, limit)).fetchall()
        pool = Pool(home)
        try:
            for message in messages:
                counters["scanned"] += 1
                session_row = db.execute(f"SELECT {session_projection} FROM sessions WHERE id=?", (message["session_id"],)).fetchone()
                if session_row is None:
                    counters["excluded_unknown"] += 1
                    continue
                session = dict(session_row)
                origin = _object(session.get("origin_json"))
                source_kind = session.get("source")
                if session.get("parent_session_id") or _internal(session) or _internal(origin) or source_kind in {"cron", "subagent", "webhook", "internal", "background", "batch", "rl"}:
                    counters["excluded_internal"] += 1
                    continue
                if source_kind not in sources:
                    counters["excluded_unknown"] += 1
                    continue
                if not _message_allowed(message):
                    counters["excluded_internal"] += 1
                    continue
                text = _text(message["content"])
                try:
                    stamp = _pool._timestamp(message["timestamp"])
                except (ValueError, TypeError):
                    counters["skipped_invalid"] += 1
                    continue
                if text is None or not text.strip() or stamp > captured:
                    counters["skipped_invalid"] += 1
                    continue
                context_rows = db.execute(f"""SELECT {message_projection} FROM messages m WHERE m.session_id=? AND m.timestamp < ?
                    AND m.role IN ('user','assistant') ORDER BY m.timestamp DESC,m.id DESC""", (message["session_id"], stamp))
                context = []
                for prior in context_rows:
                    if not _message_allowed(prior):
                        continue
                    prior_text = _text(prior["content"])
                    if prior_text is not None:
                        visible = _pool._context([{"role": prior["role"], "content": prior_text}])
                        if visible:
                            context.extend(visible)
                    if len(context) == 6:
                        break
                digest = hashlib.sha256((str(source) + "\0" + str(message["session_id"]) + "\0" + str(message["id"])).encode()).hexdigest()
                record = {"source_handle": "history:" + digest, "provenance": "history:" + str(source_kind),
                          "synthetic": False, "sid_ref": hashlib.sha256(str(message["session_id"]).encode()).hexdigest(),
                          "tid_ref": hashlib.sha256(str(message["id"]).encode()).hexdigest(),
                          "captured_at": captured, "source_at": stamp, "generation_cutoff": stamp,
                          "request": text, "recent_context": list(reversed(context)),
                          "status": "history_unknown", "actual": {}, "prediction": {}, "outcome": "unknown"}
                if len(json.dumps(record, ensure_ascii=False)) > _pool.MAX_RECORD_CHARACTERS:
                    counters["skipped_oversize"] += 1
                    continue
                try:
                    if pool.insert(record):
                        counters["imported"] += 1
                    else:
                        # Expired records are excluded, not misreported as dedupe.
                        with pool._connection() as target:
                            exists = target.execute("SELECT 1 FROM examples WHERE source_handle=?", (record["source_handle"],)).fetchone()
                        if exists:
                            counters["duplicates"] += 1
                        else:
                            counters["skipped_invalid"] += 1
                except (ValueError, TypeError):
                    counters["skipped_invalid"] += 1
            pool.record_import(counters)
        finally:
            pool.close()
    return counters


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        print(json.dumps({"ok": False, "error": "pool_arguments_invalid"}))
        raise SystemExit(2)


def main(argv=None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--home", type=Path, default=None)
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="Metadata counts only")
    history = commands.add_parser("import-history", help="Offline read-only bounded import")
    history.add_argument("--source", type=Path, default=None)
    history.add_argument("--limit", type=int, default=1000)
    history.add_argument("--sources", nargs="+", choices=sorted(HUMAN_SOURCES), default=["telegram"], help="Explicit human-source opt-in (default: telegram)")
    annotation = commands.add_parser("annotate", help="Explicit operator outcome")
    annotation.add_argument("source_handle")
    annotation.add_argument("status", nargs="?", choices=sorted(_pool.OUTCOMES))
    annotation.add_argument("--status", "--outcome", dest="status_option", choices=sorted(_pool.OUTCOMES))
    for command in (report, history, annotation):
        command.add_argument("--home", type=Path, default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.home is None:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
        else:
            home = args.home
        if args.command == "import-history":
            output = import_history(home, args.source, args.limit, args.sources)
        else:
            pool = Pool(home)
            try:
                if args.command == "report":
                    output = pool.report()
                else:
                    status = args.status_option or args.status
                    if status is None:
                        raise ValueError("annotation_status_required")
                    if not pool.annotate(args.source_handle, status):
                        print(json.dumps({"ok": False, "error": "source_handle_not_found"}))
                        return 1
                    output = {"ok": True, "outcome": status}
            finally:
                pool.close()
        print(json.dumps(output, sort_keys=True, ensure_ascii=False))
        return 0
    except (OSError, sqlite3.Error, ValueError, ImportError, RuntimeError):
        # Never reveal a request, source path, SQL payload or credential in errors.
        print(json.dumps({"ok": False, "error": "pool_operation_failed"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
