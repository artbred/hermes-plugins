"""Proofgate: checklist hints and YAML/Jev outcome enforcement.

The gym-survey gate reviews actual results and proposed finals, then returns an
explicit internal tool call through native execution middleware when authorized
work remains. See enforcement.py and README.md for bounds and display/tool
availability constraints. Checklist hints retain their existing fail-open policy;
outcome-review uncertainty uses bounded continuation and explicit INCOMPLETE.
"""

from __future__ import annotations

import logging
import threading

from . import enforcement, jev, ledger, rules

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
        enforcement.observe_result(kwargs, result, caught)
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
        enforcement.prepare(**kwargs)
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
    policy = enforcement._policy()
    if not policy:
        raise ValueError("Proofgate enforcement registry is invalid; refusing activation")
    ctx.register_tool(
        name=enforcement.bridge.TOOL_NAME, toolset="proofgate",
        schema=enforcement.bridge.tool_schema()["function"], handler=enforcement.control_tool,
        description="Internal policy continuation control")
    ctx.register_middleware("tool_execution", _observe_tool)
    ctx.register_middleware("llm_request", _gate_request)
    ctx.register_middleware("llm_execution", enforcement.execute)
    ctx.register_hook("transform_llm_output", enforcement.transform)
