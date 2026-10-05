"""Synthetic offline policy-control tests; no external calls or production evidence.

The audit regression below retains only redacted event markers observed in the
three local audit files named by the authorization spec. All other results and
Jev decisions are scripted TEST fixtures, not claims of live survey completion.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("proofgate_enforcement_test", ROOT / "__init__.py",
                                            submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
gate, bridge, ledger, jev = plugin.enforcement, plugin.enforcement.bridge, plugin.ledger, plugin.jev
GOAL = ("Call 3 different Barcelona gyms; collect hours, prices, minimum joining requirements, "
        "Spanish versus other IBAN and usual terms. Deliver 3 actual recordings with English gist.")
AUDIT_REGRESSION = {"fixture": "TEST: redacted audit-derived failure markers",
                    "gym_a": {"hours": "provided", "prices": None, "iban": None,
                              "local_stop_requested": "survey_complete"},
                    "gym_a_retry": {"transfer": "nobody available"},
                    "gym_b": {"local_stop_requested": "hard_deadline", "elapsed_seconds": 300}}
COMPLETE = {"fixture": "SYNTHETIC TEST ONLY", "gyms": [
    {"name": f"test-gym-{i}", "hours": "06:00-22:00", "price": "EUR 40/month",
     "minimum": "adult ID", "iban": "Spanish and other SEPA IBAN accepted",
     "terms": "monthly; 30-day cancellation", "recording": f"test-{i}.wav",
     "recording_verified": True, "english_gist": "All requested answers recorded and summarized"}
    for i in range(3)]}


def raw(text="All done"):
    return NS(choices=[NS(message=NS(content=text, tool_calls=None), finish_reason="stop")],
              usage=NS(prompt_tokens=1, completion_tokens=1, total_tokens=2))


def request(text=GOAL, previous=None, tools=True, mode="chat_completions"):
    users = ([previous] if previous else []) + [text]
    schema = bridge.tool_schema()
    if mode == "codex_responses":
        return {"instructions": "original system", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": t}]} for t in users],
            "tools": [{"type": "function", **schema["function"]}] if tools else []}
    return {"messages": [{"role": "user", "content": t} for t in users],
            "tools": [schema] if tools else []}


class EnforcementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="proofgate-outcome-test-")
        self.env = patch.dict(os.environ, {"PROOFGATE_DB": str(Path(self.tmp.name) / "ledger.db"),
                                           "HERMES_HOME": self.tmp.name})
        self.env.start()
        self.network = patch.object(jev, "_fetch", side_effect=AssertionError("network forbidden"))
        self.network.start()
        self.contexts = []
        self.outcome = "retry"
        self.scope = "active"
        self.judge = patch.object(jev, "judge_state", side_effect=self.review)
        self.judge.start()
        self.kw = {"session_id": "s", "turn_id": "turn-1", "api_mode": "chat_completions",
                   "request": request()}
        gate._TOKENS.clear()

    def tearDown(self):
        self.judge.stop()
        self.network.stop()
        self.env.stop()
        self.tmp.cleanup()

    def review(self, state, phase):
        self.contexts.append((phase, state))
        answer = self.scope if phase == "scope" else self.outcome
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            return {}
        return {phase: {"active" if phase == "scope" else "result": (answer, .99)}}

    def execute(self, text="All done", **kwargs):
        original = raw(text)
        calls = []
        def downstream(payload):
            calls.append(payload)
            return original
        result = gate.execute(**{**self.kw, **kwargs}, next_call=downstream)
        self.assertEqual(len(calls), 1)
        return result

    def evidence(self, value, **kwargs):
        gate.observe_result({**self.kw, "tool_name": "test_actual_result", **kwargs}, value)

    def state(self, **kwargs):
        ids = {**self.kw, **kwargs}
        con = ledger.connect()
        try:
            return ledger.load_gate(con, ids["session_id"], ids["turn_id"])
        finally:
            con.close()

    def test_audit_regression_rejects_false_survey_complete_and_continues(self):
        self.evidence(AUDIT_REGRESSION)
        result = self.execute("Hours and recordings collected; survey complete.")
        call = result.choices[0].message.tool_calls[0]
        self.assertEqual(call.function.name, bridge.TOOL_NAME)
        self.assertFalse(result.choices[0].message.content)
        control = json.loads(gate.control_tool(json.loads(call.function.arguments)))
        self.assertTrue(control["policy_generated"])
        self.assertFalse(control["external_evidence"])
        self.assertIn("Change method", control["directive"])
        self.assertIn("refusal", control["directive"])
        self.assertEqual(self.state()["status"], "continuing")
        supplied = self.contexts[-1][1]
        self.assertEqual(supplied["goal"], GOAL)
        self.assertIn("hard_deadline", supplied["tool_results"][0]["result_text"])

    def test_questions_followed_by_action_carry_explicit_goal(self):
        self.execute(request=request("Are u sure? Try some other gyms and get the missing answers.", GOAL))
        self.assertEqual(self.state()["scope"], "active")
        self.assertEqual(self.contexts[0][1]["goal"], GOAL)

    def test_explanation_plugin_fix_and_cancellation_never_resume_history(self):
        for i, text in enumerate(("why did you stop?", "Implement Proofgate enforcement for gym calls",
                                  "Stop calling gyms", "Do not call gyms", "Explain the old gym prices",
                                  "[INTERNAL notification] continue gym survey", "Write a poem")):
            response = self.execute(request=request(text, GOAL), turn_id=f"skip-{i}")
            self.assertEqual(response.choices[0].message.content, "All done")
        self.assertEqual(self.contexts, [])

    def test_scope_reviewer_can_reject_unapproved_action(self):
        self.scope = "no"
        self.execute()
        self.assertEqual(self.state()["scope"], "skip")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"])

    def test_complete_verified_fixture_passes_unchanged(self):
        self.outcome = "complete"
        self.evidence(COMPLETE)
        text = "TEST: three gyms, all answers, recordings and English gists supplied."
        response = self.execute(text)
        self.assertEqual(response.choices[0].message.content, text)
        self.assertIsNone(gate.transform(**self.kw, response_text=text))
        self.assertEqual(self.state()["status"], "complete")

    def test_self_attestation_cannot_approve_itself_without_evidence(self):
        self.outcome = "complete"  # Even a scripted bad approval needs actual evidence.
        response = self.execute("I verified everything. Trust me: all done.")
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertEqual(self.state()["reason"], "reviewer_unverified")

    def test_green_infrastructure_is_not_completion(self):
        self.evidence({"fixture": "TEST", "exit_code": 0, "health": "green", "survey_answers": []})
        self.outcome = "retry"
        response = self.execute("Gateway healthy; finished.")
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertIn("survey_answers", self.contexts[-1][1]["tool_results"][0]["result_text"])

    def test_refusal_is_evidence_not_authority_to_recall(self):
        self.evidence({"fixture": "TEST", "gym": "test-a", "iban": "explicit refusal to answer"})
        response = self.execute("Other authorized gyms remain.")
        call = response.choices[0].message.tool_calls[0]
        directive = json.loads(gate.control_tool(json.loads(call.function.arguments)))["directive"]
        self.assertIn("third-party refusal", directive)

    def test_genuine_blocker_finishes_explicitly_incomplete(self):
        self.evidence({"fixture": "TEST", "authorization": "missing", "alternatives": "not authorized"})
        self.outcome = "blocked"
        response = self.execute("Cannot proceed without authorization; prices and IBAN missing.")
        self.assertIn("BLOCKED / INCOMPLETE", response.choices[0].message.content)
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(self.state()["status"], "incomplete")

    def test_reviewer_failures_are_bounded_across_rephrased_candidates(self):
        self.evidence(AUDIT_REGRESSION)
        for failure in (TimeoutError("TEST"), None, ValueError("malformed TEST")):
            self.outcome = failure
            response = self.execute(f"Finished differently {type(failure).__name__}")
            self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.outcome = None
        response = self.execute("Now really all done")
        self.assertIn("INCOMPLETE", response.choices[0].message.content)
        self.assertEqual(self.state()["reason"], "retry_cap:reviewer_unverified")
        reviews = len(self.contexts)
        self.execute("Different wording again")
        self.assertEqual(len(self.contexts), reviews)

    def test_uncertain_scope_does_not_authorize_external_continuation(self):
        self.scope = TimeoutError("TEST")
        response = self.execute()
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertFalse(response.choices[0].message.content)
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        control = json.loads(gate.control_tool(args))
        self.assertIn("internal verification only", control["directive"])
        self.assertIn("Do not contact gyms", control["directive"])
        self.assertIn("ask the user for clarification", control["directive"])
        self.assertNotIn("Change method", control["directive"])
        self.assertTrue(control["policy_generated"])
        self.assertFalse(control["external_evidence"])
        self.assertEqual(self.state()["scope"], "uncertain")
        self.assertEqual(self.state()["status"], "verifying_scope")
        self.assertEqual(self.state()["reason"], "scope_uncertain")

    def test_uncertain_scope_internal_rounds_end_at_durable_cap(self):
        for failure in (TimeoutError("TEST"), None, ValueError("TEST")):
            self.scope = failure
            response = self.execute(f"Internal review: {type(failure).__name__}")
            self.assertEqual(response.choices[0].finish_reason, "tool_calls")
            args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            self.assertIn("internal verification only",
                          json.loads(gate.control_tool(args))["directive"])
        self.scope = None
        response = self.execute("The authority remains uncertain.")
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertIn("INCOMPLETE", response.choices[0].message.content)
        self.assertEqual(self.state()["reason"], "retry_cap:scope_uncertain")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"] * 4)
        reviews = len(self.contexts)
        self.execute("Rephrasing cannot restart verification.")
        self.assertEqual(len(self.contexts), reviews)

    def test_uncertain_scope_recovers_only_after_internal_round_and_valid_review(self):
        self.scope = None
        self.evidence(COMPLETE)
        response = self.execute("I think this was authorized.")
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        control = gate.control_tool(args)
        gate.observe_result({**self.kw, "tool_name": bridge.TOOL_NAME}, control)
        self.scope = "active"
        self.outcome = "complete"
        text = "TEST: the latest user requested this survey; all requested evidence supplied."
        response = self.execute(text)
        self.assertEqual(response.choices[0].message.content, text)
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertIsNone(gate.transform(**self.kw, response_text=text))
        self.assertEqual(self.state()["scope"], "active")
        self.assertEqual(self.state()["status"], "complete")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope", "scope", "outcome"])
        context = self.contexts[1][1]
        self.assertEqual(context["current_user"], GOAL)
        self.assertEqual(context["internal_verification"], text)
        self.assertEqual(len(context["tool_results"]), 1)
        self.assertNotIn("policy_generated", context["tool_results"][0]["result_text"])

    def test_uncertain_scope_reviewer_no_stops_internal_verification(self):
        self.scope = None
        response = self.execute()
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        gate.control_tool(args)
        self.scope = "no"
        response = self.execute("This is not an authorized action request.")
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(response.choices[0].message.content, "This is not an authorized action request.")
        self.assertEqual(self.state()["scope"], "skip")
        self.assertIsNone(gate.transform(**self.kw, response_text="Stopped"))
        self.assertEqual([phase for phase, _ in self.contexts], ["scope", "scope"])

    def test_uncertain_scope_cancellation_and_changed_scope_invalidate_tokens(self):
        self.scope = None
        for i, latest in enumerate(("Cancel this survey", "Write a poem", "Do not call gyms")):
            ids = {**self.kw, "turn_id": f"uncertain-cancel-{i}"}
            response = self.execute(turn_id=ids["turn_id"])
            args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            ids["request"] = request(latest, GOAL)
            gate.prepare(**ids)
            directive = json.loads(gate.control_tool(args))["directive"]
            self.assertIn("No matching pending policy decision", directive)
            response = self.execute("Acknowledged.", **ids)
            self.assertEqual(response.choices[0].message.content, "Acknowledged.")
            self.assertIsNone(response.choices[0].message.tool_calls)
            self.assertEqual(self.state(turn_id=ids["turn_id"])["scope"], "skip")

    def test_uncertain_scope_requires_consumed_current_control_token(self):
        self.scope = None
        response = self.execute()
        old_args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        self.scope = "active"
        response = self.execute("I claim authority without executing verification.")
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"])
        self.assertEqual(self.state()["scope"], "uncertain")
        self.assertIn("No matching pending policy decision",
                      json.loads(gate.control_tool(old_args))["directive"])
        self.assertNotIn("scope_verification_ready", self.state())
        self.assertIn("internal verification only", json.loads(gate.control_tool(args))["directive"])
        self.assertIn("No matching pending policy decision",
                      json.loads(gate.control_tool(args))["directive"])

    def test_uncertain_scope_token_is_bound_to_identity_and_scope_revision(self):
        self.scope = None
        response = self.execute()
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        directive = json.loads(gate.control_tool(args, session_id="another", turn_id="turn-1"))["directive"]
        self.assertIn("No matching pending policy decision", directive)
        self.assertNotIn("scope_verification_ready", self.state())
        response = self.execute()
        old_args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        response = self.execute(request=request("Try some other gyms", GOAL))
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        self.assertIn("No matching pending policy decision",
                      json.loads(gate.control_tool(old_args))["directive"])
        self.assertIn("internal verification only", json.loads(gate.control_tool(args))["directive"])

    def test_scope_verification_and_outcome_share_retry_budget(self):
        self.scope = None
        for _ in range(3):
            response = self.execute()
            args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
            gate.control_tool(args)
        self.scope = "active"
        self.outcome = "retry"
        response = self.execute("Authority is now established but evidence is missing.")
        self.assertIn("INCOMPLETE", response.choices[0].message.content)
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(self.state()["reason"], "retry_cap:retry")

    def test_uncertain_scope_without_supported_tool_finishes_unverified(self):
        self.scope = None
        response = self.execute(request=request(tools=False))
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertIn("cannot use a supported continuation bridge", response.choices[0].message.content)
        self.assertEqual(self.state()["scope"], "uncertain")

    def test_oversized_internal_verification_cannot_approve_from_prefix(self):
        self.scope = None
        response = self.execute()
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        gate.control_tool(args)
        self.scope = "active"
        response = self.execute("Permission is established. " * 1000)
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertEqual(self.state()["scope"], "uncertain")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"])

    def test_scope_becoming_truncated_invalidates_pending_verification(self):
        self.scope = None
        response = self.execute()
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        self.scope = "active"
        response = self.execute(request=request(GOAL + " details" * 1200 + " Cancel."))
        self.assertIn("INCOMPLETE", response.choices[0].message.content)
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(self.state()["reason"], "scope_truncated")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"])
        self.assertIn("No matching pending policy decision",
                      json.loads(gate.control_tool(args))["directive"])

    def test_changed_carried_goal_invalidates_verification_for_same_latest_user(self):
        self.scope = None
        latest = "Continue the gym survey"
        response = self.execute(request=request(latest, GOAL))
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        changed = "Check gym hours only; do not contact anyone."
        gate.prepare(**{**self.kw, "request": request(latest, changed)})
        self.assertEqual(self.state()["goal"], changed)
        self.assertIn("No matching pending policy decision",
                      json.loads(gate.control_tool(args))["directive"])
        self.assertNotIn("scope_verification_ready", self.state())

    def test_both_continuation_phases_forward_original_request_to_bridge(self):
        for i, scope in enumerate((None, "active")):
            self.scope = scope
            req = request()
            with patch.object(bridge, "continue_response", wraps=bridge.continue_response) as forward:
                response = self.execute(request=req, turn_id=f"request-forward-{i}")
            self.assertEqual(response.choices[0].finish_reason, "tool_calls")
            self.assertIs(forward.call_args.kwargs["request"], req)

    def test_deferred_or_disabled_tool_is_not_fake_enforcement(self):
        response = self.execute(request=request(tools=False))
        self.assertIn("cannot use a supported continuation bridge", response.choices[0].message.content)
        self.assertEqual(self.state()["reason"], "continuation_tool_unavailable")

    def test_changed_scope_same_turn_cancels_pending_control(self):
        response = self.execute()
        args = json.loads(response.choices[0].message.tool_calls[0].function.arguments)
        gate.prepare(**{**self.kw, "request": request("Cancel this survey", GOAL)})
        self.assertNotIn("Change method", json.loads(gate.control_tool(args))["directive"])
        self.assertIsNone(gate.transform(**self.kw, response_text="Cancelled"))

    def test_scope_change_cannot_reset_durable_retry_budget(self):
        for i in range(3):
            self.execute(request=request(f"Try {i+1} other gyms", GOAL))
        response = self.execute(request=request("Try different gyms", GOAL))
        self.assertIn("INCOMPLETE", response.choices[0].message.content)

    def test_session_turn_isolation(self):
        self.evidence({"fixture": "TEST", "marker": "session-A-only"})
        self.execute()
        self.execute(session_id="other")
        self.assertEqual(self.contexts[-1][1]["tool_results"], [])
        self.assertEqual(self.state(session_id="other")["status"], "continuing")

    def test_responses_current_scope_and_pairing(self):
        req = request("Are u sure? Try some other gyms", GOAL, mode="codex_responses")
        req["input"].insert(1, {"type": "function_call_output", "call_id": "test", "output": "old result"})
        original = json.dumps(req)
        gate.prepare(**{**self.kw, "request": req, "api_mode": "codex_responses"})
        self.assertEqual(self.state()["goal"], GOAL)
        self.assertEqual(json.dumps(req), original)

    def test_native_budget_summary_cannot_claim_unreviewed_success(self):
        self.execute()
        output = gate.transform(**self.kw, response_text="Budget summary: all done!")
        self.assertIn("INCOMPLETE", output)
        self.assertEqual(self.state()["reason"], "unreviewed_terminal_path")

    def test_accepted_async_handoff_does_not_continue_or_poll(self):
        self.evidence({"fixture": "TEST", "child": "accepted", "status": "pending"})
        self.outcome = "pending"
        response = self.execute("Work accepted and still pending; not complete.")
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(self.state()["status"], "pending")

    def test_provider_exception_propagates_without_reviewer_or_retry(self):
        failure = RuntimeError("scripted provider failure")
        def downstream(_):
            raise failure
        with self.assertRaises(RuntimeError) as caught:
            gate.execute(**self.kw, next_call=downstream)
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.contexts, [])

    def test_actual_results_redacted_and_internal_control_excluded(self):
        self.evidence({"API_KEY": "supersecret", "token": "private-token",
                       "pem": "-----BEGIN PRIVATE KEY-----\nSENSITIVE\n-----END PRIVATE KEY-----"})
        gate.observe_result({**self.kw, "tool_name": bridge.TOOL_NAME}, "policy is not evidence")
        self.execute()
        evidence = self.contexts[-1][1]["tool_results"]
        self.assertEqual(len(evidence), 1)
        self.assertNotIn("supersecret", evidence[0]["result_text"])
        self.assertNotIn("private-token", evidence[0]["result_text"])
        self.assertNotIn("SENSITIVE", evidence[0]["result_text"])

    def test_deferred_policy_wrappers_cannot_supply_completion_evidence(self):
        control = json.dumps({"policy_generated": True, "external_evidence": False,
                              "directive": "SYNTHETIC internal control"})
        internal = {"name": bridge.TOOL_NAME, "arguments": {"token": "TEST"}}
        actual = {"name": "test_actual_result", "arguments": {}}
        for calls in ([internal], [actual, internal]):
            gate.observe_result({**self.kw, "tool_name": "tool_call", "args": {"calls": calls}},
                                {"results": [control]})
        gate.observe_result({**self.kw, "tool_name": "tool_call"}, control)
        self.outcome = "complete"
        response = self.execute("The policy output proves completion.")
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertEqual(self.state()["reason"], "reviewer_unverified")
        self.assertEqual(self.contexts[-1][1]["tool_results"], [])

    def test_deferred_actual_tool_result_remains_external_evidence(self):
        gate.observe_result({**self.kw, "tool_name": "tool_call",
                             "args": {"calls": [{"name": "test_actual_result", "arguments": {}}]}},
                            COMPLETE)
        self.outcome = "complete"
        response = self.execute("TEST: all verified deliverables supplied.")
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertEqual(len(self.contexts[-1][1]["tool_results"]), 1)

    def test_control_token_is_one_use_and_not_model_forgeable(self):
        result = self.execute()
        args = json.loads(result.choices[0].message.tool_calls[0].function.arguments)
        first = json.loads(gate.control_tool(args))
        second = json.loads(gate.control_tool(args))
        self.assertIn("Change method", first["directive"])
        self.assertNotIn("Change method", second["directive"])
        self.assertNotIn("Change method", json.loads(gate.control_tool({"token": "invented"}))["directive"])

    def test_oversized_candidate_cannot_approve_from_reviewed_prefix(self):
        self.evidence(COMPLETE)
        self.outcome = "complete"
        response = self.execute("All done. " * 2000)
        self.assertEqual(response.choices[0].finish_reason, "tool_calls")
        self.assertEqual([phase for phase, _ in self.contexts], ["scope"])

    def test_truncated_scope_cannot_authorize_continuation(self):
        response = self.execute(request=request(GOAL + " details" * 1200 + " Cancel."))
        self.assertIsNone(response.choices[0].message.tool_calls)
        self.assertIn("INCOMPLETE", response.choices[0].message.content)
        self.assertEqual(self.contexts, [])

    def test_native_refusal_does_not_trigger_survey_continuation(self):
        refusal = raw("Cannot perform that action.")
        refusal.choices[0].finish_reason = "content_filter"
        response = gate.execute(**self.kw, next_call=lambda _: refusal)
        self.assertIs(response, refusal)
        self.assertIsNone(gate.transform(**self.kw, response_text=refusal.choices[0].message.content))
        self.assertEqual(self.state()["status"], "native_safe_end")

    def test_unsupported_response_is_explicitly_unverified_not_continued(self):
        response = {"unknown_provider_shape": "claiming success"}
        gate.execute(**self.kw, next_call=lambda _: response)
        self.assertEqual(self.state()["reason"], "unsupported_response")
        self.assertIn("INCOMPLETE", gate.transform(**self.kw, response_text="All done"))

    def test_structured_jev_review_validates_outcome_and_reuses_transport(self):
        self.judge.stop()
        state = {"goal": GOAL, "current_user": GOAL, "tool_results": [AUDIT_REGRESSION],
                 "candidate_final": "All done"}
        policy = jev.load_registry()["enforcement"]
        choices = policy["outcome"]["scenarios"][0]["questions"]["result"]["criteria"]
        answer = {"type": "choice", "choice": "retry", "confidence": .99,
                  "probabilities": {name: float(name == "retry") for name in choices}}
        requests = []
        def fetch(body, key):
            requests.append(json.loads(body))
            return {"answers": {"result": answer}}
        with patch.object(jev, "_api_key", return_value="TEST-NOT-A-KEY"), \
                patch.object(jev, "_fetch", side_effect=fetch):
            self.assertEqual(jev.judge_state(state, "outcome"),
                             {"outcome": {"result": ("retry", .99)}})
            self.assertEqual(requests[0]["state"]["tool_results"], [AUDIT_REGRESSION])
            self.assertEqual(requests[0]["model"], "jev-latest")
            answer["confidence"] = .1
            self.assertEqual(jev.judge_state(state, "outcome"), {})
            answer["confidence"] = .99
            answer["probabilities"] = {}
            self.assertEqual(jev.judge_state(state, "outcome"), {})


if __name__ == "__main__":
    unittest.main()
