"""Synthetic offline fixtures: no API, telephone, or production-evidence calls.

Run: python3 plugins/proofgate/tests/test_native_bridge.py
Native checks run in a subprocess with isolated HERMES_HOME. HERMES_SOURCE can
select the installed Hermes checkout (default /usr/local/lib/hermes-agent).
"""

from copy import deepcopy
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
bridge = importlib.import_module("bridge")

MODES = ("chat_completions", "anthropic_messages", "codex_responses")
REJECTED = "SYNTHETIC REJECTED FINAL: hours alone prove everything complete."
FINISHED = "SYNTHETIC POLICY FINAL"


def fixture(mode, text=REJECTED):
    common = {"id": "synthetic_response", "model": "synthetic-model", "usage": {
        "prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
        "input_tokens": 11, "output_tokens": 7, "cache_read_input_tokens": 3,
    }}
    if mode == "chat_completions":
        return {**common, "object": "chat.completion", "created": 1, "choices": [{
            "index": 0, "finish_reason": "stop", "logprobs": None,
            "message": {"role": "assistant", "content": text, "tool_calls": None,
                        "refusal": None, "reasoning_content": "synthetic reasoning",
                        "reasoning_details": [{"type": "reasoning.encrypted", "data": "opaque"}]},
        }]}
    if mode == "anthropic_messages":
        return {**common, "type": "message", "role": "assistant", "stop_reason": "end_turn",
                "stop_sequence": None, "content": [
                    {"type": "thinking", "thinking": "synthetic reasoning", "signature": "signature-one"},
                    {"type": "redacted_thinking", "data": "opaque-two"},
                    {"type": "text", "text": text},
                ]}
    return {**common, "object": "response", "created_at": 1, "status": "completed",
            "incomplete_details": None, "output_text": text, "output": [
                {"type": "reasoning", "id": "rs_synthetic", "encrypted_content": "opaque-three",
                 "summary": [{"type": "summary_text", "text": "synthetic reasoning"}]},
                {"type": "message", "id": "msg_synthetic", "role": "assistant", "status": "completed",
                 "phase": "final_answer", "content": [{"type": "output_text", "text": text, "annotations": []}]},
            ]}


def namespace(value):
    if isinstance(value, dict):
        return SimpleNamespace(**{key: namespace(item) for key, item in value.items()})
    if isinstance(value, list):
        return [namespace(item) for item in value]
    return value


def plain(value):
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [plain(item) for item in value]
    if hasattr(value, "__dict__"):
        return plain(vars(value))
    return value


def wire_tool(mode):
    schema = bridge.tool_schema()
    if mode == "chat_completions":
        return schema
    function = schema["function"]
    if mode == "anthropic_messages":
        return {"name": function["name"], "description": function["description"],
                "input_schema": function["parameters"]}
    return {"type": "function", **function}


class BridgeTests(unittest.TestCase):
    def test_candidate_and_copy_across_dict_and_namespace(self):
        for mode in MODES + ("responses",):
            for as_namespace in (False, True):
                with self.subTest(mode=mode, sdk_style=as_namespace):
                    raw = fixture(mode)
                    original = deepcopy(raw)
                    response = namespace(raw) if as_namespace else raw
                    usage = bridge._get(response, "usage")
                    self.assertEqual(bridge.inspect_response(response, mode), REJECTED)
                    self.assertEqual(bridge.response_kind(response, mode), "final")
                    rewritten = bridge.continue_response(response, mode, 'synthetic-"token"')
                    self.assertIsNot(response, rewritten)
                    self.assertIs(bridge._get(rewritten, "usage"), usage)
                    self.assertEqual(plain(response), original)
                    self.assertNotIn(REJECTED, json.dumps(plain(rewritten)))
                    self.assertEqual(bridge.response_kind(rewritten, mode), "tools")
                    self.assertIsNone(bridge.inspect_response(rewritten, mode))
                    again = bridge.continue_response(response, mode, 'synthetic-"token"')
                    self.assertNotEqual(plain(rewritten), plain(again))
                    ended = bridge.finish_response(response, mode, FINISHED)
                    self.assertEqual(bridge.inspect_response(ended, mode), FINISHED)
                    self.assertEqual(bridge.response_kind(ended, mode), "final")
                    self.assertIs(bridge._get(ended, "usage"), usage)
                    self.assertNotIn(REJECTED, json.dumps(plain(ended)))
                    self.assertEqual(bridge._get(ended, "model"), "synthetic-model")
                    if mode == "anthropic_messages":
                        self.assertIs(bridge._get(rewritten, "content")[0], bridge._get(response, "content")[0])
                        self.assertIs(bridge._get(rewritten, "content")[1], bridge._get(response, "content")[1])
                    elif mode in {"codex_responses", "responses"}:
                        self.assertIs(bridge._get(rewritten, "output")[0], bridge._get(response, "output")[0])

    def test_direct_schema_required_and_unproven_deferred_rejected(self):
        for mode in MODES:
            tool = wire_tool(mode)
            self.assertTrue(bridge.supports_tool({"tools": [tool]}, mode))
            rewritten = bridge.continue_response(fixture(mode), mode, "token", request={"tools": [tool]})
            self.assertEqual(bridge.response_kind(rewritten, mode), "tools")
            with self.assertRaises(ValueError):
                bridge.continue_response(fixture(mode), mode, "token", request={"tools": []})
            self.assertTrue(bridge.supports_tool(namespace({"tools": [tool]}), mode))
            self.assertFalse(bridge.supports_tool({"tools": [{"type": "tool_search", "name": "tool_search"}]}, mode))
            self.assertFalse(bridge.supports_tool({"tools": [{**tool, "defer_loading": True}]}, mode))
            self.assertFalse(bridge.supports_tool({"tools": [{"type": "tool_search", "tools": [tool]}]}, mode))
            wrong = deepcopy(tool)
            params = wrong["function"]["parameters"] if mode == "chat_completions" else wrong["input_schema" if mode == "anthropic_messages" else "parameters"]
            params["properties"]["directive"] = {"type": "string"}
            self.assertFalse(bridge.supports_tool({"tools": [wrong]}, mode))
        self.assertFalse(bridge.supports_tool({}, "unknown"))

    def test_refusals_incomplete_and_unsupported_not_candidates(self):
        for mode in MODES:
            raw = fixture(mode)
            if mode == "chat_completions":
                raw["choices"][0]["finish_reason"] = "length"
            elif mode == "anthropic_messages":
                raw["stop_reason"] = "max_tokens"
            else:
                raw["status"] = "incomplete"
                raw["incomplete_details"] = {"reason": "max_output_tokens"}
            self.assertIsNone(bridge.inspect_response(raw, mode))
            self.assertEqual(bridge.response_kind(raw, mode), "incomplete")
            with self.assertRaises(ValueError):
                bridge.continue_response(raw, mode, "token")
        chat = fixture("chat_completions")
        chat["choices"][0]["message"]["refusal"] = "synthetic refusal"
        anthropic = fixture("anthropic_messages")
        anthropic["stop_reason"] = "refusal"
        codex = fixture("codex_responses")
        codex["output"][1]["content"] = [{"type": "refusal", "refusal": "synthetic refusal"}]
        for mode, raw in zip(MODES, (chat, anthropic, codex)):
            self.assertIsNone(bridge.inspect_response(raw, mode))
            self.assertEqual(bridge.response_kind(raw, mode), "safe_end")
        bedrock = fixture("anthropic_messages")
        bedrock["model_extra"] = {"amazon-bedrock-guardrailAction": "INTERVENED"}
        self.assertEqual(bridge.response_kind(bedrock, "anthropic_messages"), "safe_end")
        self.assertIsNone(bridge.inspect_response({}, "unknown"))
        self.assertEqual(bridge.response_kind({}, "unknown"), "unsupported")
        # Interleaved signatures may bind the removed text: do not claim safety.
        signed = fixture("anthropic_messages")
        signed["content"] = [signed["content"][2], signed["content"][0]]
        self.assertEqual(bridge.response_kind(signed, "anthropic_messages"), "unsupported")
        codex = fixture("codex_responses")
        codex["output"][1]["status"] = "in_progress"
        self.assertIsNone(bridge.inspect_response(codex, "codex_responses"))
        codex["output"][1]["status"] = "completed"
        codex["output"][1]["phase"] = "commentary"
        self.assertIsNone(bridge.inspect_response(codex, "codex_responses"))

    def test_native_middleware_normalization_and_tool_round(self):
        source = Path(os.environ.get("HERMES_SOURCE", "/usr/local/lib/hermes-agent"))
        self.assertTrue((source / "agent" / "turn_api_call.py").is_file(),
                        "Native integration requires HERMES_SOURCE pointing to Hermes")
        # Child bootstraps the installed dependency path BEFORE isolating home.
        # Passing an empty home before hermes_bootstrap hides its installed deps.
        env = dict(os.environ, HERMES_SOURCE=str(source),
                   HERMES_DISABLE_LAZY_INSTALLS="1", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-I", str(Path(__file__).resolve()), "--native"],
                                env=env, text=True, capture_output=True, timeout=180)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("NATIVE BRIDGE PASS: 3 transports, real middleware and tool rounds", result.stdout)
        self.assertIn("DEFERRED BRIDGE PASS: 3 transports, real assembly, dispatch and scoped denial", result.stdout)


def native_checks():
    """Actual facade/middleware/assembly/dispatch/round; only provider is synthetic."""
    from agent.turn_api_call import perform_api_call
    from agent.turn_tool_round import run_tool_round
    from agent.turn_final_response import finish_text_response
    from agent.chat_completion_helpers import build_assistant_message
    from agent.transports.chat_completions import ChatCompletionsTransport
    from agent.transports.anthropic import AnthropicTransport
    from agent.transports.codex import ResponsesApiTransport
    from hermes_cli import plugins
    from openai.types.chat import ChatCompletion
    from openai.types.responses import Response, ResponseOutputMessage, ResponseReasoningItem
    from anthropic.types import Message
    from tools.registry import registry
    from model_tools import get_tool_definitions, handle_function_call
    from tools.tool_search import assemble_tool_defs, scoped_deferrable_names
    from agent.subagent_lifecycle import get_active_subagent_parent
    from agent.turn_facade import TurnFacadeMixin

    def in_native_turn(agent, callback):
        # Enter the actual production facade: it owns bind_subagent_parent.
        # Replace only the conversation body with the focused native calls
        # below, not the context reader or binding implementation.
        def conversation_body(bound_agent, *args, **kwargs):
            assert bound_agent is agent
            assert get_active_subagent_parent() is agent
            return callback()

        assert get_active_subagent_parent() is None
        with patch("agent.conversation_loop.run_conversation", side_effect=conversation_body):
            result = TurnFacadeMixin.run_conversation(agent, "SYNTHETIC native turn", task_id="synthetic-task")
        assert get_active_subagent_parent() is None
        return result

    def native_api_call(agent, **kwargs):
        return in_native_turn(agent, lambda: perform_api_call(agent, **kwargs))

    import importlib.util
    os.environ["PROOFGATE_DB"] = str(Path(os.environ["HERMES_HOME"]) / "synthetic-proofgate.db")
    spec = importlib.util.spec_from_file_location(
        "proofgate_native_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    enforcement = plugin.enforcement
    manager = plugins.PluginManager()
    plugin.register(plugins.PluginContext(
        plugins.PluginManifest(name="proofgate", path=str(ROOT), source="user"), manager))
    entry = registry.get_entry(bridge.TOOL_NAME, scope=manager.scope_key)
    assert entry.handler is enforcement.control_tool and entry.toolset == "proofgate"
    # Normal tool search defaults, no config override or core/setup relabel.
    # Limit this synthetic session to the internal toolset: no external check_fn
    # probes or tools are needed to exercise the real native catalog.
    raw_defs = get_tool_definitions(
        enabled_toolsets=["proofgate"], quiet_mode=True, skip_tool_search_assembly=True)
    raw_snapshot = deepcopy(raw_defs)
    assembly = assemble_tool_defs(raw_defs)
    visible_defs = get_tool_definitions(enabled_toolsets=["proofgate"], quiet_mode=True)
    assert assembly.activated and assembly.deferred_count == 1
    assert bridge.TOOL_NAME in scoped_deferrable_names(raw_defs)
    assert visible_defs == assembly.tool_defs
    visible_names = {tool["function"]["name"] for tool in visible_defs}
    assert bridge.TOOL_NAME not in visible_names and "tool_call" in visible_names
    assert raw_defs == raw_snapshot
    assert get_tool_definitions(enabled_toolsets=["proofgate"], quiet_mode=True) == visible_defs
    transports = (ChatCompletionsTransport(), AnthropicTransport(), ResponsesApiTransport())
    cases = [(mode, transport, deferred) for deferred in (False, True)
             for mode, transport in zip(MODES, transports)]
    for mode, transport, deferred in cases:
        # Real installed SDK containers, not merely dict-shaped unit fixtures.
        raw_data = fixture(mode)
        if mode == "chat_completions":
            sdk = ChatCompletion.model_validate(raw_data)
        elif mode == "anthropic_messages":
            sdk = Message.model_validate(raw_data)
        else:
            # Construct the outer SDK object without unrelated request echoes;
            # preserve genuine SDK output items and its computed output_text.
            raw_data.pop("output_text")
            raw_data["output"] = [
                ResponseReasoningItem.model_validate(raw_data["output"][0]),
                ResponseOutputMessage.model_validate(raw_data["output"][1]),
            ]
            sdk = Response.model_construct(**raw_data)
        assert bridge.inspect_response(sdk, mode) == REJECTED
        rewritten_sdk = bridge.continue_response(sdk, mode, "sdk-token")
        assert rewritten_sdk.usage is sdk.usage
        normalized_sdk = transport.normalize_response(rewritten_sdk)
        assert normalized_sdk.tool_calls[0].name == bridge.TOOL_NAME
        assert not normalized_sdk.content
        finished_sdk = bridge.finish_response(sdk, mode, FINISHED)
        assert transport.normalize_response(finished_sdk).content == FINISHED

        session_tools = visible_defs if deferred else [bridge.tool_schema()]
        request = {"model": "synthetic-model", "tools": transport.convert_tools(session_tools)}
        schema_snapshot = deepcopy(request["tools"])
        # A visible wrapper without host-bound session scope is not sufficient.
        assert bridge.supports_tool(request, mode) is (not deferred)
        assert not bridge.supports_tool({"tools": [{"type": "tool_search", "tools": request["tools"]}]}, mode)
        if not deferred:
            assert not bridge.supports_tool({"tools": [{**request["tools"][0], "defer_loading": True}]}, mode)
        goal = "SYNTHETIC TEST: Collect a gym survey with hours, prices and IBAN requirements."
        messages = [{"role": "user", "content": goal}]
        session_id = "synthetic-session-" + mode + ("-deferred" if deferred else "-direct")
        identity = {"session_id": session_id, "turn_id": "synthetic-turn"}
        enforcement.observe_result({**identity, "tool_name": "synthetic_fixture"},
                                   {"synthetic_test": True, "hours": "09:00-17:00",
                                    "prices": None, "iban": None})
        responses = iter([namespace(fixture(mode)), namespace(fixture(mode, FINISHED))])
        provider_requests, executed, trace, projected = [], [], [], []

        def provider(kwargs):
            provider_requests.append(deepcopy(kwargs))
            return next(responses)

        def gate(*, request, next_call, **context):
            trace.append((context["api_mode"], context["api_request_id"], context["middleware_trace"]))
            return enforcement.execute(request=request, next_call=next_call, **context)

        def judge_state(state, phase):
            if phase == "scope":
                return {"scope": {"active": ("active", 0.99)}}
            assert phase == "outcome"
            assert state["tool_results"]
            verdict = "retry" if state["candidate_final"] == REJECTED else "complete"
            return {"outcome": {"result": (verdict, 0.99)}}

        def execute_tools(assistant, history, task_id, api_count):
            for tool_call in assistant.tool_calls:
                expected_name = "tool_call" if deferred else bridge.TOOL_NAME
                assert tool_call.name == expected_name
                arguments = json.loads(tool_call.arguments)
                inner = arguments["calls"][0]["arguments"] if deferred else arguments
                assert set(inner) == {"token"} and inner["token"]
                if deferred:
                    assert arguments == {"calls": [{"name": bridge.TOOL_NAME, "arguments": inner}]}
                result = handle_function_call(
                    tool_call.name, arguments, **identity, task_id=task_id,
                    tool_call_id=tool_call.id, enabled_toolsets=agent.enabled_toolsets,
                    disabled_toolsets=agent.disabled_toolsets)
                control = json.loads(result)
                assert control["policy_generated"] and not control["external_evidence"]
                assert control["directive"]
                enforcement.observe_result({**identity, "tool_name": "synthetic_fixture"},
                                           {"synthetic_test": True, "hours": "09:00-17:00",
                                            "prices": "synthetic 20", "iban": "synthetic EU accepted",
                                            "recording": "synthetic fixture only"})
                executed.append(tool_call.id)
                history.append({"role": "tool", "name": tool_call.name,
                                "tool_call_id": tool_call.id,
                                "content": result})

        agent = SimpleNamespace(
            api_mode=mode, model="synthetic-model", provider="synthetic-offline",
            base_url="https://invalid.example", session_id=session_id, platform="test", client=Mock(),
            _disable_streaming=True, thinking_callback=None, reasoning_callback=None,
            stream_delta_callback=None, _stream_callback=None, quiet_mode=True, verbose_logging=False,
            _tool_guardrail_halt_decision=None, context_compressor=SimpleNamespace(awaiting_real_usage_after_compression=True),
            valid_tool_names={tool["function"]["name"] for tool in session_tools}, tools=session_tools,
            enabled_toolsets=["proofgate"], disabled_toolsets=None,
            _conversation_root_id=lambda: session_id,
            _has_pending_redirect=lambda: False, _get_transport=lambda: transport,
            _is_copilot_url=lambda: False, _is_codex_backend=lambda: False,
            _interruptible_api_call=provider, _uniquify_tool_call_ids=lambda calls: None,
            _cap_delegate_task_calls=lambda calls: calls, _deduplicate_tool_calls=lambda calls: calls,
            _flush_messages_to_session_db=lambda *args: True,
            _emit_interim_assistant_message=lambda message: projected.append(message),
            _interim_assistant_visible_text=lambda message: message.get("content") or "",
            _execute_tool_calls=execute_tools, _touch_activity=lambda *args: None,
            _extract_reasoning=lambda message: message.reasoning,
            _needs_thinking_reasoning_pad=lambda: False, _strip_think_blocks=lambda text: text,
            _split_responses_tool_id=lambda value: (value, None),
            _derive_responses_function_call_id=lambda call_id, item_id: item_id or "fc_" + call_id,
            _has_content_after_think_block=bool,
            _emit_pending_fallback_notice=lambda: None, _clear_status_buffer=lambda: None,
            _looks_like_codex_intermediate_ack=lambda **kwargs: False,
            _current_turn_id="synthetic-turn",
        )
        agent._build_assistant_message = lambda message, reason: build_assistant_message(agent, message, reason)
        if deferred:
            def check_scope():
                assert bridge.supports_tool(request, mode)
                rewritten = bridge.continue_response(sdk, mode, "sdk-token", request=request)
                assert transport.normalize_response(rewritten).tool_calls[0].name == "tool_call"
                assert rewritten.usage is sdk.usage
                assert not transport.normalize_response(rewritten).content
                assert REJECTED not in json.dumps(plain(rewritten))
                malformed = deepcopy(request)
                dispatcher = next(tool for tool in malformed["tools"]
                                  if bridge._get(bridge._get(tool, "function", tool), "name") == "tool_call")
                params = (dispatcher["function"]["parameters"] if mode == "chat_completions"
                          else dispatcher["input_schema" if mode == "anthropic_messages" else "parameters"])
                params["properties"]["calls"]["items"]["properties"]["arguments"]["type"] = "string"
                assert not bridge.supports_tool(malformed, mode)
                with patch("model_tools.get_tool_definitions", side_effect=RuntimeError("catalog unavailable")):
                    assert not bridge.supports_tool(request, mode)
                with patch.object(agent, "valid_tool_names", set()):
                    assert not bridge.supports_tool(request, mode)
                with patch.object(agent, "tools", []):
                    assert not bridge.supports_tool(request, mode)
                with patch.object(agent, "enabled_toolsets"):
                    del agent.enabled_toolsets
                    assert not bridge.supports_tool(request, mode)
                for enabled, disabled in (([], None), (["proofgate"], ["proofgate"])):
                    agent.enabled_toolsets, agent.disabled_toolsets = enabled, disabled
                    assert not bridge.supports_tool(request, mode)
                    try:
                        bridge.continue_response(sdk, mode, "denied-token", request=request)
                    except ValueError:
                        pass
                    else:
                        raise AssertionError("Out-of-scope continuation accepted")
                    denied = json.loads(handle_function_call(
                        "tool_call", {"calls": [{"name": bridge.TOOL_NAME, "arguments": {"token": "denied"}}]},
                        **identity, enabled_toolsets=enabled, disabled_toolsets=disabled))
                    assert "not available in this session" in denied["error"]
                agent.enabled_toolsets, agent.disabled_toolsets = ["proofgate"], None
                assert bridge.supports_tool(request, mode)

            with patch.object(plugins, "_delivery_manager", return_value=manager), \
                    patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in synthetic test")):
                in_native_turn(agent, check_scope)
        manager._middleware["llm_execution"] = [gate]
        with patch.object(plugins, "_delivery_manager", return_value=manager), \
                patch.object(plugin.jev, "judge_state", side_effect=judge_state), \
                patch("socket.socket.connect", side_effect=AssertionError("Network forbidden in synthetic test")):
            for iteration in (1, 2):
                if mode == "chat_completions":
                    request["messages"] = deepcopy(messages)
                elif mode == "anthropic_messages":
                    system, converted = transport.convert_messages(messages)
                    request.update(system=system, messages=converted)
                else:
                    request["input"] = transport.convert_messages(messages, model=agent.model)
                    request["instructions"] = "SYNTHETIC TEST system instructions"
                call = native_api_call(
                    agent, api_kwargs=request, _original_api_kwargs=deepcopy(request),
                    _llm_middleware_trace=[{"source": "synthetic-test"}], _moa_prepared_request=None,
                    _retry=SimpleNamespace(), thinking_spinner=None, retry_count=0,
                    api_call_count=iteration, api_request_id=f"synthetic-{iteration}",
                    effective_task_id="synthetic-task", turn_id="synthetic-turn", interrupted=False,
                )
                assert call.action == "fallthrough"
                normalized = transport.normalize_response(call.response)
                if iteration == 1:
                    assert normalized.finish_reason == "tool_calls"
                    assert not normalized.content
                    assert REJECTED not in json.dumps(plain(normalized))
                    if mode == "anthropic_messages":
                        blocks = normalized.anthropic_content_blocks
                        assert [b["type"] for b in blocks] == ["thinking", "redacted_thinking", "tool_use"]
                        assert blocks[0]["signature"] == "signature-one"
                        assert blocks[1]["data"] == "opaque-two"
                    if mode == "codex_responses":
                        assert normalized.codex_message_items is None
                        assert normalized.codex_reasoning_items[0]["encrypted_content"] == "opaque-three"
                    result = run_tool_round(
                        agent, assistant_message=normalized, finish_reason=normalized.finish_reason,
                        messages=messages, conversation_history=[], api_call_count=iteration,
                        effective_task_id="synthetic-task", user_message=goal,
                        system_message="", active_system_prompt="", compression_attempts=0,
                        max_compression_attempts=0, final_response="", failed=False,
                        _turn_exit_reason=None, truncated_tool_call_retries=0, current_turn_user_idx=0,
                    )
                    assert result.action == "continue"
                    assert len(executed) == 1
                    assert messages[-1]["role"] == "tool"
                    assert messages[-1]["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]
                    assert REJECTED not in json.dumps(messages)
                    assert all(not row.get("content") for row in projected)
                else:
                    assert normalized.content == FINISHED
                    assert not normalized.tool_calls
                    assert normalized.finish_reason == "stop"
                    ended = finish_text_response(
                        agent, assistant_message=normalized, response=call.response,
                        finish_reason=normalized.finish_reason, messages=messages,
                        api_messages=messages, conversation_history=[], api_call_count=iteration,
                        user_message=goal, active_system_prompt="", final_response=None,
                        _turn_exit_reason=None, _preflight_compression_blocked=False,
                        codex_ack_continuations=0, truncated_response_parts=[],
                        length_continue_retries=0, _pending_verification_response=None,
                        _pending_verification_response_previewed=False,
                        effective_task_id="synthetic-task")
                    assert ended.action == "break"
                    assert ended.final_response == FINISHED
                    assert messages[-1]["content"] == FINISHED
                assert request["tools"] == schema_snapshot
            assert len(provider_requests) == 2
            assert REJECTED not in json.dumps(provider_requests[1])
            assert bridge.TOOL_NAME in json.dumps(provider_requests[1])
            assert "policy_generated" in json.dumps(provider_requests[1])
            assert "external_evidence" in json.dumps(provider_requests[1])
            con = enforcement.ledger.connect()
            try:
                evidence = enforcement.ledger.read_evidence(con, session_id, "synthetic-turn")
                assert "policy_generated" not in json.dumps(evidence)
            finally:
                con.close()
            assert len(trace) == 2 and trace[0][0] == mode
            assert trace[0][2] == [{"source": "synthetic-test"}]
            # Provider exceptions propagate through actual single-use middleware.
            expected = RuntimeError("synthetic-provider-failure")
            agent._interruptible_api_call = Mock(side_effect=expected)
            try:
                native_api_call(
                    agent, api_kwargs=request, _original_api_kwargs=request,
                    _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(),
                    thinking_spinner=None, retry_count=0, api_call_count=3,
                    api_request_id="synthetic-failure", effective_task_id="synthetic-task",
                    turn_id="synthetic-turn", interrupted=False,
                )
            except RuntimeError as exc:
                assert exc is expected
            else:
                raise AssertionError("Provider error swallowed")
            assert agent._interruptible_api_call.call_count == 1
            # Demonstrate, rather than hide, why streaming consumers are outside
            # the enforcement guarantee: bytes leave during next_call itself.
            leaked = []
            def streaming_provider(payload, on_first_delta):
                leaked.append(REJECTED)
                return namespace(fixture(mode))
            agent._disable_streaming = False
            agent._has_stream_consumers = lambda: True
            agent._interruptible_streaming_api_call = streaming_provider
            streamed = native_api_call(
                agent, api_kwargs=request, _original_api_kwargs=request,
                _llm_middleware_trace=[], _moa_prepared_request=None, _retry=SimpleNamespace(),
                thinking_spinner=None, retry_count=0, api_call_count=4,
                api_request_id="synthetic-stream", effective_task_id="synthetic-task",
                turn_id="synthetic-turn", interrupted=False)
            assert leaked == [REJECTED]
            assert not transport.normalize_response(streamed.response).content
    print("NATIVE BRIDGE PASS: 3 transports, real middleware and tool rounds")
    print("DEFERRED BRIDGE PASS: 3 transports, real assembly, dispatch and scoped denial")


if __name__ == "__main__":
    if "--native" in sys.argv:
        sys.path.insert(0, os.environ.get("HERMES_SOURCE", "/usr/local/lib/hermes-agent"))
        os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        importlib.import_module("hermes_bootstrap")  # Load installed dependency target.
        with tempfile.TemporaryDirectory(prefix="proofgate-native-test-") as home:
            os.environ["HERMES_HOME"] = home
            native_checks()
    else:
        unittest.main()
