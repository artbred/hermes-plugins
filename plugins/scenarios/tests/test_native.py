"""Installed Hermes integration without actor or classifier provider calls."""
import copy
import importlib
import json
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGINS = Path(__file__).resolve().parents[2]
NATIVE = Path("/usr/local/lib/hermes-agent")
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))
scenarios = importlib.import_module("scenarios")


@unittest.skipUnless((NATIVE / "agent" / "turn_context.py").is_file(), "installed Hermes unavailable")
class NativeIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        # Import failures in an existing installation are failures, not skipped coverage.
        cls.native = importlib.import_module("agent.turn_context")
        cls.lifecycle = importlib.import_module("hermes_cli.lifecycle")
        cls.spill = importlib.import_module("tools.hook_output_spill")

    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline harness"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.agent = SimpleNamespace(session_id="synthetic-session", model="synthetic-model", platform="cli",
                                     _persist_disabled=False, _current_turn_timestamp=0,
                                     ephemeral_system_prompt="", _copy_reasoning_content_for_api=lambda *_: None,
                                     _should_sanitize_tool_calls=lambda: False)
        self.guidance = "Use existing ompx in background. Parent independently verifies the result."
        self.classify = patch.object(scenarios.classifier, "classify", return_value=[self.guidance]).start()
        self.addCleanup(patch.stopall)

    def collect(self, content, messages):
        callbacks = {}
        scenarios.register(SimpleNamespace(register_hook=lambda name, callback: callbacks.setdefault(name, callback)))
        self.assertEqual(list(callbacks), ["pre_llm_call"])

        def dispatch(name, **kwargs):
            self.assertEqual(name, "pre_llm_call")
            self.assertEqual(kwargs["user_message"], content)
            self.assertEqual(kwargs["conversation_history"], messages)
            return [callbacks[name](**kwargs)]

        with patch.object(self.lifecycle, "invoke_hook", side_effect=dispatch), \
             patch.object(self.spill, "get_spill_config", return_value={}), \
             patch.object(self.spill, "spill_if_oversized", side_effect=lambda text, **_: text):
            return self.native._collect_pre_llm_call_context(
                self.agent, effective_task_id="synthetic-task", turn_id="synthetic-turn",
                original_user_message=content, messages=messages, conversation_history=[])

    def test_native_collector_and_all_actor_wire_formats_leave_final_reply_unchanged(self):
        text = "Implement a parser with comprehensive regression tests."
        final = {"role": "assistant", "content": "Unchanged final response from the actor."}
        messages = [{"role": "user", "content": text, "display_kind": ""}, copy.deepcopy(final)]
        before = copy.deepcopy(messages)
        context = self.collect(text, messages)
        self.assertEqual(context, self.guidance)
        wire, system = self.native.build_api_messages(
            self.agent, messages, current_turn_user_idx=0, ext_prefetch_cache="NATIVE_MEMORY_PRIVATE",
            plugin_user_context=context, moa_config=None, active_system_prompt="SYSTEM_PRIVATE")
        self.assertEqual(system, "SYSTEM_PRIVATE")
        self.assertNotIn(self.guidance, system)
        self.assertIn(self.guidance, wire[1]["content"])
        self.assertIn("NATIVE_MEMORY_PRIVATE", wire[1]["content"])
        self.assertEqual(wire[-1], final)
        self.classify.assert_called_once_with(text)
        self.assertEqual(messages, before)

        from agent.anthropic_message_convert import convert_messages_to_anthropic
        from agent.codex_responses_adapter import _chat_messages_to_responses_input
        from agent.gemini_native_adapter import build_gemini_request

        anthropic_system, anthropic = convert_messages_to_anthropic(wire)
        formats = {
            "chat_completions": wire,
            "anthropic_messages_bedrock_vertex": anthropic,
            "responses": _chat_messages_to_responses_input(wire),
            "responses_xai": _chat_messages_to_responses_input(wire, is_xai_responses=True),
            "responses_github": _chat_messages_to_responses_input(wire, is_github_responses=True),
            "gemini_native": build_gemini_request(messages=wire)["contents"],
        }
        self.assertNotIn(self.guidance, json.dumps(anthropic_system))
        for mode, payload in formats.items():
            with self.subTest(mode=mode):
                encoded = json.dumps(payload)
                self.assertIn(self.guidance, encoded)
                self.assertIn(final["content"], encoded)
                self.assertNotIn("verification stopped", encoded.lower())
        self.assertEqual(messages, before)
        self.assertEqual(wire[-1], final)

    def test_native_no_match_has_no_context_or_final_reply_rewrite(self):
        self.classify.return_value = []
        text = "Explain parser designs without changing code."
        messages = [{"role": "user", "content": text}]
        self.assertEqual(self.collect(text, messages), "")
        final = {"role": "assistant", "content": "A normal explanatory answer."}
        messages.append(final)
        wire, _ = self.native.build_api_messages(
            self.agent, messages, current_turn_user_idx=0, ext_prefetch_cache="", plugin_user_context="",
            moa_config=None, active_system_prompt="")
        self.assertEqual(wire, messages)
        self.assertEqual(wire[-1], final)

    def test_native_typed_internal_event_does_not_classify(self):
        text = "Implement new functionality now."
        messages = [{"role": "user", "content": text, "display_kind": "notification"}]
        self.assertEqual(self.collect(text, messages), "")
        self.classify.assert_not_called()

    def test_native_multimodal_context_composition_is_private_to_actor(self):
        text = "Implement the requested parser."
        content = [{"type": "text", "text": text},
                   {"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_MEDIA"}}]
        messages = [{"role": "user", "content": content}]
        context = self.collect(content, messages)
        self.classify.assert_called_once_with(text)
        actor_context = self.native.compose_multimodal_context_part("MEMORY_PRIVATE", context)
        self.assertIn("MEMORY_PRIVATE", actor_context)
        self.assertIn(self.guidance, actor_context)
        self.assertNotIn("MEMORY_PRIVATE", json.dumps(self.classify.call_args.args))
        self.assertNotIn("PRIVATE_MEDIA", json.dumps(self.classify.call_args.args))

    def test_native_current_human_projection_strips_merged_summary(self):
        from agent.context_compressor import (
            COMPRESSED_SUMMARY_METADATA_KEY, SUMMARY_PREFIX, _MERGED_PRIOR_CONTEXT_HEADER,
            _MERGED_SUMMARY_DELIMITER, user_originated_turn_view,
        )
        text = "Implement the requested parser."
        merged = f"{_MERGED_PRIOR_CONTEXT_HEADER}\n{text}\n\n{_MERGED_SUMMARY_DELIMITER}\n{SUMMARY_PREFIX}\nSUMMARY_PRIVATE"
        row = {"role": "user", "content": merged, COMPRESSED_SUMMARY_METADATA_KEY: True,
               "display_kind": "hidden", "api_content": "SIDECAR_PRIVATE"}
        view = user_originated_turn_view(row)
        self.assertIsNotNone(view)
        self.assertEqual(view["content"], text)
        self.assertEqual(self.collect(merged, [row]), self.guidance)
        self.classify.assert_called_once_with(text)

    def test_native_memory_fence_and_attached_context_are_removed(self):
        text = "Implement @file:parser.py"
        composed = self.native.compose_user_api_content(text, "MEMORY_PRIVATE", "OTHER_PLUGIN_PRIVATE")
        self.assertIsNotNone(composed)
        self.assertEqual(self.collect(composed, [{"role": "user", "content": composed}]), self.guidance)
        self.classify.assert_called_once_with(text)

    def test_native_skill_body_is_not_user_request(self):
        from agent.skill_commands import extract_user_instruction_from_skill_message

        text = "Implement the parser and regression tests."
        scaffold = (
            '[IMPORTANT: The user has invoked the "synthetic" skill. '
            "The full skill content is loaded below.]\nSKILL_BODY_PRIVATE\n"
            "The user has provided the following instruction alongside the skill invocation: "
            + text + "\n\n[Runtime note: PRIVATE]"
        )
        self.assertEqual(extract_user_instruction_from_skill_message(scaffold), text)
        self.assertEqual(self.collect(scaffold, [{"role": "user", "content": scaffold}]), self.guidance)
        self.classify.assert_called_once_with(text)


if __name__ == "__main__":
    unittest.main()
