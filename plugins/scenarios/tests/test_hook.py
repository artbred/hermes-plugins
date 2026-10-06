"""Offline contract for the sole current-human routing hook."""
import copy
import importlib
import socket
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

PLUGINS = Path(__file__).resolve().parents[2]
if str(PLUGINS) not in sys.path:
    sys.path.insert(0, str(PLUGINS))
scenarios = importlib.import_module("scenarios")


class HookTests(unittest.TestCase):
    def setUp(self):
        self.network = patch.object(socket.socket, "connect", side_effect=AssertionError("offline test"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.classify = patch.object(scenarios.classifier, "classify", return_value=["Use ompx in background; parent verifies."]).start()
        self.addCleanup(patch.stopall)

    def test_registration_is_only_pre_llm_call(self):
        ctx = Mock()
        scenarios.register(ctx)
        self.assertEqual(ctx.mock_calls, [unittest.mock.call.register_hook("pre_llm_call", scenarios.pre_llm_call)])

    def test_only_current_request_reaches_classifier(self):
        history = [
            {"role": "system", "content": "SYSTEM_PRIVATE"},
            {"role": "developer", "content": "DEVELOPER_PRIVATE"},
            {"role": "user", "content": "OLD_PRIVATE"},
            {"role": "tool", "content": "TOOL_PRIVATE"},
            {"role": "user", "content": "Implement the parser and regression tests.", "api_content": "MEMORY_PRIVATE"},
        ]
        original = copy.deepcopy(history)
        result = scenarios.pre_llm_call(user_message=history[-1]["content"], conversation_history=history,
                                       session_id="PRIVATE_ID", memory="MEMORY_PRIVATE")
        self.assertEqual(result, {"context": "Use ompx in background; parent verifies."})
        self.classify.assert_called_once_with("Implement the parser and regression tests.")
        self.assertEqual(history, original)

    def test_no_match_and_errors_are_no_output_mutation(self):
        reply = {"role": "assistant", "content": "Final answer stays exactly as written."}
        before = copy.deepcopy(reply)
        self.classify.return_value = []
        self.assertIsNone(scenarios.pre_llm_call(user_message="Discuss the design", response=reply))
        self.classify.side_effect = RuntimeError("PRIVATE exception detail")
        with self.assertLogs("scenarios", level="WARNING") as logs:
            self.assertIsNone(scenarios.pre_llm_call(user_message="Do the work", response=reply))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.assertEqual(reply, before)
        self.assertFalse(hasattr(scenarios, "transform_llm_output"))

    def test_multimodal_only_allowlisted_human_text(self):
        content = [
            {"type": "text", "text": "Implement the parser"},
            {"type": "image_url", "image_url": {"url": "data:PRIVATE_IMAGE"}, "text": "MEDIA_PRIVATE"},
            {"type": "input_audio", "input_audio": {"data": "PRIVATE_AUDIO"}},
            {"type": "input_text", "text": "and regression tests."},
            {"type": "tool_result", "content": "TOOL_PRIVATE"},
            {"type": "thinking", "text": "REASONING_PRIVATE"},
            {"type": "output_text", "text": "ASSISTANT_PRIVATE"},
            {"type": "text", "role": "developer", "text": "DEVELOPER_PRIVATE"},
        ]
        before = copy.deepcopy(content)
        scenarios.pre_llm_call(user_message=content)
        self.classify.assert_called_once_with("Implement the parser\nand regression tests.")
        self.assertEqual(content, before)

    def test_nontext_empty_and_unsupported_never_classify(self):
        for content in (None, "", "  \n", 12, {}, {"text": "not a native turn"},
                        [None, 42], [{"type": "image", "source": {"data": "private"}}],
                        [{"type": "text", "text": {"nested": "private"}}],
                        [SimpleNamespace(type="text", text="unsupported object")]):
            with self.subTest(content=content):
                self.assertIsNone(scenarios.pre_llm_call(user_message=content))
        self.classify.assert_not_called()

    def test_oversized_full_input_never_classifies_prefix(self):
        for content in ("x" * 4001, " " * 4001 + "Fix bug", "😀" * 4001,
                        [{"type": "text", "text": "x" * 2500}, {"type": "text", "text": "y" * 2500}],
                        "Fix bug\n\n<memory-context>" + "x" * 4001 + "</memory-context>"):
            with self.subTest(kind=type(content).__name__):
                self.assertIsNone(scenarios.pre_llm_call(user_message=content))
        self.classify.assert_not_called()

    def test_malformed_unicode_abstains(self):
        self.assertIsNone(scenarios.pre_llm_call(user_message="Fix bug\ud800"))
        self.classify.assert_not_called()

    def test_internal_envelopes_without_metadata_never_classify(self):
        for text in ("[System: model changed]", "[SYSTEM] background notice", "[Runtime note: resumed]",
                     "[CONTEXT SUMMARY]: old task", "[CONTEXT COMPACTION — REFERENCE ONLY] old task",
                     "[IMPORTANT: Background process completed]", "[ASYNC DELEGATION] completed",
                     "[OUT-OF-BAND] notification", "Cronjob Response: completed", "<task-notification>done</task-notification>",
                     "<system-reminder>internal</system-reminder>", "[Your active task list] old tasks",
                     "[Planning state preserved] old work"):
            with self.subTest(text=text):
                self.assertIsNone(scenarios.pre_llm_call(user_message=text))
        self.classify.assert_not_called()

    def test_native_display_metadata_beats_human_looking_text(self):
        for kind in ("hidden", "notification", "internal", "compression_summary", "cron"):
            row = {"role": "user", "content": "Implement a complete parser", "display_kind": kind}
            with self.subTest(kind=kind):
                self.assertIsNone(scenarios.pre_llm_call(user_message=row["content"], conversation_history=[row]))
        self.classify.assert_not_called()

    def test_explicit_human_metadata_beats_arbitrary_notice_looking_text(self):
        text = "[System: please explain this literal syntax]"
        for kind in (None, "", "steer"):
            row = {"role": "user", "content": text, "display_kind": kind}
            with self.subTest(kind=kind):
                scenarios.pre_llm_call(user_message=text, conversation_history=[row])
        self.assertEqual(self.classify.call_count, 3)
        self.classify.assert_called_with(text)

    def test_unrelated_historical_metadata_is_not_current_authority(self):
        history = [{"role": "user", "content": "old background notice", "display_kind": "hidden"}]
        scenarios.pre_llm_call(user_message="Implement the parser", conversation_history=history)
        self.classify.assert_called_once_with("Implement the parser")

    def test_older_equal_human_row_cannot_authorize_current_notice(self):
        text = "[System: machine notice]"
        history = [{"role": "user", "content": text, "display_kind": ""},
                   {"role": "user", "content": "a later, unrelated human turn"}]
        self.assertIsNone(scenarios.pre_llm_call(user_message=text, conversation_history=history))
        self.classify.assert_not_called()

    def test_native_clean_persistence_variant_keeps_synthetic_metadata(self):
        history = [{"role": "user", "content": "Gateway envelope: implement the parser",
                    "display_kind": "internal_notification"}]
        self.assertIsNone(scenarios.pre_llm_call(
            user_message="Implement the parser", conversation_history=history, turn_id="synthetic-turn"))
        self.classify.assert_not_called()

    def test_latest_equal_row_wins_over_old_human_row(self):
        text = "Implement the parser"
        history = [{"role": "user", "content": text, "display_kind": ""},
                   {"role": "assistant", "content": "old reply"},
                   {"role": "user", "content": text, "display_kind": "notification"}]
        self.assertIsNone(scenarios.pre_llm_call(user_message=text, conversation_history=history))
        self.classify.assert_not_called()

    def test_context_appendages_never_leave_plugin(self):
        for suffix in ("\n\n<memory-context>MEMORY_PRIVATE</memory-context>",
                       "\n\n<memory-context>UNTERMINATED_PRIVATE",
                       "\n\n--- Attached Context ---\n\nFILE_PRIVATE",
                       "\n\n--- Context Warnings ---\nWARNING_PRIVATE",
                       "\n\n[System note: The following is recalled memory context, NOT new user input.]\nMEMORY_PRIVATE"):
            with self.subTest(suffix=suffix):
                scenarios.pre_llm_call(user_message="Implement @file:parser.py" + suffix)
                self.classify.assert_called_with("Implement @file:parser.py")
        self.assertEqual(self.classify.call_count, 5)

    def test_multimodal_context_appendage_is_not_human_text(self):
        scenarios.pre_llm_call(user_message=[{"type": "text", "text": "Implement the parser"},
                                            {"type": "text", "text": "<memory-context>PRIVATE</memory-context>"},
                                            {"type": "text", "text": "PLUGIN_CONTEXT_PRIVATE"}])
        self.classify.assert_called_once_with("Implement the parser")

    def test_native_control_appendages_never_leave_plugin(self):
        for tag in ("system-reminder", "task-notification", "ide_opened_file", "ide_selection"):
            with self.subTest(tag=tag):
                scenarios.pre_llm_call(user_message=f"Implement the parser\n<{tag}>PRIVATE</{tag}>")
                self.classify.assert_called_with("Implement the parser")

    def test_auto_loaded_skill_scaffolding_abstains(self):
        text = '[IMPORTANT: The "synthetic" skill is auto-loaded. Follow its instructions for this session.]\nPRIVATE'
        for history in ([], [{"role": "user", "content": text, "display_kind": ""}]):
            self.assertIsNone(scenarios.pre_llm_call(user_message=text, conversation_history=history))
        self.classify.assert_not_called()

    def test_delegated_model_authored_child_prompt_does_not_classify(self):
        self.assertIsNone(scenarios.pre_llm_call(
            user_message="Implement the parser from this model-authored task.",
            parent_session_id="synthetic-parent"))
        self.classify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
