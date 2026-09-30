"""Bounded pre-delivery verification, optionally assisted by an outcome-only tool.

The original agent runs first with its normal tools. A validated cooperative
review may accept its outcome, authorize a terse verified-memory acknowledgment,
or send correction/recovery guidance back through the existing ``pre_verify``
continuation gate. Default standalone behavior remains Kimi K3 followed by
OpenRouter Muse Spark Contributor at maximum reasoning effort.

Non-file verification uses a plugin-only compatibility adapter that stamps a
transient .md sentinel into the agent's private mutation set. No file or core
source is written. This depends on host internals and is not a public universal
pre-execution policy seam. Evidence and model-bound drafts are egress-redacted.
"""

import os
import re
import json
import logging
import logging.handlers
import datetime
import threading
import time
import contextvars
import httpx

logger = logging.getLogger("hermes.plugin.response_critic")

DEFAULT_MAX_ITERATIONS = 5
MIN_RESPONSE_CHARS = 30
REQUEST_TIMEOUT = 300.0
OPENROUTER_TIMEOUT = 300.0  # Both providers share the five-minute caller deadline.

# Doc extension on purpose: filtered by agent/verification_stop.py.
SENTINEL = ".hermes/response-critic-gate.md"

# Internal ladder (OpenRouter-style names). Kimi K3 accepts only low/high/max.
EFFORT_LADDER = ["minimal", "low", "medium", "high", "xhigh", "max"]
DEFAULT_MIN_EFFORT = "low"
DEFAULT_MAX_EFFORT = "xhigh"
KIMI_EFFORT_MAP = {
    "minimal": "low", "low": "low", "medium": "high",
    "high": "high", "xhigh": "max", "max": "max",
}

DEFAULT_JUDGE_MODEL = "k3"
DEFAULT_JUDGE_BASE_URL = "https://api.kimi.com/coding/v1"
DEFAULT_FALLBACK_MODEL = "meta/muse-spark-1.3-contributor"
DEFAULT_FALLBACK_EFFORT = "max"

_max_iterations = DEFAULT_MAX_ITERATIONS
_min_effort = DEFAULT_MIN_EFFORT
_max_effort = DEFAULT_MAX_EFFORT
_triage_enabled = True
_judge_model = DEFAULT_JUDGE_MODEL
_judge_base_url = DEFAULT_JUDGE_BASE_URL
_fallback_model = DEFAULT_FALLBACK_MODEL
_fallback_enabled = True
_fallback_effort = DEFAULT_FALLBACK_EFFORT
_review_budget_seconds = 300.0
_provider_cooldown_seconds = 300.0
_judge_deadline = contextvars.ContextVar('response_critic_deadline', default=None)
_review_session = contextvars.ContextVar('response_critic_session', default=None)
_review_workers = {}
_review_workers_lock = threading.Lock()
_provider_backoff = {}
_provider_backoff_lock = threading.Lock()


def _request_timeout(default):
    deadline = _judge_deadline.get()
    return max(.001, min(default, deadline - time.monotonic())) if deadline is not None else min(default, _review_budget_seconds)


def _provider_available(name):
    with _provider_backoff_lock:
        return time.monotonic() >= _provider_backoff.get(name, 0)


def _cool_provider(name):
    with _provider_backoff_lock:
        _provider_backoff[name] = time.monotonic() + _provider_cooldown_seconds

# Optional cooperative policy; standalone behavior stays enabled and unchanged.
_plugin_context = None
_critic_mode = "active"
_outcome_review_enabled = False
_outcome_review_mode = "shadow"
_OUTCOME_CONFIDENCE = 0.97
_OUTCOME_DISPOSITIONS = {"accept", "correct", "recover", "acknowledge", "handoff", "uncertain", "refusal"}
_acknowledgments: dict[str, dict] = {}

# Prior challenges for the turn in flight, keyed by session id. Lets the judge
# see what it already asked for so it cannot re-raise an objection the agent
# has no way to satisfy.
_turn_feedback: dict[str, list[str]] = {}
_feedback_lock = threading.Lock()

# Turn provenance comes from plugin hooks, not from text pretending to be a user.
_turn_context: dict[str, dict] = {}
_last_completed: dict[str, str] = {}
_trusted_internal_messages: dict[str, tuple[float, bool]] = {}
_context_lock = threading.Lock()
_HANDOFF = re.compile(
    r"\b(background|subagents?|research|work|results?|refactor|tests?|implementation)\b.{0,100}"
    r"\b(running|pending|finishing|waiting|awaiting|finish|arrive)\b|"
    r"\b(waiting|awaiting|finishing)\b.{0,100}\b(results?|research|comparison)\b",
    re.I | re.S,
)


def observe_gateway_input(event=None, **_kwargs):
    """Remember machine provenance only from the gateway-owned internal flag.

    Never skip dispatch/auth or classify an inbound human by a marker alone.
    Entries are consumed by pre_llm_call and expire, bounded to 128 notices.
    """
    import time
    if event is None:
        return None
    internal = bool(getattr(event, "internal", False))
    text = str(getattr(event, "text", "") or "").strip()
    if not text:
        return None
    now = time.monotonic()
    with _context_lock:
        expired = [k for k, (t, _) in _trusted_internal_messages.items() if now - t > 300]
        for key in expired:
            _trusted_internal_messages.pop(key, None)
        if len(_trusted_internal_messages) >= 128:
            _trusted_internal_messages.pop(next(iter(_trusted_internal_messages)), None)
        _trusted_internal_messages[text] = (now, internal)
    return None


def capture_turn_context(session_id="", user_message="", conversation_history=None,
                         parent_session_id="", turn_id="", **_kwargs):
    """Observe an already-started agent turn; never classify or skip execution."""
    import time
    import uuid
    body = _message_text(user_message)
    history = conversation_history or []
    current_index = next((i for i in range(len(history) - 1, -1, -1)
                          if isinstance(history[i], dict) and history[i].get("role") == "user"
                          and not _synthetic_user(history[i])), None)
    current = history[current_index] if current_index is not None else None
    if current and _message_text(current.get("content")) != body:
        current, current_index = None, None  # history can be prior-only on older hosts
    # Host-authored current metadata is authoritative. An older notification or
    # a quotation in a genuine human row cannot grant internal provenance.
    internal = bool(current and current.get("display_kind") == "internal_notification")
    now = time.monotonic()
    with _context_lock:
        if current is None:
            # Compatibility with older gateways: exact event match only, never
            # substring matching against a human quotation.
            stamp = _trusted_internal_messages.pop(body, None)
            internal = bool(stamp and now - stamp[0] <= 300 and stamp[1])
        else:
            _trusted_internal_messages.pop(body, None)
        prior = _last_completed.get(session_id, "")
        if not prior:
            for msg in reversed(history):
                if not isinstance(msg, dict) or msg.get("role") != "assistant" or msg.get("tool_calls"):
                    continue
                if msg.get("finish_reason") in {"verify_hook_continue", "verification_required"}:
                    continue
                candidate = msg.get("content")
                if isinstance(candidate, str) and candidate.strip():
                    candidate = candidate.strip()
                    if len(candidate) <= 1200 and _HANDOFF.search(candidate):
                        break
                    prior = candidate[:12000]
                    break
        _acknowledgments.pop(session_id, None)
        _turn_context[session_id] = {
            "user_message": body, "internal": internal,
            "prior_answer": prior, "parent_session_id": parent_session_id,
            "turn_id": str(turn_id or uuid.uuid4().hex), "explicit_turn_id": bool(turn_id),
            "history_start": current_index,
        }
        while len(_turn_context) > 256:
            oldest = next(iter(_turn_context))
            _turn_context.pop(oldest, None)
            _acknowledgments.pop(oldest, None)
    if internal and _critic_mode == "active":
        return {"context": (
            "This turn is a gateway-generated completion notice, not a new human request. "
            "If the original request was already answered and the results add no material "
            "correction, return exactly NO_REPLY. Do not repeat or paraphrase the previous "
            "conclusion. If a prior turn was only a pending-work handoff, finish the original "
            "task now. Material new corrections may be reported concisely."
        )}
    return None


def _synthetic_user(msg):
    return any(msg.get(flag) for flag in (
        "_pre_verify_hook_continue", "_verification_nudge", "_verification_ack",
    ))


def _background_pending(session_id: str) -> bool:
    """Read runtime status; never infer pending work from a promise alone."""
    if not session_id:
        return False
    try:
        from tools.async_delegation import list_async_delegations
        return any(
            row.get("status") in {"running", "stalling", "finalizing"}
            and session_id in {
                str(row.get("parent_session_id") or ""),
                str(row.get("origin_ui_session_id") or ""),
            }
            for row in list_async_delegations()
        )
    except Exception as exc:
        logger.debug("Critic: background status unavailable (%s)", type(exc).__name__)
        return False


def _is_background_handoff(text: str, session_id: str) -> bool:
    return len(text) <= 1200 and bool(_HANDOFF.search(text)) and _background_pending(session_id)


def record_completed_answer(session_id="", assistant_response="", **_kwargs):
    text = str(assistant_response or "").strip()
    if not text or text in {"NO_REPLY", "[SILENT]", "SILENT"}:
        return
    if _is_background_handoff(text, session_id):
        return
    with _context_lock:
        _last_completed[session_id] = text[:12000]
        # Do not clear turn context here: older hosts call this observer before
        # the output transform. capture_turn_context resets it on the next turn.
        # Bound session state without dropping the active entry.
        if len(_last_completed) > 256:
            oldest = next(iter(_last_completed))
            if oldest != session_id:
                _last_completed.pop(oldest, None)


def suppress_exact_internal_duplicate(response_text="", session_id="", turn_id="", **_kwargs):
    """Final-only transform; both duplicate and acknowledgment state are turn scoped."""
    with _context_lock:
        context = dict(_turn_context.get(session_id, {}))
        ack = dict(_acknowledgments.get(session_id, {}))
        if turn_id and context.get("explicit_turn_id") and context.get("turn_id") != str(turn_id):
            return None
        _turn_context.pop(session_id, None)
        _acknowledgments.pop(session_id, None)
    if _critic_mode != "active":
        return None
    prior = str(context.get("prior_answer") or "").strip()
    text = str(response_text or "").strip()
    if context.get("internal") and prior and text == prior:
        logger.info("Critic: suppressed exact duplicate on internal completion")
        return "NO_REPLY"
    if (_critic_mode == "active" and _outcome_review_mode == "active"
            and _outcome_review_enabled and not context.get("internal")
            and ack.get("turn_id") == context.get("turn_id") and ack.get("draft") == text):
        with _context_lock:
            _last_completed[session_id] = "Added."
        return "Added."
    return None

JUDGE_PROMPT_TEMPLATE = """You are an external validator for an AI assistant. You are shown the
assistant's draft final answer before it reaches the user, plus a digest of the tool
evidence the assistant actually gathered this turn (when present). The user never sees
you and must never learn you exist.

TODAY'S DATE IS {today}. This is authoritative and comes from the system clock, not from
your training data, which is older. A claim that something was checked, browsed or
verified "today" or on {today} is therefore ORDINARY AND PLAUSIBLE. Never object that
such a date is "in the future", "future-dated", or impossible. Never demand proof that
today is today.

Decide whether the draft can be delivered.

CHALLENGE it only for real defects:
1. Fabrication — invented facts, names, URLs, prices, dates, citations or statistics.
2. Unsupported success claims — the draft asserts something was deployed, verified,
   tested or confirmed, AND the evidence digest shows no such check was performed.
   If the digest shows the check (a command, API call or read whose output supports
   the claim), the claim is SUPPORTED — do not challenge it.
3. Violated user instructions — an explicit request was ignored (format, language,
   scope, or an inclusion/exclusion rule).
4. Broken output — malformed markdown, raw HTML tags, truncated text.
5. Incomplete work — the user asked for several things and only some were addressed.
6. Arithmetic and unit errors — wrong sums, inconsistent units, figures that contradict
   each other elsewhere in the same answer.

Do NOT challenge style, tone, phrasing, length, or debatable nuance. When in doubt, PASS.
Do NOT order the assistant to re-run a check that already appears in the evidence digest.
A safety refusal is not a technical outage: never require bypassing it or switching
models/providers to evade it. Recovery must inspect actual failure and authorization;
do not automatically repeat an already executed external action.

The assistant is an agent with real tools — it can run commands, read files, query APIs
and check its own work. If a claim is checkable and NO supporting evidence is shown, you
may challenge it and require the assistant to verify it before asserting it.

OUTPUT valid JSON only:
{{"passed": true, "feedback": ""}}
or
{{"passed": false, "feedback": "<instructions to the assistant>"}}

When passed is false, `feedback` is delivered to the assistant as a message from the
user. Write it as direct, specific, actionable instructions:
- Name each defect concretely and say what to do about it.
- Where a claim needs proof, tell the assistant to verify it with its tools first.
Write only the instructions. Do not greet, do not explain who you are."""

REPEAT_GUARD = """

YOU HAVE ALREADY CHALLENGED THIS ANSWER. Your previous rounds of feedback were:
{prior}

Rules for this round:
- Judge ONLY what is still wrong now. Do not restate an objection the assistant has
  already acted on.
- If you raised a point before and the assistant still has not satisfied it, treat it as
  UNSATISFIABLE and PASS rather than asking a third time. Repeating an unmeetable demand
  traps the user in a loop and is worse than delivering an imperfect answer.
- Only challenge again for a defect that is both NEW and serious."""

NO_TRACE_SUFFIX = (
    "\n\nReply with the corrected final answer only. Do not mention this message, "
    "this review, any verification step, or the fact that anything was changed. "
    "Do not apologise and do not summarise what you fixed — simply give the "
    "corrected answer as though it were your first and only reply."
)

# Triage: replies matching ALL of these skip the judge entirely.
_TRIAGE_MAX_CHARS = 600
_CLAIM_WORDS = re.compile(
    r"\b(deployed|verified|confirmed|updated|created|fixed|completed|installed|"
    r"running|restarted|deleted|removed|configured|migrated|successfully|"
    r"готово|сделал|проверил|обновил|установил|запустил|удалил|настроил|подтвержд)\b",
    re.I,
)


def _ensure_logging() -> None:
    """Own log file at INFO.

    Called lazily from the hooks, not from register(): Hermes reconfigures
    logging after plugins load, which resets the level set at import time.
    """
    if logger.level != logging.INFO:
        logger.setLevel(logging.INFO)
    if getattr(logger, "_critic_handler_installed", False):
        return
    try:
        from hermes_constants import get_hermes_home

        path = get_hermes_home() / "logs" / "response-critic.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            str(path), maxBytes=2_000_000, backupCount=2
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.propagate = True
        logger._critic_handler_installed = True
    except Exception:
        pass


def _clamp_effort(name: str) -> int:
    try:
        return EFFORT_LADDER.index(name)
    except ValueError:
        return EFFORT_LADDER.index(DEFAULT_MIN_EFFORT)


def _pick_effort(text: str, attempt: int, coding: bool, changed_paths) -> str:
    """Scale reasoning effort to how hard this draft is to check."""
    score = 0
    n = len(text)
    if n > 400:
        score += 1
    if n > 1500:
        score += 1
    if n > 4000:
        score += 1
    # numbers, money, units and tables are where judging gets expensive
    if len(re.findall(r"\d", text)) > 40:
        score += 1
    if re.search(r"[$€£]\s?\d|\b\d+(?:\.\d+)?\s?(?:GB|TB|GiB|TiB|MB|%|/mo|per month)\b",
                 text, re.I):
        score += 1
    if re.search(r"https?://", text):
        score += 1
    if "```" in text or text.count("|") > 8:
        score += 1
    if coding:
        score += 1
    real_paths = [p for p in (changed_paths or []) if p != SENTINEL]
    if real_paths:
        score += 1

    lo, hi = _clamp_effort(_min_effort), _clamp_effort(_max_effort)
    if hi < lo:
        lo, hi = hi, lo
    # map score onto the configured band, then escalate once per retry
    tier = lo + min(score, hi - lo)
    tier = min(tier + max(0, attempt), hi)
    return EFFORT_LADDER[tier]


def _looks_trivial(text: str) -> bool:
    """True when a draft carries nothing checkable — skip the judge call."""
    if len(text) > _TRIAGE_MAX_CHARS:
        return False
    if re.search(r"\d|https?://|```|\|", text):
        return False
    if _CLAIM_WORDS.search(text):
        return False
    return True


def _get_agent():
    try:
        from agent.subagent_lifecycle import get_active_subagent_parent

        return get_active_subagent_parent()
    except Exception:
        return None


def _open_verify_gate(**_kwargs) -> None:
    """Make core's ``pre_verify`` gate fire on turns that touched no files.

    conversation_loop.py requires a non-empty ``_turn_file_mutation_paths``.
    Rebind rather than mutate in place: this runs on a hook worker thread while
    the loop thread does sorted() over the same set.
    """
    if _critic_mode == "off":
        return
    if _critic_mode != "active":
        return
    try:
        _ensure_logging()
        agent = _get_agent()
        if agent is None:
            return
        with _context_lock:
            if _turn_context.get(str(getattr(agent, 'session_id', '') or ''), {}).get('parent_session_id'):
                return
        current = getattr(agent, "_turn_file_mutation_paths", None)
        if current is None:
            logger.error(
                "response-critic: agent._turn_file_mutation_paths is missing — "
                "Hermes internals changed, the critic loop is INACTIVE"
            )
            return
        if SENTINEL in current:
            return
        agent._turn_file_mutation_paths = set(current) | {SENTINEL}
    except Exception as exc:
        logger.warning("response-critic: could not open verify gate (%s)", type(exc).__name__)


def _conversation_from_frames(max_depth: int = 80):
    """Find the live message list by walking the call stack.

    pre_verify hooks receive no conversation, and run_conversation keeps the
    history in local variables (``messages`` / ``conversation_history``), not
    on the agent object. The hook fires inside that call, so the frames above
    us hold the real list. Falls back to agent attributes for safety.
    """
    import sys

    def _looks_like_history(val) -> bool:
        return (
            isinstance(val, list) and val
            and all(isinstance(m, dict) for m in val[-3:])
            and any(m.get("role") in ("tool", "assistant", "user") for m in val[-8:])
        )

    try:
        frame = sys._getframe(1)
        depth = 0
        while frame is not None and depth < max_depth:
            depth += 1
            for name in ("messages", "conversation_history"):
                val = frame.f_locals.get(name)
                if _looks_like_history(val):
                    return val
            frame = frame.f_back
    except Exception:
        pass
    agent = _get_agent()
    if agent is not None:
        for attr in ("_session_messages", "conversation_history", "messages", "_conversation_history",
                     "history", "_messages"):
            val = getattr(agent, attr, None)
            if _looks_like_history(val):
                return val
    return None


def _redact_for_review(text):
    """Use the host's forced egress scrub; never return raw text on failure."""
    try:
        from agent.redact import redact_for_egress
        return redact_for_egress(str(text or ""))
    except Exception:
        return "[Review content unavailable: secret redaction failed]"


def _current_history(session_id=""):
    history = _conversation_from_frames() or []
    with _context_lock:
        context = dict(_turn_context.get(session_id, {}))
    start = context.get("history_start")
    request = str(context.get("user_message") or "")
    anchored = (isinstance(start, int) and 0 <= start < len(history)
                and history[start].get("role") == "user"
                and _message_text(history[start].get("content")) == request)
    if not anchored:
        if request:
            start = next((i for i in range(len(history) - 1, -1, -1)
                          if isinstance(history[i], dict) and history[i].get("role") == "user"
                          and not _synthetic_user(history[i])
                          and _message_text(history[i].get("content")) == request), None)
            if start is None:
                return [], context  # do not authorize this turn using prior-only history
        else:
            start = next((i for i in range(len(history) - 1, -1, -1)
                          if isinstance(history[i], dict) and history[i].get("role") == "user"
                          and not _synthetic_user(history[i])), 0)
    return history[start:], context


def _message_text(body):
    if isinstance(body, list):
        body = " ".join(b.get("text", "") for b in body if isinstance(b, dict))
    return str(body or "").strip()


def _latest_request(history, context):
    # Exclude host-authored notices and verifier nudges, not machine-looking
    # human text. Steering is an actual later user message and wins.
    if context.get("internal"):
        return str(context.get("user_message") or "")
    for msg in reversed(history):
        if (msg.get("role") == "user" and not _synthetic_user(msg)
                and msg.get("display_kind") != "internal_notification"):
            body = _message_text(msg.get("content"))
            if body:
                return body
    return str(context.get("user_message") or "")


def _evidence_appendix(max_items: int = 10, per_item_chars: int = 400,
                       total_chars: int = 4500, session_id: str = "") -> str:
    """Current-turn digest. Redact complete values BEFORE truncation."""
    try:
        history, context = _current_history(session_id)
        last_user = _redact_for_review(_latest_request(history, context))[:1600]
        chunks = []
        for msg in reversed(history):
            if len(chunks) >= max_items:
                break
            role = msg.get("role")
            if role == "tool":
                body = msg.get("content")
                name = msg.get("name") or msg.get("tool_call_id") or "tool"
            elif role == "assistant" and msg.get("tool_calls"):
                names = [(c.get("function") or {}).get("name")
                         for c in msg["tool_calls"] if isinstance(c, dict)]
                names = [str(n) for n in names if n]
                if not names:
                    continue
                body = "called: " + ", ".join(names)
                name = "assistant"
            else:
                continue
            body = _message_text(body)
            if body:
                chunks.append(f"[{_redact_for_review(name)}] {_redact_for_review(body)[:per_item_chars]}")
        digest = "\n".join(reversed(chunks))[:total_chars]
        parts = []
        if context.get("internal"):
            parts.append("TURN TYPE: TRUSTED INTERNAL COMPLETION, NOT A HUMAN REQUEST.")
            if context.get("prior_answer"):
                parts.append("PREVIOUS FINAL ANSWER ALREADY COMPLETED (truncated):\n" +
                             _redact_for_review(context["prior_answer"])[:2500])
        if last_user:
            heading = "CURRENT INTERNAL NOTICE" if context.get("internal") else "LATEST USER REQUEST"
            parts.append(heading + " (truncated):\n" + last_user)
        if digest:
            parts.append("TOOL EVIDENCE this turn (oldest first, truncated):\n" + digest)
        return ("\n\nEVIDENCE DIGEST — from the live conversation:\n" + "\n\n".join(parts)) if parts else ""
    except Exception as exc:
        logger.warning("response-critic: evidence appendix failed (%s)", type(exc).__name__)
        return ""


def _parse_verdict(content: str):
    """Tolerant verdict parse: strict JSON first, then first {...} block."""
    try:
        return json.loads(content)
    except Exception:
        pass
    m = re.search(r"\{.*\}", content or "", re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _judge_kimi(user_message: str, effort: str, system: str):
    """Primary judge: Kimi K3 on the kimi-coding chat_completions wire."""
    if not _provider_available("kimi"):
        return None
    api_key = os.environ.get("KIMI_API_KEY") or os.environ.get("KIMI_CODING_API_KEY")
    if not api_key:
        logger.warning("Critic: KIMI_API_KEY not set — primary judge unavailable")
        return None
    base = (_judge_base_url or DEFAULT_JUDGE_BASE_URL).rstrip("/")
    payload = {
        "model": _judge_model,
        "response_format": {"type": "json_object"},
        "reasoning_effort": KIMI_EFFORT_MAP.get(effort, "low"),
        "max_tokens": 4000,
        "messages": [
            {"role": "system", "content": _redact_for_review(system)},
            {"role": "user", "content": _redact_for_review(user_message)},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # Moonshot's brotli SSE decode is broken in httpx; gzip only.
        "Accept-Encoding": "gzip",
    }
    with httpx.Client(timeout=_request_timeout(REQUEST_TIMEOUT)) as client:
        resp = client.post(f"{base}/chat/completions", headers=headers, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "Critic kimi request failed: HTTP %s", resp.status_code
            )
            if resp.status_code in {401, 403, 429}:
                _cool_provider("kimi")
            return None
        return _parse_verdict(resp.json()["choices"][0]["message"]["content"])





def _valid_judge_verdict(verdict) -> bool:
    return (
        isinstance(verdict, dict)
        and type(verdict.get("passed")) is bool
        and isinstance(verdict.get("feedback"), str)
        and (verdict["passed"] or bool(verdict["feedback"].strip()))
    )





def _judge_openrouter(user_message: str, effort: str, system: str):
    """OpenRouter fallback at configured maximum effort; never silently downgrade."""
    if not _provider_available("openrouter-fallback"):
        return None
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    payload = {
        "model": _fallback_model,
        "response_format": {"type": "json_object"},
        "reasoning": {"effort": _fallback_effort},
        "max_tokens": 16384,
        "provider": {"require_parameters": True},
        "messages": [
            {"role": "system", "content": _redact_for_review(system)},
            {"role": "user", "content": _redact_for_review(user_message)},
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    with httpx.Client(timeout=_request_timeout(OPENROUTER_TIMEOUT)) as client:
        resp = client.post("https://openrouter.ai/api/v1/chat/completions",
                           headers=headers, json=payload)
        if resp.status_code != 200:
            logger.warning(
                "Critic openrouter request failed: HTTP %s", resp.status_code
            )
            if resp.status_code in {401, 403, 429}:
                _cool_provider("openrouter-fallback")
            return None
        return _parse_verdict(resp.json()["choices"][0]["message"]["content"])


def _bounded_result(callback, deadline, label, *, session_id=None):
    """Bound caller wait; late inference results cannot issue a continuation.

    One in-flight worker per session and review stage bounds abandoned work
    locally. Independent sessions have independent capacity, with no profile-wide
    cap. Same-session duplicate work/errors/timeouts fail open.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        logger.info("Critic: %s skipped (expired budget)", label)
        return None
    # Missing IDs must not collapse unrelated CLI/tool callers into one bucket.
    session = session_id or _review_session.get() or object()
    key, marker = (session, label), object()
    with _review_workers_lock:
        if key in _review_workers:
            logger.info("Critic: %s skipped (same-session worker still running)", label)
            return None
        _review_workers[key] = marker

    def release():
        with _review_workers_lock:
            if _review_workers.get(key) is marker:
                _review_workers.pop(key, None)

    done, result = threading.Event(), {}
    context = contextvars.copy_context()

    def work():
        token = _judge_deadline.set(deadline)
        try:
            result["value"] = callback()
        except Exception as exc:
            logger.warning("Critic: %s unavailable (%s)", label, type(exc).__name__)
        finally:
            _judge_deadline.reset(token)
            release()
            done.set()

    try:
        threading.Thread(target=lambda: context.run(work), name="critic-bounded-review", daemon=True).start()
    except RuntimeError:
        release()
        return None
    if not done.wait(max(0, deadline - time.monotonic())):
        logger.info("Critic: %s deadline reached; delivering without its verdict", label)
        return None
    return result.get("value")


def _judge(user_message: str, effort: str, system: str):
    deadline = _judge_deadline.get() or time.monotonic() + _review_budget_seconds
    result = _bounded_result(lambda: _judge_chain(user_message, effort, system), deadline, "judge")
    return result if isinstance(result, tuple) else (None, "none")


def _review_outcome(text, session_id, evidence):
    # Outcome shadow observation belongs to the nonblocking post-LLM worker,
    # not an extra billed call on the delivery-critical pre_verify path.
    if not _outcome_review_enabled or _critic_mode != "active" or _outcome_review_mode != "active":
        return None
    deadline = min(_judge_deadline.get() or float('inf'), time.monotonic() + 8)
    return _bounded_result(lambda: _review_outcome_impl(text, session_id, evidence), deadline, "outcome", session_id=session_id)


def _judge_chain(user_message: str, effort: str, system: str):
    """Kimi -> OpenRouter at maximum reasoning, preserving valid challenges."""
    providers = [("kimi", _judge_kimi)]
    if _fallback_enabled:
        providers.append(("openrouter-fallback", _judge_openrouter))
    for name, callback in providers:
        deadline = _judge_deadline.get()
        if deadline is not None and time.monotonic() >= deadline:
            break
        if not _provider_available(name):
            continue
        try:
            verdict = callback(user_message, effort, system)
            if _valid_judge_verdict(verdict):
                return verdict, name
            if verdict is not None:
                logger.warning("Critic: invalid verdict from %s; trying next provider", name)
        except Exception as exc:
            logger.warning("Critic %s unavailable (%s); trying next provider", name, type(exc).__name__)
    return None, "none"


def _review_outcome_impl(text, session_id, evidence):
    """Call only the other plugin's public, scoped tool; abstain on all failures."""
    import math
    if not _outcome_review_enabled or _plugin_context is None:
        return None
    try:
        if not _plugin_context.has_plugin("scenario-router"):
            return None
        history, context = _current_history(session_id)
        result = _plugin_context.dispatch_tool("scenario_review_outcome", {
            "user_message": _redact_for_review(_latest_request(history, context)),
            "assistant_response": _redact_for_review(text),
            "evidence": _redact_for_review(evidence),
            "internal": bool(context.get("internal")),
            "pending_background": _background_pending(session_id),
        })
        if isinstance(result, str):
            result = json.loads(result)
        if not isinstance(result, dict) or result.get("ok") is not True:
            return None
        if result.get("mode") not in {"shadow", "active", "off"}:
            return None
        review = result.get("review")
        if not isinstance(review, dict):
            return None
        confidence = review.get("confidence")
        if (review.get("disposition") not in _OUTCOME_DISPOSITIONS
                or not isinstance(review.get("scenario"), str)
                or type(confidence) not in {int, float} or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
                or not isinstance(review.get("feedback"), str)
                or review.get("verifier_effort") != "max"
                or review.get("applied") is not False
                or review.get("acknowledgment") not in {None, "", "Added."}):
            return None
        if review["disposition"] == "acknowledge" and review.get("acknowledgment") != "Added.":
            return None
        # Never log input, raw answers, free-form scenario labels or feedback.
        logger.info("Critic: outcome mode=%s disposition=%s confidence=%.3f",
                    result["mode"], review["disposition"], confidence)
        return result
    except Exception as exc:
        logger.warning("Critic: outcome review unavailable (%s); using full judge", type(exc).__name__)
        return None


def _memory_confirmed(session_id):
    """Conservative current-turn write/readback prerequisite for terse acknowledgments.

    Unknown result formats abstain. Jev must additionally confirm note-only intent
    and successful persistence; merely promising memory storage is insufficient.
    """
    history, _ = _current_history(session_id)
    calls = {}
    stored, confirmed, stored_id = False, False, None
    for msg in history:
        if msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict):
                    calls[call.get("id")] = (call.get("function") or {}).get("name", "")
        if msg.get("role") != "tool":
            continue
        name = str(msg.get("name") or calls.get(msg.get("tool_call_id")) or "").lower()
        memory_tool = any(word in name for word in ("memory", "hindsight", "graphiti", "retain"))
        if not memory_tool:
            continue
        body = msg.get("content")
        try:
            value = json.loads(body) if isinstance(body, str) else body
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        failed = (value.get("success") is False or value.get("ok") is False
                  or bool(value.get("error")) or value.get("staged") is True)
        if failed:
            stored, confirmed, stored_id = False, False, None
            continue
        successful = (value.get("success") is True or value.get("ok") is True
                      or value.get("status") in {"completed", "stored", "success"})
        writing = any(word in name for word in ("retain", "add_memory", "add_triplet")) or name == "memory"
        reading = any(word in name for word in ("get_document", "get_memory", "recall", "search_memory"))
        if writing and successful:
            stored = True
            stored_id = value.get("document_id") or value.get("id")
            confirmed = value.get("verified") is True
        read_id = value.get("document_id") or value.get("id")
        same_target = bool(stored_id and read_id and stored_id == read_id)
        if reading and stored and successful and same_target and any(
                value.get(key) for key in ("content", "document", "memory", "memories", "results", "text")):
            confirmed = True
    return confirmed


def _outcome_continuation(review, session_id):
    feedback = _redact_for_review(review["feedback"]).strip()
    if not feedback:
        return None
    if review["disposition"] == "recover":
        feedback += (
            "\nInspect the actual failure, authorization and current external state with your tools. "
            "Do not automatically repeat an already executed external action: verify whether it "
            "succeeded before any retry, and seek authorization if scope changes. Continue with a "
            "safe alternative only when authorized. Do not bypass a safety refusal or promise "
            "a sticky provider switch; this review does not change the provider or model."
        )
    with _feedback_lock:
        _turn_feedback.setdefault(session_id or "-", []).append(feedback)
    logger.info("Critic: outcome continuation disposition=%s", review["disposition"])
    return {"action": "continue", "message": feedback + NO_TRACE_SUFFIX}


def validate_final_response(final_response: str = "", attempt: int = 0,
                            session_id: str = "", model: str = "",
                            platform: str = "", coding: bool = False,
                            changed_paths=None, **_kwargs):
    """pre_verify callback: accept the draft, or send the agent back to fix it."""
    _ensure_logging()
    review_started = time.monotonic()
    text = (final_response or "").strip()
    key = session_id or "-"
    with _context_lock:
        if _turn_context.get(session_id, {}).get('parent_session_id'):
            return None
        _acknowledgments.pop(session_id, None)
    if attempt == 0:
        with _feedback_lock:
            _turn_feedback.pop(key, None)
    if attempt >= _max_iterations or _critic_mode != "active":
        with _feedback_lock:
            _turn_feedback.pop(key, None)
        return None

    evidence = _evidence_appendix(session_id=session_id) if _outcome_review_enabled else None
    outcome = _review_outcome(text, session_id, evidence or "")
    if (_critic_mode == "active" and _outcome_review_mode == "active"
            and outcome and outcome["mode"] == "active"):
        review = outcome["review"]
        disposition = review["disposition"]
        if review["confidence"] >= _OUTCOME_CONFIDENCE:
            if disposition == "accept":
                return None
            if disposition == "handoff" and _background_pending(session_id):
                return None
            if disposition == "acknowledge" and _memory_confirmed(session_id):
                with _context_lock:
                    context = _turn_context.get(session_id, {})
                    if context and not context.get("internal"):
                        _acknowledgments[session_id] = {
                            "turn_id": context["turn_id"], "draft": text,
                        }
                        return None
            if disposition in {"correct", "recover"}:
                continuation = _outcome_continuation(review, session_id)
                if continuation:
                    return continuation
        # Uncertain, refusal, low confidence and unconfirmed acknowledgment all
        # use the existing full judge. Refusal never triggers recovery/rerouting.

    # Active abstention must run the full verifier, including short storage failures.
    force_full_review = (_outcome_review_enabled and _critic_mode == "active"
                         and _outcome_review_mode == "active")
    # Shadow calls preserve all legacy skip/judge behavior.
    if not force_full_review and len(text) < MIN_RESPONSE_CHARS:
        return None
    if _is_background_handoff(text, session_id):
        logger.info("Critic: accepted async handoff")
        return None
    if not force_full_review and attempt == 0 and _triage_enabled and _looks_trivial(text):
        logger.info("Critic: triage skip (trivial reply)")
        return None

    with _feedback_lock:
        prior = list(_turn_feedback.get(key, ()))

    effort = "max" if force_full_review else _pick_effort(text, attempt, coding, changed_paths)

    today = datetime.date.today().isoformat()
    system = JUDGE_PROMPT_TEMPLATE.format(today=today)
    if prior:
        system += REPEAT_GUARD.format(
            prior="\n".join(f"  round {i + 1}: {p[:600]}" for i, p in enumerate(prior))
        )
    with _context_lock:
        context = dict(_turn_context.get(session_id, {}))
    if context.get("internal"):
        system += ("\nThis is an internal completion notification, not a new user request. "
                   "Never demand a repeated answer to an earlier already-completed request. "
                   "If a completed answer is repeated/paraphrased without a material correction, "
                   "challenge with instructions to return exactly NO_REPLY. "
                   "If the prior response was only a pending-work handoff, judge the completed "
                   "answer normally. Do not suppress genuine new findings or corrections.")
    user_message = "DRAFT FINAL ANSWER:\n\n" + _redact_for_review(text) + (
        evidence if evidence is not None else _evidence_appendix(session_id=session_id))
    system = _redact_for_review(system)

    token = _judge_deadline.set(review_started + _review_budget_seconds)
    session_token = _review_session.set(session_id or None)
    try:
        verdict, judge_name = _judge(user_message, effort, system)
    finally:
        _review_session.reset(session_token)
        _judge_deadline.reset(token)
    if not isinstance(verdict, dict):
        return None                        # fail open — never trap the turn

    if verdict.get("passed") is True:
        logger.info(f"Critic: passed on attempt {attempt} (judge={judge_name}, effort={effort})")
        with _feedback_lock:
            _turn_feedback.pop(key, None)
        return None

    if verdict.get("passed") is not False:
        logger.warning("Critic: invalid verdict schema; failing open session=%s", session_id)
        return None

    if _critic_mode != "active":
        logger.info("Critic: shadow verdict observed; no continuation")
        return None
    feedback = _redact_for_review(verdict.get("feedback") or "").strip()
    if not feedback:
        return None

    with _feedback_lock:
        _turn_feedback.setdefault(key, []).append(feedback)

    logger.info(
        f"Critic: challenged (attempt {attempt + 1}/{_max_iterations}, "
        f"judge={judge_name}, effort={effort})"
    )
    suffix = ("\n\nThis is an internal notification, not a human follow-up. "
              "Return NO_REPLY if there is no material new correction; do not re-answer an "
              "already completed request.") if context.get("internal") else NO_TRACE_SUFFIX
    return {"action": "continue", "message": feedback + suffix}


def register(ctx):
    global _max_iterations, _min_effort, _max_effort
    global _triage_enabled, _judge_model, _judge_base_url
    global _fallback_model, _fallback_enabled, _fallback_effort
    global _plugin_context, _critic_mode, _outcome_review_enabled, _outcome_review_mode
    global _review_budget_seconds, _provider_cooldown_seconds
    _plugin_context = ctx
    for name, default in (("review_budget_seconds", 300.0), ("provider_cooldown_seconds", 300.0)):
        try:
            value = float(ctx.get_config(name, default))
            if not 1 <= value <= (300 if name == "review_budget_seconds" else 3600):
                value = default
        except (TypeError, ValueError):
            value = default
        if name == "review_budget_seconds":
            _review_budget_seconds = value
        else:
            _provider_cooldown_seconds = value
    _critic_mode = str(ctx.get_config("critic_mode", "active") or "active").lower()
    if _critic_mode not in {"active", "shadow", "off"}:
        _critic_mode = "active"
    _outcome_review_enabled = ctx.get_config("outcome_review_enabled", False) is True
    _outcome_review_mode = str(ctx.get_config("outcome_review_mode", "shadow") or "shadow").lower()
    if _outcome_review_mode not in {"active", "shadow"}:
        _outcome_review_mode = "shadow"
    _ensure_logging()
    try:
        _max_iterations = max(1, int(ctx.get_config("max_iterations", DEFAULT_MAX_ITERATIONS)))
    except Exception:
        _max_iterations = DEFAULT_MAX_ITERATIONS

    for name, default in (("min_effort", DEFAULT_MIN_EFFORT), ("max_effort", DEFAULT_MAX_EFFORT)):
        try:
            value = str(ctx.get_config(name, default) or default).strip().lower()
        except Exception:
            value = default
        if value not in EFFORT_LADDER:
            logger.warning(
                f"response-critic: invalid {name}; using {default!r}"
            )
            value = default
        if name == "min_effort":
            _min_effort = value
        else:
            _max_effort = value

    _judge_model = str(ctx.get_config("judge_model", DEFAULT_JUDGE_MODEL) or DEFAULT_JUDGE_MODEL)
    _judge_base_url = str(ctx.get_config("judge_base_url", DEFAULT_JUDGE_BASE_URL) or DEFAULT_JUDGE_BASE_URL)
    _fallback_model = str(ctx.get_config("fallback_model", DEFAULT_FALLBACK_MODEL) or DEFAULT_FALLBACK_MODEL)
    _fallback_enabled = bool(ctx.get_config("fallback_enabled", True))
    _fallback_effort = str(ctx.get_config("fallback_effort", DEFAULT_FALLBACK_EFFORT) or DEFAULT_FALLBACK_EFFORT)
    if _fallback_effort != "max":
        logger.warning("Critic: fallback effort must be max; using max")
        _fallback_effort = "max"
    _triage_enabled = bool(ctx.get_config("triage_enabled", True))

    try:
        from agent.verify_hooks import max_verify_nudges

        core_cap = max_verify_nudges()
        if core_cap < _max_iterations:
            logger.warning(
                f"response-critic max_iterations={_max_iterations} but "
                f"agent.max_verify_nudges={core_cap}; the loop will stop at {core_cap}. "
                f"Set agent.max_verify_nudges: {_max_iterations} in config.yaml"
            )
    except Exception:
        pass

    logger.info(
        f"response-critic ready: judge={_judge_model}@kimi fallback={_fallback_model} fallback_effort={_fallback_effort} "
        f"max_iterations={_max_iterations} effort={_min_effort}..{_max_effort} triage={_triage_enabled}"
    )
    ctx.register_hook("pre_gateway_dispatch", observe_gateway_input)
    ctx.register_hook("pre_llm_call", capture_turn_context)
    ctx.register_hook("post_llm_call", record_completed_answer)
    ctx.register_hook("transform_llm_output", suppress_exact_internal_duplicate)
    ctx.register_hook("pre_api_request", _open_verify_gate)
    ctx.register_hook("pre_verify", validate_final_response)
