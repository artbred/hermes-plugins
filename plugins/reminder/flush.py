#!/usr/bin/env python3
"""Reminder ledger -> Graphiti auto-sync (v0.2.0).

Reads new rows from the local reminder ledger and writes meaningful ones as
triplets into Graphiti group ``infra`` (the living system map). Deliberately
selective: routine successful touches are noise and are never synced. Only
task checklists, verified resolutions, and tool failures cross over.

Runs from system cron every 30 minutes. State (per-table cursors) lives next
to the ledger. Max triplets per run caps cost and time.

Usage:
  flush.py            # normal run
  flush.py --status   # show cursors and pending counts, no writes
"""
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home() / ".hermes"
LEDGER_DB = HOME / "reminder" / "ledger.db"
STATE_FILE = HOME / "reminder" / "sync-state.json"
AUTH_FILE = HOME / "graphiti-auth-client.txt"
GRAPHITI_URL = "http://100.95.234.17:18000/mcp"
GROUP_ID = "infra"
MAX_TRIPLETS = 8


class GraphitiError(Exception):
    pass


class GraphitiClient:
    def __init__(self, url=GRAPHITI_URL):
        raw = os.environ.get("GRAPHITI_BASIC", "")
        if not raw and AUTH_FILE.exists():
            raw = AUTH_FILE.read_text().strip()
        if not raw or ":" not in raw:
            raise GraphitiError("graphiti credentials unavailable")
        self.basic = base64.b64encode(raw.encode()).decode()
        self.url = url
        self.session = None

    def _post(self, body):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "Authorization": "Basic " + self.basic,
                   "Host": "localhost:8000"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                if not self.session:
                    sid = resp.headers.get("Mcp-Session-Id")
                    if sid:
                        self.session = sid
                return resp.read().decode()
        except Exception as error:
            raise GraphitiError(
                f"graphiti POST failed: {type(error).__name__}") from None

    @staticmethod
    def _payload(raw):
        for line in raw.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        return json.loads(raw)

    def initialize(self):
        raw = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "reminder-flush",
                                                    "version": "0.2.0"}}})
        data = self._payload(raw)
        if "error" in data:
            raise GraphitiError(f"initialize: {data['error']}")
        if not self.session:
            raise GraphitiError("no session id from graphiti")

    def add_triplet(self, source, edge, fact, target):
        body = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "add_triplet",
                           "arguments": {"source_node_name": source,
                                         "edge_name": edge, "fact": fact,
                                         "target_node_name": target,
                                         "group_id": GROUP_ID}}}
        data = self._payload(self._post(body))
        if "error" in data:
            raise GraphitiError(f"add_triplet: {data['error']}")
        return data


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"tasks": 0.0, "obligations": 0, "touches": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def collect(con, state):
    """Return ordered triplet ops: (cursor_table, cursor_value, op)."""
    ops = []
    for thash, summary, verbs, created in con.execute(
            "SELECT task_hash, summary, verbs, created_at FROM tasks "
            "WHERE created_at > ? ORDER BY created_at", (state["tasks"],)):
        node = f"reminder task {thash[:8]}"
        if "secret" in verbs.split(","):
            fact = f"Secret-hygiene task opened ({verbs}); details withheld."
        else:
            fact = f"Task opened: {summary[:280]}"
        ops.append(("tasks", created,
                    (node, "NEEDS_CHECK", fact, f"{verbs} checklist")))
    for oid, thash, kind, evidence, _ in con.execute(
            "SELECT id, task_hash, kind, evidence, resolved_at FROM obligations "
            "WHERE status='done' AND id > ? ORDER BY id", (state["obligations"],)):
        ops.append(("obligations", oid,
                    (f"reminder task {thash[:8]}", "VERIFIED",
                     f"{kind} check resolved: {evidence[:280]}",
                     f"{kind} check")))
    for tid, thash, tool, summary, _ in con.execute(
            "SELECT id, task_hash, tool, summary, created_at FROM touches "
            "WHERE ok=0 AND id > ? ORDER BY id", (state["touches"],)):
        ops.append(("touches", tid,
                    (tool, "FAILED_WITH",
                     f"Tool failed during task: {summary[:280]}",
                     f"reminder task {thash[:8]}")))
    ops.sort(key=lambda op: (op[0] != "touches", op[0] != "obligations"))
    return ops[:MAX_TRIPLETS]


def main():
    if "--status" in sys.argv:
        state = load_state()
        print(f"cursors: {state}")
        if LEDGER_DB.exists():
            con = sqlite3.connect(str(LEDGER_DB))
            ops = collect(con, state)
            print(f"pending triplet ops: {len(ops)}")
        return 0
    if not LEDGER_DB.exists():
        print("no ledger yet")
        return 0
    con = sqlite3.connect(str(LEDGER_DB))
    state = load_state()
    ops = collect(con, state)
    if not ops:
        print("nothing to sync")
        return 0
    client = GraphitiClient()
    client.initialize()
    synced = 0
    for table, cursor, (src, edge, fact, tgt) in ops:
        try:
            client.add_triplet(src, edge, fact, tgt)
        except GraphitiError as error:
            print(f"STOP {src} -[{edge}]-> {tgt}: {error}")
            break
        state[table] = cursor
        save_state(state)
        synced += 1
        print(f"synced: {src} -[{edge}]-> {tgt}")
        time.sleep(2)
    print(f"SUMMARY synced={synced}/{len(ops)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
