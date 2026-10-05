"""Mechanical native-response bridge for Proofgate's internal continuation tool.

Policy and user-facing directives belong in the scenario registry, not here. Copies
share untouched metadata (notably usage and signed/encrypted reasoning); rejected
visible text is never retained in a replay carrier. This is a final-only bridge:
already streamed text cannot be retracted by execution middleware.
"""

from copy import copy
import json
from uuid import uuid4


TOOL_NAME = "proofgate_continue"
_MODES = {"chat_completions", "anthropic_messages", "codex_responses", "responses"}


def tool_schema():
    """Return the registry's OpenAI function schema, with no semantic policy."""
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "Internal policy-generated continuation transport.",
            "parameters": {
                "type": "object",
                "properties": {"token": {"type": "string"}},
                "required": ["token"],
                "additionalProperties": False,
            },
        },
    }


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _replace(value, **fields):
    if isinstance(value, dict):
        return {**value, **fields}
    model_copy = getattr(value, "model_copy", None)
    if callable(model_copy):
        return model_copy(update=fields)
    result = copy(value)
    for key, item in fields.items():
        setattr(result, key, item)
    return result


class _Block(dict):
    """New wire blocks usable by both dict readers and SDK-style normalizers."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _text_blocks(content, allowed):
    if not isinstance(content, list):
        return None
    parts = []
    for block in content:
        if _get(block, "type") not in allowed or not isinstance(_get(block, "text"), str):
            return None
        parts.append(_get(block, "text"))
    return "\n".join(parts)


def inspect_response(response, api_mode):
    """Return visible candidate text only for a supported completed final."""
    if api_mode not in _MODES or response is None:
        return None
    if api_mode == "chat_completions":
        choices = _get(response, "choices")
        if not isinstance(choices, list) or len(choices) != 1:
            return None
        choice = choices[0]
        message = _get(choice, "message")
        if (_get(choice, "finish_reason") != "stop" or message is None
                or _get(message, "tool_calls") or _get(message, "function_call")
                or _get(message, "refusal")):
            return None
        content = _get(message, "content")
        text = content if isinstance(content, str) else _text_blocks(content, {"text"})
    elif api_mode == "anthropic_messages":
        if _get(response, "stop_reason") not in {"end_turn", "stop_sequence"}:
            return None
        # Native Bedrock guardrail responses must remain refusals.
        extra = _get(response, "model_extra", {}) or {}
        if str(_get(response, "amazon-bedrock-guardrailAction",
                    _get(extra, "amazon-bedrock-guardrailAction", ""))).upper() == "INTERVENED":
            return None
        content = _get(response, "content")
        if not isinstance(content, list):
            return None
        parts = []
        seen_text = False
        for block in content:
            kind = _get(block, "type")
            if kind == "text" and isinstance(_get(block, "text"), str):
                parts.append(_get(block, "text"))
                seen_text = True
            elif kind in {"thinking", "redacted_thinking"}:
                # A signature can bind preceding text. Rewriting that prefix is
                # unsupported; never pretend such a continuation is replay-safe.
                if seen_text and (_get(block, "signature") or _get(block, "data")):
                    return None
            else:
                return None
        text = "\n".join(parts)
    else:
        if (_get(response, "status") != "completed" or _get(response, "incomplete_details")
                or _get(response, "error")):
            return None
        output = _get(response, "output")
        if not isinstance(output, list):
            return None
        parts = []
        for item in output:
            if _get(item, "status") not in {None, "completed"}:
                return None
            kind = _get(item, "type")
            if kind == "reasoning":
                continue
            if (kind != "message" or _get(item, "role", "assistant") != "assistant"
                    or _get(item, "phase") not in {None, "final_answer"}):
                return None
            item_text = _text_blocks(_get(item, "content"), {"output_text"})
            if item_text is None:
                return None
            parts.append(item_text)
        text = "\n".join(parts)
        if not text:
            text = _get(response, "output_text")
    return text if isinstance(text, str) and text.strip() else None


def response_kind(response, api_mode):
    """Classify native control state without assigning any semantic task verdict."""
    if api_mode not in _MODES or response is None:
        return "unsupported"
    if inspect_response(response, api_mode) is not None:
        return "final"
    if api_mode == "chat_completions":
        choices = _get(response, "choices")
        if not isinstance(choices, list) or len(choices) != 1:
            return "unsupported"
        choice = choices[0]
        message = _get(choice, "message")
        reason = _get(choice, "finish_reason")
        if reason == "content_filter" or _get(message, "refusal"):
            return "safe_end"
        if reason == "length":
            return "incomplete"
        if _get(message, "tool_calls") or _get(message, "function_call"):
            return "tools"
    elif api_mode == "anthropic_messages":
        reason = _get(response, "stop_reason")
        extra = _get(response, "model_extra", {}) or {}
        if (reason in {"refusal", "guardrail_intervened"}
                or str(_get(response, "amazon-bedrock-guardrailAction",
                            _get(extra, "amazon-bedrock-guardrailAction", ""))).upper() == "INTERVENED"):
            return "safe_end"
        if reason in {"max_tokens", "pause_turn"}:
            return "incomplete"
        if any(_get(b, "type") in {"tool_use", "server_tool_use"}
               for b in (_get(response, "content") or [])):
            return "tools"
    else:
        status = _get(response, "status")
        detail = _get(response, "incomplete_details")
        if status == "cancelled" or _get(detail, "reason") == "content_filter":
            return "safe_end"
        output = _get(response, "output") or []
        if any(_get(b, "type") == "refusal" for item in output
               if _get(item, "type") == "message" for b in (_get(item, "content") or [])):
            return "safe_end"
        if status in {"incomplete", "in_progress", "queued"}:
            return "incomplete"
        if any(_get(item, "type") in {
            "function_call", "custom_tool_call", "web_search_call", "file_search_call",
            "computer_call", "code_interpreter_call", "mcp_call", "mcp_approval_request",
        } for item in output):
            return "tools"
    return "unsupported"


def _rewrite(response, api_mode, text, token, tool_name=TOOL_NAME):
    if inspect_response(response, api_mode) is None:
        raise ValueError("Proofgate cannot rewrite an unsupported or non-final response")
    continuing = token is not None
    call_id = "call_pg_" + uuid4().hex if continuing else None
    inputs = {"token": token}
    if tool_name == "tool_call":
        inputs = {"calls": [{"name": TOOL_NAME, "arguments": inputs}]}
    arguments = json.dumps(inputs, separators=(",", ":")) if continuing else None
    if api_mode == "chat_completions":
        choice = _get(response, "choices")[0]
        call = _Block(id=call_id, type="function", function=_Block(name=tool_name, arguments=arguments))
        message = _replace(_get(choice, "message"), content=None if continuing else text,
                           tool_calls=[call] if continuing else None)
        return _replace(response, choices=[_replace(choice, message=message,
                        finish_reason="tool_calls" if continuing else "stop")])
    if api_mode == "anthropic_messages":
        blocks = [block for block in _get(response, "content")
                  if _get(block, "type") in {"thinking", "redacted_thinking"}]
        blocks.append(_Block(type="tool_use", id=call_id, name=tool_name, input=inputs)
                      if continuing else _Block(type="text", text=text))
        return _replace(response, content=blocks,
                        stop_reason="tool_use" if continuing else "end_turn", stop_sequence=None)
    # Message items are also a replay carrier, not just the output_text display.
    # Drop them rather than appending a call beside the rejected final answer.
    output = [item for item in _get(response, "output") if _get(item, "type") == "reasoning"]
    if continuing:
        output.append(_Block(type="function_call", id="fc_pg_" + uuid4().hex,
                             call_id=call_id, name=tool_name, arguments=arguments, status="completed"))
    else:
        output.append(_Block(type="message", role="assistant", status="completed",
                             content=[_Block(type="output_text", text=text, annotations=[])]))
    fields = {"output": output}
    # SDK Responses expose output_text as a computed property. Only replace a
    # stored fallback (dicts, SimpleNamespace, streaming assemblers).
    if isinstance(response, dict) or "output_text" in getattr(response, "__dict__", {}):
        fields["output_text"] = "" if continuing else text
    return _replace(response, **fields)


def continue_response(response, api_mode, token, request=None):
    """Suppress a rejected final using the validated direct or deferred route.

    Omitting request retains the mechanical direct rewrite for response fixtures;
    enforcement always supplies the actual provider request.
    """
    if not isinstance(token, str):
        raise TypeError("Proofgate continuation token must be a string")
    route = TOOL_NAME if request is None else _tool_route(request, api_mode)
    if route is None:
        raise ValueError("Proofgate continuation tool is unavailable in this session")
    return _rewrite(response, api_mode, None, token, route)


def finish_response(response, api_mode, text):
    """Replace a supported final with registry-generated policy text, without tools."""
    if not isinstance(text, str):
        raise TypeError("Proofgate final text must be a string")
    return _rewrite(response, api_mode, text, None)


def _visible_parameters(request, api_mode, name):
    tools = _get(request, "tools")
    if api_mode not in _MODES or not isinstance(tools, list):
        return None
    for tool in tools:
        if _get(tool, "defer_loading", False):
            continue
        if api_mode == "chat_completions":
            if _get(tool, "type") != "function":
                continue
            function = _get(tool, "function")
            parameters = _get(function, "parameters")
        elif api_mode == "anthropic_messages":
            if _get(tool, "type") not in {None, "custom"}:
                continue
            function = tool
            parameters = _get(tool, "input_schema")
        else:
            if _get(tool, "type") != "function":
                continue
            function = tool
            parameters = _get(tool, "parameters")
        if _get(function, "name") == name and not _get(function, "defer_loading", False):
            return parameters
    return None


def _token_schema(parameters):
    properties = _get(parameters, "properties")
    names = (set(properties) if isinstance(properties, dict)
             else set(vars(properties)) if hasattr(properties, "__dict__") else set())
    return (_get(parameters, "type") == "object" and names == {"token"}
            and _get(_get(properties, "token"), "type") == "string"
            and _get(parameters, "required") == ["token"]
            and _get(parameters, "additionalProperties") is False)


def _dispatcher_schema(parameters):
    calls = _get(_get(parameters, "properties"), "calls")
    item = _get(calls, "items")
    properties = _get(item, "properties")
    return (_get(parameters, "type") == "object"
            and _get(parameters, "required") == ["calls"]
            and _get(calls, "type") == "array"
            and _get(item, "type") == "object"
            and _get(item, "required") == ["name", "arguments"]
            and _get(_get(properties, "name"), "type") == "string"
            and _get(_get(properties, "arguments"), "type") == "object")


def _tool_route(request, api_mode):
    if _token_schema(_visible_parameters(request, api_mode, TOOL_NAME)):
        return TOOL_NAME
    if not _dispatcher_schema(_visible_parameters(request, api_mode, "tool_call")):
        return None
    try:
        # The host binds this context per turn (including delegated children).
        # Never infer a grant from global registration, descriptions, or a parent
        # profile: use the same scoped raw catalog as native tool_call dispatch.
        from agent.subagent_lifecycle import get_active_subagent_parent
        from model_tools import get_tool_definitions
        from tools.tool_search import scoped_deferrable_names
        from tools.registry import registry

        agent = get_active_subagent_parent()
        if (agent is None or not hasattr(agent, "enabled_toolsets")
                or not hasattr(agent, "disabled_toolsets")
                or "tool_call" not in getattr(agent, "valid_tool_names", ())
                or not _dispatcher_schema(_visible_parameters(
                    {"tools": getattr(agent, "tools", None)}, "chat_completions", "tool_call"))):
            return None
        catalog = get_tool_definitions(
            enabled_toolsets=agent.enabled_toolsets, disabled_toolsets=agent.disabled_toolsets,
            quiet_mode=True, skip_tool_search_assembly=True)
        if (TOOL_NAME not in scoped_deferrable_names(catalog)
                or not _token_schema(_visible_parameters(
                    {"tools": catalog}, "chat_completions", TOOL_NAME))):
            return None
        entry = registry.get_entry(TOOL_NAME)
        if entry is not None and callable(entry.handler):
            return "tool_call"
    except Exception:
        # Missing native readers, unavailable catalogs and absent turn bindings
        # are unsupported, not permission to use the unrestricted registry.
        return None
    return None


def supports_tool(request, api_mode):
    """Whether this request has a direct or session-scoped native deferred route."""
    return _tool_route(request, api_mode) is not None
