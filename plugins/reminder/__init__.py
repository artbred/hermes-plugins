"""Reminder plugin: obligation ledger + gatekeeper injection.

- ``tool_execution`` middleware observes every tool call and records a short
  touch (tool name, argument summary, ok/error) against the current task.
- ``llm_request`` middleware detects task verbs in the latest user message,
  opens checklist obligations once per task, and injects the open checks back
  into the request so the model sees them while working.

Everything is local SQLite and regexes: no network, no Jev calls, no added
latency beyond microseconds. Any exception is swallowed (middleware failures
only warn once and skip) — the plugin can never break a run.
"""

from __future__ import annotations

import threading

from . import ledger

_STATE = threading.Lock()
_CURRENT_TASK: dict = {"hash": None}


def _latest_user_text(request) -> str:
    try:
        messages = request.get("messages", []) if isinstance(request, dict) else []
    except Exception:
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            text = "\n".join(parts).strip()
            if text:
                return text
    return ""


def _summarize_kwargs(kwargs) -> str:
    bits = []
    for key, value in kwargs.items():
        if key in ("next_call", "request", "original_request"):
            continue
        text = str(value)
        bits.append(f"{key}={text[:120]}")
        if len(bits) >= 4:
            break
    return "; ".join(bits)[:300]


def _observe_tool(**kwargs):
    next_call = kwargs.get("next_call")
    payload = kwargs.get("args", kwargs.get("request"))
    caught = None
    try:
        result = next_call(payload) if next_call else payload
        ok = True
    except Exception as error:
        caught = error
        result = None
        ok = False
    try:
        with _STATE:
            thash = _CURRENT_TASK["hash"]
        if thash:
            tool = str(kwargs.get("tool_name", "tool"))
            con = ledger.connect()
            try:
                ledger.record_touch(con, thash, tool,
                                    _summarize_kwargs(kwargs), ok)
            finally:
                con.close()
    except Exception:
        pass
    if not ok:
        raise caught  # type: ignore[misc]
    return result


def _gate_request(**kwargs):
    request = kwargs.get("request")
    try:
        text = _latest_user_text(request)
        if text:
            con = ledger.connect()
            try:
                thash, _ = ledger.ensure_task(con, text)
                if thash:
                    with _STATE:
                        _CURRENT_TASK["hash"] = thash
                    block = ledger.reminder_block(con, thash)
                    if block and isinstance(request, dict):
                        request = dict(request)
                        messages = list(request.get("messages", []))
                        messages.append({"role": "system", "content": block})
                        request["messages"] = messages
                        return {"request": request, "source": "reminder"}
            finally:
                con.close()
    except Exception:
        pass
    return None


def register(ctx):
    ctx.register_middleware("tool_execution", _observe_tool)
    ctx.register_middleware("llm_request", _gate_request)
