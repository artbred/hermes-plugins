"""Advisory scenario context for the current human turn, never an output gate."""
from __future__ import annotations

import logging
import re

from . import classifier

logger = logging.getLogger("scenarios")

# Native context_references and memory_manager append these after the human ask.
# Stop at the opening boundary even when an appendage is unterminated; removing
# only balanced tags would disclose partial memory or subsequent plugin context.
_CONTEXT_BOUNDARY = re.compile(
    r"<\s*(?:memory-context|system-reminder|task-notification|ide_opened_file|ide_selection)\s*>"
    r"|\n+--- (?:Context Warnings|Attached Context) ---\n"
    r"|\[System note:\s*The following is recalled memory context,",
    re.IGNORECASE,
)
# These are native machine envelopes, not coding-keyword routing heuristics.
# An explicit display_kind on the current row takes precedence over this fallback.
_INTERNAL_PREFIXES = (
    "[System:", "[System note:", "[Runtime note:", "[SYSTEM]", "[CONTEXT",
    "[PRIOR CONTEXT", "[IMPORTANT: Background", "[Your active task list",
    "[Planning state preserved", "[ASYNC DELEGATION", "[OUT-OF-BAND",
    "Cronjob Response:", "<task-notification", "<system-reminder",
    "<command-message", "<command-name", "<local-command-",
    '[IMPORTANT: The "',
)


def _bounded_text(content):
    """Flatten only input text; reject the full text, never classify a prefix."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        chars = 0
        for part in content:
            if isinstance(part, str):
                value = part
            elif (isinstance(part, dict) and part.get("type") in ("text", "input_text")
                  and part.get("role") in (None, "user")):
                value = part.get("text")
            else:
                continue
            if not isinstance(value, str) or not value:
                continue
            chars += len(value) + bool(parts)
            if chars > classifier.MAX_TEXT_CHARS:
                return None
            parts.append(value)
        text = "\n".join(parts)
    else:
        return None
    if len(text) > classifier.MAX_TEXT_CHARS:
        return None
    try:
        if len(text.encode("utf-8")) > classifier.MAX_TEXT_BYTES:
            return None
    except UnicodeError:
        return None
    return text


def _without_context(text):
    boundary = _CONTEXT_BOUNDARY.search(text)
    return (text[:boundary.start()] if boundary else text).strip()


def _human_summary_view(row):
    # Use the native provenance/boundary parser, not a guessed summary delimiter.
    # It also discards stale api_content; never use that sidecar as classifier input.
    try:
        from agent.context_compressor import user_originated_turn_view
    except ImportError:
        return None
    return user_originated_turn_view(row)


def _current_row(content, history, native_turn):
    if not isinstance(history, list):
        return None
    for row in reversed(history):
        if not isinstance(row, dict) or row.get("role") != "user":
            continue
        if row.get("content") == content:
            return row
        # Native passes the current staged row, but original_user_message may be
        # its clean persistence variant (e.g. without the gateway voice prefix).
        # Typed synthetic events must not escape filtering through that mismatch.
        if native_turn and row.get("display_kind") and not row.get("_compressed_summary"):
            return row
        if row.get("_compressed_summary"):
            view = _human_summary_view(row)
            if view is not None and view.get("content") == content:
                return row
            if view is None:
                continue
        # Do not let a repeated historical prompt lend its metadata to this turn.
        return None
    return None


def _current_human_text(kwargs):
    if kwargs.get("parent_session_id"):
        # Delegated children receive model-authored task prompts, not human turns.
        return None
    content = kwargs.get("user_message")
    text = _bounded_text(content)
    if text is None:
        return None
    row = _current_row(content, kwargs.get("conversation_history"), bool(kwargs.get("turn_id")))
    explicit_kind = row is not None and "display_kind" in row
    if row is not None and row.get("_compressed_summary"):
        view = _human_summary_view(row)
        if view is None:
            return None
        # A clean original request wins; only unwrap a summary when the hook was
        # actually passed its carrier. History supplies metadata, never new text.
        if row.get("content") == content:
            text = _bounded_text(view.get("content"))
            if text is None:
                return None
    elif explicit_kind and row.get("display_kind") not in (None, "", "steer"):
        return None
    if text.lstrip().startswith('[IMPORTANT: The "'):
        # Channel auto-load scaffolds contain skill bodies before the human ask;
        # without an invocation boundary, abstain rather than export that body.
        return None
    if text.startswith("[IMPORTANT: The user has invoked the "):
        try:
            from agent.skill_commands import extract_user_instruction_from_skill_message
        except ImportError:
            return None
        text = extract_user_instruction_from_skill_message(text)
        if not text:
            return None
    if not explicit_kind and text.lstrip().startswith(_INTERNAL_PREFIXES):
        return None
    return _without_context(text) or None


def pre_llm_call(**kwargs):
    """Return trusted guidance on a match; all abstentions leave the turn alone."""
    try:
        text = _current_human_text(kwargs)
        if not text:
            return None
        blocks = classifier.classify(text)
        return {"context": "\n\n".join(blocks)} if blocks else None
    except Exception:
        # Exception messages can contain request bodies, credentials or identifiers.
        logger.warning("scenarios skipped: hook_error")
        return None


def register(ctx):
    ctx.register_hook("pre_llm_call", pre_llm_call)
