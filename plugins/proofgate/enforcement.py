"""Turn-scoped completion control. Semantic decisions live in scenarios.yaml.

A control tool is policy-generated, never claimed as provider/external evidence.
The native loop owns execution, approvals, cancellation and its normal budget.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import uuid

from . import bridge, jev, ledger, rules

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()
_TOKENS: dict[str, tuple[str, str, str, str, str]] = {}
_MAX_TOKENS = 256


def _policy():
    registry = jev.load_registry()
    return registry.get("enforcement") if registry else None


def _identity(kwargs):
    return str(kwargs.get("session_id") or ""), str(kwargs.get("turn_id") or "")


def _clean(value, limit=12000):
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = type(value).__name__
    try:
        from agent.redact import redact_sensitive_text
        value = redact_sensitive_text(value, force=True)
    except ImportError:
        pass
    clean = rules.redact(value)
    return clean if len(clean) <= limit else clean[:limit - 12] + " [TRUNCATED]"


def _choice(verdicts, phase, question):
    return verdicts.get(phase, {}).get(question, (None,))[0]


def _review(context, phase, question):
    try:
        return _choice(jev.judge_state(context, phase), phase, question)
    except Exception:
        return None


def _scope_input(request, policy):
    texts = rules.user_texts(request)
    current = texts[-1] if texts else ""
    if not current or re.search(policy["skip_pattern"], current):
        return None
    direct = re.search(policy["action_pattern"], current) and re.search(
        policy["subject_pattern"], current)
    previous = texts[-2] if len(texts) > 1 else ""
    carried = (re.search(policy["carry_pattern"], current)
               and not re.search(policy["skip_pattern"], previous)
               and re.search(policy["action_pattern"], previous)
               and re.search(policy["subject_pattern"], previous))
    if not direct and not carried:
        return None
    goal = previous if carried else current
    clean_current, clean_goal = _clean(current, 8000), _clean(goal, 8000)
    return {"current_user": clean_current, "goal": clean_goal,
            "scope_truncated": (len(current) > 8000 or len(goal) > 8000
                                or clean_current.endswith(" [TRUNCATED]")
                                or clean_goal.endswith(" [TRUNCATED]"))}


def prepare(**kwargs):
    """Resolve current authority once per scope revision, not per candidate.

    State is keyed by native session AND turn; saving a changed scope cannot
    reset the separate durable retry counter. No historical goal search.
    """
    session, turn = _identity(kwargs)
    request = kwargs.get("request")
    policy = _policy()
    if not session or not turn or not isinstance(request, dict) or not policy:
        return
    texts = rules.user_texts(request)
    # A carried goal is part of the revision too: unchanged "continue" must not
    # revive a token issued for a different immediately preceding objective.
    current_hash = rules.short_hash(json.dumps(texts[-2:], ensure_ascii=False))
    con = ledger.connect()
    try:
        state = ledger.load_gate(con, session, turn)
        if state and state.get("current_hash") == current_hash:
            return
        context = _scope_input(request, policy)
        state = {"current_hash": current_hash, "scope": "skip"}
        if context:
            answer = None if context["scope_truncated"] else _review(context, "scope", "active")
            state.update(context)
            state["scope"] = "active" if answer == "active" else (
                "skip" if answer == "no" else "uncertain")
        ledger.save_gate(con, session, turn, state)
    finally:
        con.close()


def observe_result(kwargs, result, error=None):
    """Store bounded actual results, never inferred success from an exit code."""
    session, turn = _identity(kwargs)
    if not session or not turn or kwargs.get("tool_name") == bridge.TOOL_NAME:
        return
    if kwargs.get("tool_name") == "tool_call":
        args = kwargs.get("args", kwargs.get("request"))
        calls = args.get("calls") if isinstance(args, dict) else None
        # Native dispatch records actual tools separately. Never promote an
        # outer wrapper containing policy control (even a mixed batch) to
        # external evidence.
        if isinstance(calls, list) and any(
                isinstance(call, dict) and call.get("name") == bridge.TOOL_NAME
                for call in calls):
            return
        output = result
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                output = None
        if (isinstance(output, dict) and output.get("policy_generated") is True
                and output.get("external_evidence") is False):
            return
    con = ledger.connect()
    try:
        state = ledger.load_gate(con, session, turn)
        if state and state.get("scope") == "skip":
            return
        value = {"error_type": type(error).__name__} if error is not None else result
        ledger.record_evidence(con, session, turn, str(kwargs.get("tool_name") or "tool"),
                               _clean(value, 2000))
    finally:
        con.close()


def _terminal(con, session, turn, state, policy, reason, message="incomplete"):
    text = policy["messages"][message]
    state.update(status="incomplete", reason=reason, terminal_text=text)
    state.pop("approved_hash", None)
    ledger.save_gate(con, session, turn, state)
    return text


def _continue(con, session, turn, state, policy, response, mode, request, phase, answer, reason):
    """Reserve a shared bounded retry and issue a scope-bound internal token."""
    if not bridge.supports_tool(request, mode):
        text = _terminal(con, session, turn, state, policy,
                         "continuation_tool_unavailable", "unavailable")
        return bridge.finish_response(response, mode, text)
    if ledger.claim_retry(con, session, turn, policy["max_retries"]) is None:
        text = _terminal(con, session, turn, state, policy, "retry_cap:" + reason)
        return bridge.finish_response(response, mode, text)
    scenario = policy[phase]["scenarios"][0]
    block_id = scenario["on_match"].get(answer or "none")
    directive = scenario["blocks"].get(block_id, "")
    if not directive:
        text = _terminal(con, session, turn, state, policy, "continuation_policy_missing")
        return bridge.finish_response(response, mode, text)
    token = uuid.uuid4().hex
    with _LOCK:
        if len(_TOKENS) >= _MAX_TOKENS:
            _TOKENS.pop(next(iter(_TOKENS)))
        _TOKENS[token] = (session, turn, state["current_hash"], state["scope"], directive)
    state.update(status="verifying_scope" if phase == "scope" else "continuing",
                 reason=reason, pending_token=token)
    state.pop("approved_hash", None)
    ledger.save_gate(con, session, turn, state)
    return bridge.continue_response(response, mode, token, request=request)


def execute(**kwargs):
    """Exactly one downstream execution. Rejection returns a native tool round."""
    response = kwargs["next_call"](kwargs["request"])
    session, turn = _identity(kwargs)
    policy = _policy()
    if not session or not turn or not policy:
        return response
    con = None
    state = None
    try:
        # Also works when an earlier request middleware returned a replacement.
        prepare(**kwargs)
        con = ledger.connect()
        state = ledger.load_gate(con, session, turn)
        if not state or state.get("scope") == "skip":
            return response
        mode = str(kwargs.get("api_mode") or "")
        candidate = bridge.inspect_response(response, mode)
        if candidate is None:
            kind = bridge.response_kind(response, mode)
            if kind == "safe_end":
                state.update(status="native_safe_end")
                ledger.save_gate(con, session, turn, state)
            elif kind == "unsupported":
                _terminal(con, session, turn, state, policy, "unsupported_response", "unavailable")
            return response
        if state.get("terminal_text"):
            return bridge.finish_response(response, mode, state["terminal_text"])
        if state.get("scope") == "uncertain":
            if state.get("scope_truncated"):
                ledger.record_review(con, session, turn, _clean(candidate), "unverified", "scope_truncated")
                text = _terminal(con, session, turn, state, policy, "scope_truncated")
                return bridge.finish_response(response, mode, text)
            # Only an executed internal verification round enables another scope
            # judgment. Its candidate is untrusted reasoning, never authority.
            if state.pop("scope_verification_ready", False):
                context = {"goal": state["goal"], "current_user": state["current_user"],
                           "scope_truncated": False,
                           "tool_results": ledger.read_evidence(con, session, turn),
                           "internal_verification": _clean(candidate)}
                answer = _review(context, "scope", "active") if len(candidate) <= 12000 else None
                state["scope"] = "active" if answer == "active" else (
                    "skip" if answer == "no" else "uncertain")
                ledger.save_gate(con, session, turn, state)
                if state["scope"] == "skip":
                    return response
            if state["scope"] == "uncertain":
                ledger.record_review(con, session, turn, _clean(candidate), "unverified", "scope_uncertain")
                return _continue(con, session, turn, state, policy, response, mode,
                                 kwargs["request"], "scope", None, "scope_uncertain")
        evidence = ledger.read_evidence(con, session, turn)
        context = {"goal": state["goal"], "current_user": state["current_user"],
                   "tool_results": evidence, "candidate_final": _clean(candidate)}
        answer = _review(context, "outcome", "result") if len(candidate) <= 12000 else None
        if answer in ("complete", "pending") and not evidence:
            answer = None
        reason = answer or "reviewer_unverified"
        ledger.record_review(con, session, turn, _clean(candidate), answer or "unverified", reason)
        if answer in ("complete", "pending", "safe_end"):
            state.update(status=answer, approved_hash=rules.short_hash(candidate))
            ledger.save_gate(con, session, turn, state)
            return response
        if answer == "blocked":
            text = _terminal(con, session, turn, state, policy, reason, "blocked")
            text += "\n\n" + _clean(candidate, 6000)
            state["terminal_text"] = text
            ledger.save_gate(con, session, turn, state)
            return bridge.finish_response(response, mode, text)
        return _continue(con, session, turn, state, policy, response, mode,
                         kwargs["request"], "outcome", answer, reason)
    except Exception:
        # Hermes execution middleware fails open on callback exceptions. Keep
        # gate failures inside this frame and use the final transform safety net
        # if a provider shape cannot be rewritten safely.
        logger.warning("proofgate: outcome control failed; completion remains unverified")
        if state and state.get("scope") != "skip":
            try:
                text = _terminal(con, session, turn, state, policy, "gate_error")
                return bridge.finish_response(response, str(kwargs.get("api_mode") or ""), text)
            except Exception:
                pass
        return response
    finally:
        if con is not None:
            con.close()


def control_tool(args, **kwargs):
    """Consume one opaque policy token. No I/O, approvals, or external action."""
    policy = _policy()
    token = args.get("token") if isinstance(args, dict) else None
    with _LOCK:
        pending = _TOKENS.pop(token, None) if isinstance(token, str) else None
    if not pending:
        return json.dumps({"policy_generated": True, "external_evidence": False,
                           "directive": policy["messages"]["invalid_token"] if policy else ""})
    session, turn, current_hash, scope, directive = pending
    con = ledger.connect()
    try:
        state = ledger.load_gate(con, session, turn)
        expected_status = "verifying_scope" if scope == "uncertain" else "continuing"
        if (not policy or not state or state.get("scope") != scope
                or state.get("current_hash") != current_hash
                or state.get("pending_token") != token
                or state.get("status") != expected_status or state.get("terminal_text")
                or state.get("scope_truncated")
                or any(kwargs.get(key) is not None and str(kwargs[key]) != expected
                       for key, expected in (("session_id", session), ("turn_id", turn)))):
            directive = policy["messages"]["invalid_token"] if policy else ""
        else:
            state.pop("pending_token", None)
            if scope == "uncertain":
                state["scope_verification_ready"] = True
            ledger.save_gate(con, session, turn, state)
    finally:
        con.close()
    return json.dumps({"policy_generated": True, "external_evidence": False,
                       "session_id": session, "turn_id": turn, "directive": directive})


def transform(**kwargs):
    """Final-only safety net, including the native budget-summary bypass.

    This hook cannot continue or alter native turn bookkeeping. It only ensures
    a terminal text never presents an unreviewed outcome as verified delivery.
    """
    session, turn = _identity(kwargs)
    policy = _policy()
    if not session or not turn or not policy:
        return None
    con = ledger.connect()
    try:
        state = ledger.load_gate(con, session, turn)
        if not state or state.get("scope") == "skip":
            return None
        if state.get("terminal_text"):
            return state["terminal_text"]
        if state.get("status") == "native_safe_end":
            return None
        if state.get("approved_hash") == rules.short_hash(kwargs.get("response_text") or ""):
            return None
        text = _terminal(con, session, turn, state, policy, "unreviewed_terminal_path")
        ledger.record_review(con, session, turn, _clean(kwargs.get("response_text") or ""),
                             "unverified", "unreviewed_terminal_path")
        return text
    finally:
        con.close()
