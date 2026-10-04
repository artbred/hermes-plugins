"""Proofgate: proof before "done" — per-turn checklists, evidence, nudges.

- ``tool_execution`` middleware records every tool call (tool, redacted
  argument text, ran/ok, signature, result hash) against the current turn,
  keyed by the Hermes turn id it is called with — no module-global task.
- ``llm_request`` middleware opens checklist obligations from the turn's
  request (imperative verbs only), closes them automatically when a read-only
  verification ran after the action, and injects the remaining open checks,
  any injection blocks fired by the Jev scenario registry (e.g. ompx routing
  for substantial coding turns), and a method-switch hint after repeated
  identical failures.

Local SQLite and regexes first. For a non-question, non-internal request
longer than 40 chars, the first model call of the turn makes ONE bounded Jev
call (jev.py) asking every question in scenarios.yaml. The `checklist`
scenario adds kinds (merged with the regex kinds); other scenarios fire
injection blocks via their on_match. Any Jev failure means regex-only kinds
and no blocks. Every middleware body is exception-swallowed, so a plugin bug
degrades to a no-op.
"""

from __future__ import annotations

import logging
import threading

from . import jev, ledger, rules

logger = logging.getLogger(__name__)

_STATE_LOCK = threading.Lock()
_TURNS: dict = {}          # session key -> {"turn", "thash", "routes"}
_MAX_SESSIONS = 256
_FAIL_REPEAT = 2
_PRUNED = {"done": False}
_JEV_MIN_CHARS = 40


def _jev_eligible(text: str) -> bool:
    """Jev judges only substantive requests (regex hits included)."""
    stripped = (text or "").strip()
    return (len(stripped) > _JEV_MIN_CHARS and not rules._INTERNAL.search(text)
            and not rules._is_question(text))


def _judge_request(text: str):
    """(kinds, routes): regex kinds merged with at most one Jev judgment's
    checklist kinds, plus the registry injection blocks it fired."""
    kinds = rules.detect_kinds(text) if text else []
    if not _jev_eligible(text):
        return kinds, ()
    try:
        verdicts = jev.judge_turn(text)
        extra = [choice for choice, _confidence in verdicts.get("checklist", {}).values()
                 if choice in rules.CHECKLISTS]
        routes = tuple(jev.injections(verdicts))
    except Exception:
        return kinds, ()
    return sorted(set(kinds) | set(extra)), routes


def _turn_hash(kwargs) -> str:
    turn = str(kwargs.get("turn_id") or "")
    if not turn:
        turn = f"{kwargs.get('session_id') or ''}:{kwargs.get('task_id') or ''}"
    return rules.short_hash(turn) if turn.strip(":") else ""


def _redact(text: str) -> str:
    try:
        from agent.redact import redact_sensitive_text
        text = redact_sensitive_text(text, force=True)
    except Exception:
        pass
    return rules.redact(text)


def _observe_tool(**kwargs):
    next_call = kwargs.get("next_call")
    payload = kwargs.get("args", kwargs.get("request"))
    tool = str(kwargs.get("tool_name") or "tool")
    caught = None
    result = None
    try:
        result = next_call(payload) if next_call else payload
    except Exception as error:  # re-raised unchanged below
        caught = error
    try:
        thash = _turn_hash(kwargs)
        if thash:
            text = rules.action_text(tool, payload)
            if caught is not None:
                ran, ok = 0, 0
            else:
                ran, ok = rules.result_status(
                    result, text if tool in rules.TERMINAL_TOOLS else "")
            clean = _redact(text)
            con = ledger.connect()
            try:
                ledger.record_touch(con, thash, tool, clean, bool(ok), bool(ran),
                                    rules.signature(tool, clean),
                                    rules.result_hash(result))
            finally:
                con.close()
    except Exception:
        logger.debug("proofgate: touch not recorded", exc_info=True)
    if caught is not None:
        raise caught
    return result


def _remember_turn(session_key: str, turn: str, thash: str) -> bool:
    """True when this is the first request seen for this turn."""
    with _STATE_LOCK:
        current = _TURNS.get(session_key)
        if current and current["turn"] == turn:
            return False
        if len(_TURNS) >= _MAX_SESSIONS:
            _TURNS.pop(next(iter(_TURNS)))
        _TURNS[session_key] = {"turn": turn, "thash": thash, "routes": ()}
        return True


def _turn_routes(session_key: str, turn: str, routes=None) -> tuple:
    """Set (when `routes` is given) and return this turn's injection blocks."""
    with _STATE_LOCK:
        current = _TURNS.get(session_key)
        if not current or current["turn"] != turn:
            return ()
        if routes is not None:
            current["routes"] = tuple(routes)
        return current["routes"]


def _repeated_failure(rows, window: int = 4):
    """(signature, count) for a call that failed >= N times, still unresolved.

    Stale failures expire: once `window` later touches happened since a
    signature's last failure, the model has moved on (switched method)."""
    counts, last_fail = {}, {}
    for idx, (_tid, tool, text, _ran, ok) in enumerate(rows):
        sig = rules.signature(tool, text or "")
        if ok:
            counts.pop(sig, None)
        else:
            counts[sig] = counts.get(sig, 0) + 1
            last_fail[sig] = idx
    recent = {s: c for s, c in counts.items() if len(rows) - 1 - last_fail[s] < window}
    worst = max(recent.items(), key=lambda kv: kv[1], default=None)
    return worst if worst and worst[1] >= _FAIL_REPEAT else None


def _gate_request(**kwargs):
    request = kwargs.get("request")
    try:
        if not isinstance(request, dict):
            return None
        thash = _turn_hash(kwargs)
        if not thash:
            return None
        session_key = str(kwargs.get("session_id") or kwargs.get("task_id") or "")
        turn = str(kwargs.get("turn_id") or thash)
        first = _remember_turn(session_key, turn, thash)
        text = rules.latest_user_text(request) if first else ""
        # Before opening the ledger: the Jev call may take a few seconds.
        if first:
            kinds, routes = _judge_request(text)
            routes = _turn_routes(session_key, turn, routes)
        else:
            kinds, routes = [], _turn_routes(session_key, turn)
        con = ledger.connect()
        try:
            if not _PRUNED["done"]:
                _PRUNED["done"] = True
                ledger.prune(con)
            if kinds:
                ledger.open_task(con, thash, session_key, _redact(text)[:200], kinds)
            obligations = ledger.active_obligations(con, thash)
            rows = ledger.touches(con, thash)
            failure = _repeated_failure(rows)
            if not obligations and not failure and not routes:
                return None
            state = rules.evaluate(rows, [k for k, _, _ in obligations])
            for kind, _detail, status in obligations:
                now = state.get(kind, ("open", ""))
                if now[0] == "verified" and status == "open":
                    ledger.resolve_obligation(con, thash, kind, now[1])
                elif now[0] != "verified" and status == "done":
                    ledger.reopen_obligation(con, thash, kind)
            # Secret hygiene only nags once something secret-bearing was touched.
            shown = [(k, d) for k, d, _ in obligations
                     if k != "secret" or state.get(k, ("open",))[0] == "acted"]
            block = rules.render(shown, state, failure, routes=routes)
        finally:
            con.close()
        if not block:
            return None
        new_request = rules.inject(request, block, str(kwargs.get("api_mode") or ""))
        if new_request is None:
            return None
        return {"request": new_request, "source": "proofgate",
                "reason": "open verification checks or Jev scenario injection"}
    except Exception:
        logger.debug("proofgate: gate skipped", exc_info=True)
        return None


def register(ctx):
    ctx.register_middleware("tool_execution", _observe_tool)
    ctx.register_middleware("llm_request", _gate_request)
