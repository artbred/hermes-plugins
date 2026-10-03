"""Tests for the reminder ledger + rules + middleware (no Hermes, no network).

Run: python3 tests/test_ledger.py   (or pytest)
"""

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ledger  # noqa: E402
import rules  # noqa: E402


def _db():
    return ledger.connect(Path(tempfile.mkdtemp()) / "test.db")


def _plugin(db_path):
    os.environ["REMINDER_DB"] = str(db_path)
    spec = importlib.util.spec_from_file_location(
        "reminder_under_test", ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["reminder_under_test"] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- detection

def test_imperatives_open_checks():
    assert rules.detect_kinds("Remove the graffiti plugin") == ["remove"]
    assert rules.detect_kinds("yes and I also want you to retire all the old "
                              "plug-ins and remove them from the current Hermes.") == ["remove"]
    assert "install" in rules.detect_kinds("Install the API key cron")
    assert "secret" in rules.detect_kinds("Install the API key cron")
    assert rules.detect_kinds("удали старые плагины") == ["remove"]
    assert rules.detect_kinds("Please update hermes to the latest version") == ["update"]
    assert rules.detect_kinds("Can you install ffmpeg?") == ["install"]


def test_discussion_opens_nothing():
    assert rules.detect_kinds("What time is it?") == []
    assert rules.detect_kinds(
        "Here's how the checker would work. Sequence: extend the checks, install "
        "the plugins here in shadow mode, then flip to active. Want me to start? "
        "How would you design such a system?") == []
    assert rules.detect_kinds("Should we remove it?") == []
    assert rules.detect_kinds("I added a note about the token yesterday.") == []
    assert rules.detect_kinds("Add milk to my list") == []
    assert rules.detect_kinds("Update me on the news") == []
    assert rules.detect_kinds("[CONTEXT COMPACTION — REFERENCE ONLY] remove x") == []
    assert rules.detect_kinds("root@kuzin:~# python3 install.py\nTraceback") == []


# ---------------------------------------------------------------- evidence

def _touch(i, tool, text, ran=1, ok=1):
    return (i, tool, text, ran, ok)


def test_remove_closes_only_after_later_verification():
    rows = [_touch(1, "terminal", "ls ~/.hermes/plugins"),
            _touch(2, "terminal", "hermes plugins remove foo")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "acted"
    rows.append(_touch(3, "terminal", "hermes plugins list"))
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_same_command_action_then_check():
    rows = [_touch(1, "terminal", "rm -rf /tmp/xdir && ls /tmp/xdir")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"
    rows = [_touch(1, "terminal", "ls /tmp/xdir; rm -rf /tmp/xdir")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "acted"


def test_probe_must_name_the_target():
    rows = [_touch(1, "terminal", "rm -rf ~/.hermes/plugin-data/agent-plugin-response-critic"),
            _touch(2, "read_file", '{"path": "/root/notes/other.py"}')]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "acted"
    rows.append(_touch(3, "terminal", "ls ~/.hermes/plugin-data/ | grep critic"))
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_later_change_reopens_check():
    rows = [_touch(1, "terminal", "hermes plugins remove foo"),
            _touch(2, "terminal", "hermes plugins list"),
            _touch(3, "terminal", "sed -i 's/foo//' /etc/cron.d/foo-job")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "acted"


def test_status_subcommands_and_verify_scripts_count():
    rows = [_touch(1, "terminal", "docker compose -f /opt/app/compose.yml up -d"),
            _touch(2, "terminal", "python3 /root/app/verify.py")]
    assert rules.evaluate(rows, ["install"])["install"][0] == "verified"
    rows = [_touch(1, "terminal", "hermes config set agent.x '{}'"),
            _touch(2, "terminal", "hermes config get agent")]
    assert rules.evaluate(rows, ["install"])["install"][0] == "verified"


def test_later_probe_does_not_mask_earlier_status_check():
    rows = [_touch(1, "terminal", "hermes plugins remove foo"),
            _touch(2, "terminal", "hermes plugins list | head -5; tail -3 /var/log/x.log")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_shell_variables_are_expanded():
    rows = [_touch(1, "terminal", "S=/tmp/scratch/hp-push; rm -rf $S; git clone x $S && cd $S && ls -la ${S}")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_unnameable_target_accepts_any_probe():
    rows = [_touch(1, "terminal", "rm -rf \"$TARGET_DIR\""),
            _touch(2, "terminal", "ls -d /root/.hermes/cache/scratch/hp-push")]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_unrelated_writes_do_not_reopen_removal():
    rows = [_touch(1, "terminal", "hermes plugins remove foo"),
            _touch(2, "terminal", "hermes plugins list"),
            _touch(3, "write_file", '{"path": "/root/proj/app.py", "content": "x"}')]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "verified"


def test_doc_writes_are_not_install_actions():
    rows = [_touch(1, "write_file", '{"path": "/root/proj/README.md", "content": "x"}')]
    assert rules.evaluate(rows, ["install"])["install"][0] == "open"


def test_failed_action_does_not_count_and_failed_check_does_not_verify():
    rows = [_touch(1, "terminal", "hermes plugins remove foo", ran=1, ok=0)]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "open"
    rows = [_touch(1, "terminal", "hermes plugins remove foo"),
            _touch(2, "terminal", "hermes plugins list", ran=0, ok=0)]
    assert rules.evaluate(rows, ["remove"])["remove"][0] == "acted"


def test_install_and_schedule_via_tools():
    rows = [_touch(1, "terminal", "pip install httpx"),
            _touch(2, "terminal", "python3 -c 'import httpx'")]
    assert rules.evaluate(rows, ["install"])["install"][0] == "verified"
    cron = json.dumps({"action": "create", "schedule": "0 9 * * *"})
    rows = [_touch(1, "cronjob_manage", cron),
            _touch(2, "cronjob_manage", json.dumps({"action": "list"}))]
    assert rules.evaluate(rows, ["schedule"])["schedule"][0] == "verified"


def test_result_status():
    assert rules.result_status('{"output": "", "exit_code": 0}') == (1, 1)
    assert rules.result_status('{"output": "x", "exit_code": 2}', "rm x") == (1, 0)
    assert rules.result_status('{"output": "", "exit_code": 1}', "ls | grep foo") == (1, 1)
    assert rules.result_status('{"error": "boom"}') == (0, 0)
    assert rules.result_status("plain text") == (1, 1)


def test_redaction():
    out = rules.redact("curl -H 'Authorization: Bearer sk-abc123' API_KEY=supersecret")
    assert "sk-abc123" not in out and "supersecret" not in out


# ---------------------------------------------------------------- rendering

def test_render_and_failure_hint():
    obligations = [("remove", rules.CHECKLISTS["remove"])]
    block = rules.render(obligations, {"remove": ("acted", "rm x")}, None)
    assert block.startswith(rules.MARKER) and "not verified yet" in block
    assert rules.render(obligations, {"remove": ("verified", "")}, None) == ""
    block = rules.render([], {}, ("terminal: curl x", 2))
    assert "switch method" in block


def test_inject_shapes():
    block = "[reminder] x"
    chat = {"messages": [{"role": "user", "content": "hi"}]}
    out = rules.inject(chat, block, "chat_completions")
    assert out["messages"][-1] == {"role": "system", "content": block}
    assert chat["messages"][-1]["role"] == "user"  # original untouched
    anth = {"system": "s", "messages": [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}]}
    out = rules.inject(anth, block, "anthropic_messages")
    assert out["messages"][-1]["content"][-1] == {"type": "text", "text": block}
    assert all(m["role"] != "system" for m in out["messages"])
    assert rules.inject({"input": []}, block, "codex_responses") is None
    assert rules.latest_user_text(out) == ""  # hint is never read back as the request


# ---------------------------------------------------------------- ledger

def test_ledger_open_resolve_and_migration():
    con = _db()
    added = ledger.open_task(con, "t1", "s1", "Remove x", ["remove"])
    assert added == ["remove"]
    assert ledger.open_task(con, "t1", "s1", "Remove x", ["remove"]) == []
    ledger.resolve_obligation(con, "t1", "remove", "ls shows absence")
    assert ledger.open_obligations(con, "t1") == []

    legacy = Path(tempfile.mkdtemp()) / "legacy.db"
    raw = sqlite3.connect(str(legacy))
    raw.executescript(ledger.SCHEMA)
    raw.execute("INSERT INTO obligations (task_hash, kind, detail, created_at) "
                "VALUES ('old', 'install', 'x', 0)")
    raw.commit()
    raw.close()
    con = ledger.connect(legacy)
    assert ledger.open_obligations(con, "old") == []
    cols = {r[1] for r in con.execute("PRAGMA table_info(touches)")}
    assert {"ran", "sig", "result_hash"} <= cols


# ---------------------------------------------------------------- middleware

def _request(text):
    return {"model": "m", "messages": [{"role": "system", "content": "sys"},
                                       {"role": "user", "content": text}]}


def test_middleware_end_to_end_per_turn():
    db = Path(tempfile.mkdtemp()) / "mw.db"
    plugin = _plugin(db)
    ids = {"session_id": "sA", "turn_id": "sA:task:1", "task_id": "task",
           "api_mode": "chat_completions"}

    out = plugin._gate_request(request=_request("Remove the foo plugin"), **ids)
    assert out and "(remove)" in out["request"]["messages"][-1]["content"]

    def run(cmd, result):
        return plugin._observe_tool(tool_name="terminal", args={"command": cmd},
                                    next_call=lambda a: result, **ids)

    run("hermes plugins remove foo", '{"output": "removed", "exit_code": 0}')
    out = plugin._gate_request(request=_request("Remove the foo plugin"), **ids)
    assert "not verified yet" in out["request"]["messages"][-1]["content"]

    run("hermes plugins list", '{"output": "bar", "exit_code": 0}')
    assert plugin._gate_request(request=_request("Remove the foo plugin"), **ids) is None

    # A later change reopens the check.
    run("rm -rf /root/.hermes/foo-data", '{"output": "", "exit_code": 0}')
    out = plugin._gate_request(request=_request("Remove the foo plugin"), **ids)
    assert out and "not verified yet" in out["request"]["messages"][-1]["content"]
    run("ls /root/.hermes/ | grep foo-data", '{"output": "", "exit_code": 1}')
    assert plugin._gate_request(request=_request("Remove the foo plugin"), **ids) is None

    # Another session with the same text does not inherit this turn's state.
    other = dict(ids, session_id="sB", turn_id="sB:task:9")
    out = plugin._gate_request(request=_request("Remove the foo plugin"), **other)
    assert out and "(remove)" in out["request"]["messages"][-1]["content"]

    # Discussion turn: nothing injected.
    talk = dict(ids, turn_id="sA:task:2")
    assert plugin._gate_request(request=_request("How would you design it?"), **talk) is None


def test_middleware_repeated_failure_and_error_passthrough():
    db = Path(tempfile.mkdtemp()) / "mw2.db"
    plugin = _plugin(db)
    ids = {"session_id": "s", "turn_id": "s:t:1", "task_id": "t",
           "api_mode": "chat_completions"}
    assert plugin._gate_request(request=_request("What is the weather?"), **ids) is None
    for _ in range(2):
        plugin._observe_tool(tool_name="terminal", args={"command": "curl https://x.test"},
                             next_call=lambda a: '{"output": "", "exit_code": 7}', **ids)
    out = plugin._gate_request(request=_request("What is the weather?"), **ids)
    assert out and "switch method" in out["request"]["messages"][-1]["content"]

    class Boom(Exception):
        pass

    def explode(_):
        raise Boom("x")

    try:
        plugin._observe_tool(tool_name="terminal", args={"command": "x"},
                             next_call=explode, **ids)
    except Boom:
        pass
    else:
        raise AssertionError("original error must propagate")


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
