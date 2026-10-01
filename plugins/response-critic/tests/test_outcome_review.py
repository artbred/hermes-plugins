"""Cooperative outcome tests. All judge/tool responses below are explicit mocks."""
import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

PLUGIN = Path(__file__).resolve().parents[1]
DRAFT = 'I stored and verified the complete note in persistent memory.'
NOTE = 'A note for later: I prefer a quiet room.'
PASS = {'passed': True, 'feedback': ''}
CHALLENGE = {'passed': False, 'feedback': 'Store the complete note and verify readback with tools.'}
EXACT_HANDOFF = ('The outcome-only refactor and tests are running. Shadow mode will observe completed runs '
                 'without changing their replies or actions; not all recovery scenarios are implemented yet. '
                 'Public GitHub publication is waiting for your approval above.')


def envelope(disposition='accept', mode='active', confidence=.99, feedback='',
             judge_required=False, judge_confidence=.99, verifier_effort='max'):
    return {'ok': True, 'mode': mode, 'review': {
        'disposition': disposition, 'scenario': 'brain_dump' if disposition == 'acknowledge' else 'ordinary',
        'confidence': confidence, 'acknowledgment': 'Added.' if disposition == 'acknowledge' else '',
        'feedback': feedback, 'verifier_effort': verifier_effort, 'applied': False,
        'judge_required': judge_required, 'judge_confidence': judge_confidence,
    }, 'answers': {}, 'usage': {}}


@pytest.fixture
def critic(monkeypatch):
    spec = importlib.util.spec_from_file_location('critic_outcome_test', PLUGIN / '__init__.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_ensure_logging', lambda: None)
    monkeypatch.setattr(module, '_conversation_from_frames', lambda: [])
    monkeypatch.setattr(module, '_background_pending', lambda sid: False)
    monkeypatch.setattr(module, '_judge', Mock(return_value=(PASS, 'mock-kimi')))
    return module


def connect(c, result=None, mode='active', critic_mode='active', exists=True):
    ctx = SimpleNamespace(has_plugin=Mock(return_value=exists),
                          dispatch_tool=Mock(return_value=result or envelope()))
    c._plugin_context = ctx
    c._outcome_review_enabled = True
    c._outcome_review_mode = mode
    c._critic_mode = critic_mode
    c.capture_turn_context(session_id='s', turn_id='turn-1', user_message=NOTE)
    return ctx


def memory_history(c, monkeypatch, stored=True, readback=True):
    rows = [{'role': 'user', 'content': NOTE}]
    if stored:
        rows.append({'role': 'tool', 'name': 'mcp__hindsight__sync_retain',
                     'content': json.dumps({'success': True, 'document_id': 'mock-note'})})
    if readback:
        rows.append({'role': 'tool', 'name': 'mcp__hindsight__get_document',
                     'content': json.dumps({'success': True, 'document_id': 'mock-note', 'content': NOTE})})
    monkeypatch.setattr(c, '_conversation_from_frames', lambda: rows)
    return rows


def test_standalone_default_never_dispatches_outcome(critic):
    critic._plugin_context = SimpleNamespace(has_plugin=Mock(), dispatch_tool=Mock())
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_called_once()
    critic._plugin_context.has_plugin.assert_not_called()
    critic._plugin_context.dispatch_tool.assert_not_called()


@pytest.mark.parametrize('wire_json', [False, True])
def test_active_accept_skips_full_judge_and_uses_public_contract(critic, wire_json):
    ctx = connect(critic, json.dumps(envelope()) if wire_json else envelope())
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_not_called()
    ctx.has_plugin.assert_called_once_with('scenario-router')
    name, args = ctx.dispatch_tool.call_args.args
    assert name == 'scenario_review_outcome'
    assert set(args) == {'user_message', 'assistant_response', 'evidence', 'internal', 'pending_background'}
    assert args['user_message'] == NOTE and args['assistant_response'] == DRAFT
    assert args['internal'] is False and args['pending_background'] is False


@pytest.mark.parametrize('critic_mode,local_mode,tool_mode', [
    ('active', 'shadow', 'active'), ('active', 'active', 'shadow'),
    ('active', 'active', 'off'), ('shadow', 'active', 'active'),
])
def test_never_applies_unless_all_modes_active(critic, critic_mode, local_mode, tool_mode):
    ctx = connect(critic, envelope('correct', tool_mode, feedback='Mock outcome correction.'),
                  mode=local_mode, critic_mode=critic_mode)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    if critic_mode == "active" and local_mode == "active":
        ctx.dispatch_tool.assert_called_once()
    else:
        ctx.dispatch_tool.assert_not_called()
    if critic_mode == "active":
        critic._judge.assert_called_once()
    else:
        critic._judge.assert_not_called()
    assert not critic._acknowledgments


@pytest.mark.parametrize('disposition', ['accept', 'correct', 'acknowledge', 'recover'])
def test_shadow_keeps_existing_kimi_challenge_identical(critic, disposition):
    connect(critic, envelope(disposition, feedback='Mock alternate advice.'), mode='shadow')
    critic._judge.return_value = (CHALLENGE, 'mock-kimi')
    result = critic.validate_final_response(DRAFT, session_id='s')
    assert result == {'action': 'continue', 'message': CHALLENGE['feedback'] + critic.NO_TRACE_SUFFIX}
    critic._judge.assert_called_once()
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') is None


@pytest.mark.parametrize('result', [
    'not JSON', {}, {'ok': False}, {'ok': True, 'mode': 'active'},
    envelope(confidence=.96), envelope('uncertain'), envelope('refusal'),
    {**envelope(), 'mode': 'not-a-mode'},
    {**envelope(), 'review': {**envelope()['review'], 'confidence': True}},
    {**envelope(), 'review': {**envelope()['review'], 'confidence': float('nan')}},
    {**envelope(), 'review': {**envelope()['review'], 'confidence': float('inf')}},
    {**envelope(), 'review': {**envelope()['review'], 'applied': True}},
    {**envelope(), 'review': {**envelope()['review'], 'verifier_effort': 'low'}},
    {**envelope(), 'review': {**envelope()['review'], 'disposition': 'execute-shell'}},
])
def test_uncertain_or_invalid_result_uses_full_max_judge(critic, result):
    ctx = connect(critic)
    ctx.dispatch_tool.return_value = result
    assert critic.validate_final_response('Added.', session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'
    assert not critic._acknowledgments


@pytest.mark.parametrize('failure', ['missing', 'exception', 'bad-schema'])
def test_missing_plugin_tool_error_schema_falls_back(critic, failure):
    ctx = connect(critic, exists=failure != 'missing')
    if failure == 'exception':
        ctx.dispatch_tool.side_effect = RuntimeError('Mock dispatch error')
    if failure == 'bad-schema':
        ctx.dispatch_tool.return_value = envelope('correct')  # empty feedback is not actionable
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_called_once()
    if failure == 'missing':
        ctx.dispatch_tool.assert_not_called()


def test_active_correction_returns_original_agent_continuation(critic):
    connect(critic, envelope('correct', feedback=CHALLENGE['feedback']))
    result = critic.validate_final_response(DRAFT, session_id='s')
    assert result['action'] == 'continue'
    assert 'verify readback with tools' in result['message']
    critic._judge.assert_not_called()


def test_recovery_warns_against_repeating_side_effects_or_refusal_bypass(critic):
    connect(critic, envelope('recover', feedback='Inspect the failed publication request.'))
    result = critic.validate_final_response('The API rejected the publication.', session_id='s')
    assert result['action'] == 'continue'
    assert 'Do not automatically repeat an already executed external action' in result['message']
    assert 'authorization' in result['message']
    assert 'Do not bypass a safety refusal' in result['message']
    assert 'does not change the provider or model' in result['message']
    critic._judge.assert_not_called()


def test_outcome_continuations_share_existing_iteration_cap(critic):
    ctx = connect(critic, envelope('recover', feedback='Inspect the failure.'))
    critic._max_iterations = 2
    for attempt in (0, 1):
        assert critic.validate_final_response(DRAFT, session_id='s', attempt=attempt)['action'] == 'continue'
    assert critic.validate_final_response(DRAFT, session_id='s', attempt=2) is None
    assert ctx.dispatch_tool.call_count == 2
    assert len(critic._turn_feedback.get('s', [])) == 0


@pytest.mark.parametrize('pending', [True, False])
def test_exact_production_refactor_tests_handoff_needs_real_runtime(critic, monkeypatch, pending):
    monkeypatch.setattr(critic, '_background_pending', lambda sid: pending and sid == 's')
    critic._judge.return_value = (CHALLENGE, 'mock-kimi')
    result = critic.validate_final_response(EXACT_HANDOFF, session_id='s')
    if pending:
        assert result is None
        critic._judge.assert_not_called()
        critic.record_completed_answer(session_id='s', assistant_response=EXACT_HANDOFF)
        assert 's' not in critic._last_completed
    else:
        assert result['action'] == 'continue'
        critic._judge.assert_called_once()


@pytest.mark.parametrize('pending', [True, False])
def test_jev_handoff_also_requires_runtime_pending(critic, monkeypatch, pending):
    connect(critic, envelope('handoff'))
    monkeypatch.setattr(critic, '_background_pending', lambda sid: pending)
    assert critic.validate_final_response('The queued investigation is proceeding.', session_id='s') is None
    assert critic._judge.call_count == (0 if pending else 1)


@pytest.mark.parametrize('post_before_transform', [True, False])
def test_added_only_after_agent_memory_write_and_readback_and_both_hook_orders(critic, monkeypatch, post_before_transform):
    connect(critic, envelope('acknowledge'))
    memory_history(critic, monkeypatch)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_not_called()
    if post_before_transform:
        critic.record_completed_answer(session_id='s', assistant_response=DRAFT)
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') == 'Added.'
    if not post_before_transform:
        critic.record_completed_answer(session_id='s', assistant_response='Added.')
    assert critic._last_completed['s'] == 'Added.'
    assert not critic._acknowledgments and not critic._turn_context


@pytest.mark.parametrize('stored,readback', [(False, False), (True, False), (False, True)])
def test_added_not_allowed_without_confirmed_memory(critic, monkeypatch, stored, readback):
    connect(critic, envelope('acknowledge'))
    memory_history(critic, monkeypatch, stored, readback)
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') is None


def test_failed_brain_dump_continues_store_verify_not_added(critic, monkeypatch):
    connect(critic, envelope('correct', feedback=CHALLENGE['feedback']))
    memory_history(critic, monkeypatch, stored=False, readback=False)
    result = critic.validate_final_response('Storage failed.', session_id='s')
    assert result['action'] == 'continue'
    assert 'Store the complete note and verify readback' in result['message']
    assert critic.suppress_exact_internal_duplicate('Storage failed.', session_id='s', turn_id='turn-1') is None


def test_shadow_never_adds_even_after_verified_memory(critic, monkeypatch):
    connect(critic, envelope('acknowledge'), mode='shadow')
    memory_history(critic, monkeypatch)
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') is None


def test_ack_does_not_leak_to_followup_or_different_draft(critic, monkeypatch):
    connect(critic, envelope('acknowledge'))
    memory_history(critic, monkeypatch)
    critic.validate_final_response(DRAFT, session_id='s')
    assert critic.suppress_exact_internal_duplicate('A materially changed answer.', session_id='s', turn_id='turn-1') is None
    critic.capture_turn_context(session_id='s', turn_id='turn-2', user_message='Repeat your answer.')
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-2') is None


def test_wrong_turn_transform_cannot_consume_current_ack(critic, monkeypatch):
    connect(critic, envelope('acknowledge'))
    memory_history(critic, monkeypatch)
    critic.validate_final_response(DRAFT, session_id='s')
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='old-turn') is None
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') == 'Added.'


def test_current_metadata_trusted_without_gateway_bridge_and_post_first(critic):
    critic.record_completed_answer(session_id='s', assistant_response=DRAFT)
    notice = {'role': 'user', 'content': '[INTERNAL NOTIFICATION] mock completion',
              'display_kind': 'internal_notification'}
    result = critic.capture_turn_context(session_id='s', turn_id='notice', user_message=notice['content'],
                                         conversation_history=[notice])
    assert result and critic._turn_context['s']['internal']
    critic.record_completed_answer(session_id='s', assistant_response=DRAFT)
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='notice') == 'NO_REPLY'
    critic.capture_turn_context(session_id='s', turn_id='human', user_message='Please repeat that.',
                                conversation_history=[notice, {'role': 'user', 'content': 'Please repeat that.'}])
    critic.record_completed_answer(session_id='s', assistant_response=DRAFT)
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='human') is None


def test_old_internal_metadata_does_not_classify_current_human_quotation(critic):
    notice = '[ASYNC DELEGATION BATCH COMPLETE] mock'
    rows = [{'role': 'user', 'content': notice, 'display_kind': 'internal_notification'},
            {'role': 'user', 'content': 'Why this? ' + notice}]
    assert critic.capture_turn_context(session_id='s', user_message=rows[-1]['content'], conversation_history=rows) is None
    assert not critic._turn_context['s']['internal']


def test_evidence_and_outcome_request_use_latest_human_not_notice_or_nudge(critic, monkeypatch):
    ctx = connect(critic)
    rows = [{'role': 'user', 'content': NOTE},
            {'role': 'user', 'content': 'Historical machine notice', 'display_kind': 'internal_notification'},
            {'role': 'user', 'content': '[BACKGROUND PROCESS] This is a human question, not a machine.'},
            {'role': 'user', 'content': 'Synthetic correction', '_pre_verify_hook_continue': True}]
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: rows)
    critic.validate_final_response(DRAFT, session_id='s')
    args = ctx.dispatch_tool.call_args.args[1]
    assert args['user_message'] == rows[2]['content']
    assert 'Historical machine notice' not in args['evidence']
    assert 'Synthetic correction' not in args['evidence']


def test_old_tools_do_not_authorize_added_or_enter_current_evidence(critic, monkeypatch):
    rows = [{'role': 'user', 'content': 'An earlier note.'},
            {'role': 'tool', 'name': 'memory', 'content': '{"success":true,"verified":true}'},
            {'role': 'assistant', 'content': 'Added.'}, {'role': 'user', 'content': NOTE}]
    ctx = connect(critic, envelope('acknowledge'))
    critic.capture_turn_context(session_id='s', turn_id='turn-1', user_message=NOTE, conversation_history=rows)
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: rows)
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert 'verified' not in ctx.dispatch_tool.call_args.args[1]['evidence']
    assert not critic._acknowledgments


def test_redaction_before_truncation_for_judge_and_outcome(critic, monkeypatch):
    secret = 'mockOpaqueBearerCredentialValue12345'
    ctx = connect(critic, envelope('uncertain'))
    rows = [{'role': 'user', 'content': 'Verify this token: Bearer ' + secret},
            {'role': 'tool', 'name': 'terminal', 'content': 'Authorization: Bearer ' + secret}]
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: rows)
    critic.validate_final_response('Verified credential: Bearer ' + secret, session_id='s')
    outcome_args = ctx.dispatch_tool.call_args.args[1]
    assert secret not in json.dumps(outcome_args)
    assert secret not in critic._judge.call_args.args[0]
    assert secret not in critic._evidence_appendix(session_id='s', per_item_chars=30)


def test_redaction_failure_never_returns_raw_content(critic, monkeypatch):
    import agent.redact
    monkeypatch.setattr(agent.redact, 'redact_for_egress', Mock(side_effect=RuntimeError('mock failure')))
    assert 'raw confidential mock text' not in critic._redact_for_review('raw confidential mock text')


def test_feedback_and_provider_error_bodies_not_logged(critic, monkeypatch, caplog):
    marker = 'PRIVATE_MOCK_PROVIDER_BODY'
    monkeypatch.setenv('KIMI_API_KEY', 'mock-key')
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post.return_value = httpx.Response(429, text=marker)
    monkeypatch.setattr(critic.httpx, 'Client', lambda **kw: client)
    with caplog.at_level('INFO', logger=critic.logger.name):
        critic._judge_kimi('mock draft', 'max', 'mock system')
        monkeypatch.setenv('OPENROUTER_API_KEY', 'mock-key')
        critic._judge_openrouter('mock draft', 'max', 'mock system')
        critic._judge.return_value = ({'passed': False, 'feedback': marker}, 'mock-kimi')
        critic.validate_final_response(DRAFT, session_id='s')
    assert marker not in caplog.text
    assert 'HTTP 429' in caplog.text


def test_public_manager_cooperative_dispatch_in_isolated_scope(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    critic_dir = home / 'plugins' / 'response-critic'
    router_dir = home / 'plugins' / 'scenario-router'
    critic_dir.mkdir(parents=True)
    router_dir.mkdir(parents=True)
    for name in ('__init__.py', 'plugin.yaml'):
        shutil.copy2(PLUGIN / name, critic_dir / name)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [response-critic, scenario-router]\n  entries:\n    response-critic:\n      settings:\n        outcome_review_enabled: true\n        outcome_review_mode: active\n')
    (router_dir / 'plugin.yaml').write_text('name: scenario-router\nversion: 0.0.0\nkind: standalone\n')
    # A test-only mock plugin, not a fabricated live classifier response.
    (router_dir / '__init__.py').write_text(
        'import json\nSEEN = []\nMOCK = ' + repr(envelope()) + '\n'
        'def register(ctx):\n'
        '    def handler(args, **kw):\n'
        '        SEEN.append(args)\n'
        '        return json.dumps(MOCK)\n'
        '    ctx.register_tool(name="scenario_review_outcome", toolset="scenario-router", '
        'schema={"name":"scenario_review_outcome","description":"Test-only mock outcome",'
        '"parameters":{"type":"object","properties":{}}}, handler=handler)\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    from hermes_cli.plugins import PluginManager
    manager = PluginManager(scope_key=str(home))
    manager.discover_and_load()
    loaded = manager._plugins['response-critic'].module
    monkeypatch.setattr(loaded, '_judge', Mock(side_effect=AssertionError('Active accept must skip full judge')))
    manager.invoke_hook('pre_llm_call', session_id='s', turn_id='turn-real', user_message=NOTE,
                        conversation_history=[{'role': 'user', 'content': NOTE}])
    assert all(result is None for result in manager.invoke_hook('pre_verify', final_response=DRAFT, session_id='s'))
    seen = manager._plugins['scenario-router'].module.SEEN
    assert len(seen) == 1 and seen[0]['user_message'] == NOTE
    loaded._judge.assert_not_called()


def test_current_human_not_mistaken_for_prior_only_internal_history(critic):
    old = {'role': 'user', 'content': 'Old machine completion.', 'display_kind': 'internal_notification'}
    assert critic.capture_turn_context(session_id='s', user_message='A genuine new follow-up.',
                                       conversation_history=[old]) is None
    assert not critic._turn_context['s']['internal']


def test_prior_only_history_cannot_authorize_current_ack(critic, monkeypatch):
    connect(critic, envelope('acknowledge'))
    rows = [{'role': 'user', 'content': 'Different older note.'},
            {'role': 'tool', 'name': 'memory', 'content': '{"success":true,"verified":true}'}]
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: rows)
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert not critic._acknowledgments


@pytest.mark.parametrize('readback', [
    {'success': False, 'error': 'Mock readback failure'},
    {'success': True, 'document_id': 'different-mock-note', 'content': 'Another note'},
    {'success': True, 'staged': True, 'document_id': 'mock-note', 'content': NOTE},
])
def test_failed_staged_or_different_target_readback_never_acknowledged(critic, monkeypatch, readback):
    connect(critic, envelope('acknowledge'))
    rows = memory_history(critic, monkeypatch)
    rows[-1]['content'] = json.dumps(readback)
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert not critic._acknowledgments


def test_failed_later_memory_action_revokes_confirmation(critic, monkeypatch):
    connect(critic, envelope('acknowledge'))
    rows = memory_history(critic, monkeypatch)
    rows.append({'role': 'tool', 'name': 'mcp__hindsight__sync_retain',
                 'content': '{"success":false,"error":"Mock subsequent write failure"}'})
    critic.validate_final_response(DRAFT, session_id='s')
    critic._judge.assert_called_once()
    assert not critic._acknowledgments


def test_explicit_verified_memory_result_and_call_id_resolution(critic, monkeypatch):
    connect(critic, envelope('acknowledge'))
    rows = [{'role': 'user', 'content': NOTE},
            {'role': 'assistant', 'tool_calls': [
                {'id': 'mock-call', 'function': {'name': 'memory', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'mock-call', 'content': '{"success":true,"verified":true}'}]
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: rows)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_not_called()
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') == 'Added.'


def test_off_mode_never_reviews_transforms_or_opens_private_gate(critic, monkeypatch):
    ctx = connect(critic, critic_mode='off')
    agent = SimpleNamespace(_turn_file_mutation_paths=set())
    monkeypatch.setattr(critic, '_get_agent', lambda: agent)
    critic._open_verify_gate()
    assert agent._turn_file_mutation_paths == set()
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    ctx.dispatch_tool.assert_not_called()
    critic._judge.assert_not_called()
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') is None


def test_private_gate_compatibility_sentinel_still_only_mutates_memory(critic, monkeypatch):
    agent = SimpleNamespace(_turn_file_mutation_paths={'existing.py'})
    monkeypatch.setattr(critic, '_get_agent', lambda: agent)
    critic._open_verify_gate()
    assert agent._turn_file_mutation_paths == {'existing.py', critic.SENTINEL}
    from agent.verification_stop import build_verify_on_stop_nudge
    assert build_verify_on_stop_nudge(session_id='s', changed_paths=[critic.SENTINEL]) is None


def test_state_is_bounded_and_capture_never_dispatches_jev(critic):
    ctx = connect(critic)
    for index in range(300):
        sid = 'mock-session-' + str(index)
        critic.capture_turn_context(session_id=sid, turn_id=sid, user_message=NOTE)
    assert len(critic._turn_context) <= 256
    ctx.dispatch_tool.assert_not_called()


@pytest.mark.parametrize('effort', ['medium', 'high', 'max'])
@pytest.mark.parametrize('disposition,confidence', [
    ('accept', .99), ('accept', .25), ('uncertain', .99), ('uncertain', .25),
    ('refusal', .99), ('handoff', .99), ('acknowledge', .99),
])
def test_required_judge_uses_policy_confidence_not_disposition_confidence(
        critic, monkeypatch, effort, disposition, confidence):
    connect(critic, envelope(disposition, confidence=confidence, judge_required=True,
                             judge_confidence=.97, verifier_effort=effort))
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    memory_history(critic, monkeypatch)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == effort
    assert not critic._acknowledgments
    assert critic.suppress_exact_internal_duplicate(DRAFT, session_id='s', turn_id='turn-1') is None


@pytest.mark.parametrize('effort', ['medium', 'high', 'max'])
@pytest.mark.parametrize('draft', ['', 'Added.', DRAFT, EXACT_HANDOFF])
def test_required_judge_cannot_be_bypassed_by_short_trivial_or_background_handoff(
        critic, monkeypatch, effort, draft):
    ctx = connect(critic, envelope('handoff', judge_required=True, verifier_effort=effort))
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    monkeypatch.setattr(critic, '_looks_trivial', lambda text: True)
    assert critic.validate_final_response(draft, session_id='s') is None
    ctx.dispatch_tool.assert_called_once()
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == effort


@pytest.mark.parametrize('judge_required', [True, False])
@pytest.mark.parametrize('confidence', [0, .96, .969999])
@pytest.mark.parametrize('effort', ['medium', 'max'])
def test_low_policy_confidence_uses_max_judge(critic, judge_required, confidence, effort):
    connect(critic, envelope(judge_required=judge_required, judge_confidence=confidence,
                             verifier_effort=effort))
    assert critic.validate_final_response('Added.', session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


@pytest.mark.parametrize('field,value', [
    ('judge_required', None), ('judge_required', 0), ('judge_required', 1),
    ('judge_required', 'false'), ('judge_required', 'true'), ('judge_required', []),
    ('judge_confidence', None), ('judge_confidence', True), ('judge_confidence', False),
    ('judge_confidence', '.99'), ('judge_confidence', -.01), ('judge_confidence', 1.01),
    ('judge_confidence', float('nan')), ('judge_confidence', float('inf')),
    ('judge_confidence', float('-inf')), ('judge_confidence', {}),
    ('verifier_effort', 'low'), ('verifier_effort', 'minimal'), ('verifier_effort', 'xhigh'),
    ('verifier_effort', 'unknown'), ('verifier_effort', ''), ('verifier_effort', None),
    ('verifier_effort', False), ('verifier_effort', []),
])
def test_invalid_policy_abstains_to_max_judge(critic, field, value):
    result = envelope()
    result['review'][field] = value
    connect(critic, result)
    assert critic.validate_final_response('Added.', session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


@pytest.mark.parametrize('field', ['judge_required', 'judge_confidence', 'verifier_effort'])
def test_missing_policy_field_abstains_to_max_judge(critic, field):
    result = envelope()
    result['review'].pop(field)
    connect(critic, result)
    assert critic.validate_final_response('Added.', session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


@pytest.mark.parametrize('disposition', ['accept', 'acknowledge', 'handoff'])
@pytest.mark.parametrize('effort', ['medium', 'high', 'max'])
def test_explicit_trusted_optional_judge_skips_only_supported_outcomes(
        critic, monkeypatch, disposition, effort):
    connect(critic, envelope(disposition, confidence=.97, judge_confidence=.97,
                             verifier_effort=effort))
    memory_history(critic, monkeypatch)
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_not_called()
    assert bool(critic._acknowledgments) == (disposition == 'acknowledge')


@pytest.mark.parametrize('disposition', ['accept', 'acknowledge', 'handoff', 'uncertain', 'refusal'])
def test_optional_judge_does_not_skip_uncertain_outcome(critic, monkeypatch, disposition):
    connect(critic, envelope(disposition, confidence=.96, verifier_effort='medium'))
    memory_history(critic, monkeypatch)
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    assert critic.validate_final_response(EXACT_HANDOFF, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'
    assert not critic._acknowledgments


@pytest.mark.parametrize('disposition', ['uncertain', 'refusal'])
def test_optional_judge_does_not_skip_unsupported_disposition(critic, monkeypatch, disposition):
    connect(critic, envelope(disposition, verifier_effort='medium'))
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    assert critic.validate_final_response(EXACT_HANDOFF, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


@pytest.mark.parametrize('disposition', ['acknowledge', 'handoff'])
def test_rejected_skip_gate_uses_max_not_policy_effort(critic, disposition):
    connect(critic, envelope(disposition, verifier_effort='medium'))
    assert critic.validate_final_response('Added.', session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'
    assert not critic._acknowledgments


@pytest.mark.parametrize('mode', ['shadow', 'off'])
def test_inactive_remote_mode_cannot_supply_policy_effort(critic, monkeypatch, mode):
    connect(critic, envelope('handoff', mode=mode, judge_required=True, verifier_effort='medium'))
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    assert critic.validate_final_response(EXACT_HANDOFF, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


@pytest.mark.parametrize('disposition', ['correct', 'recover'])
@pytest.mark.parametrize('policy', ['required', 'low-confidence', 'missing', 'invalid'])
@pytest.mark.parametrize('passed', [True, False])
def test_high_confidence_recovery_cannot_bypass_required_or_unknown_judge(critic, disposition, policy, passed):
    result = envelope(disposition, confidence=.97, feedback=CHALLENGE['feedback'],
                      judge_required=True, verifier_effort='medium')
    if policy == 'low-confidence':
        result['review']['judge_confidence'] = .2
    elif policy == 'missing':
        result['review'].pop('judge_required')
        result['review'].pop('judge_confidence')
    elif policy == 'invalid':
        result['review']['judge_required'] = 'true'
        result['review']['judge_confidence'] = True
    connect(critic, result)
    critic._judge.return_value = (PASS if passed else CHALLENGE, 'mock-kimi')
    continuation = critic.validate_final_response(DRAFT, session_id='s')
    if passed:
        assert continuation is None
    else:
        assert continuation['action'] == 'continue'
        assert CHALLENGE['feedback'] in continuation['message']
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == ('medium' if policy == 'required' else 'max')


@pytest.mark.parametrize('disposition', ['correct', 'recover'])
def test_low_outcome_confidence_does_not_continue_but_keeps_required_effort(critic, disposition):
    connect(critic, envelope(disposition, confidence=.96, feedback=CHALLENGE['feedback'],
                             judge_required=True, verifier_effort='high'))
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'high'


@pytest.mark.parametrize('policy', ['low-confidence', 'missing', 'invalid', 'unavailable'])
def test_untrusted_policy_cannot_fall_back_to_legacy_background_skip(critic, monkeypatch, policy):
    result = envelope('handoff', verifier_effort='medium')
    if policy == 'low-confidence':
        result['review']['judge_confidence'] = .96
    elif policy == 'missing':
        result['review'].pop('judge_required')
    elif policy == 'invalid':
        result['review']['judge_required'] = 'false'
    ctx = connect(critic, result)
    if policy == 'unavailable':
        ctx.dispatch_tool.return_value = {'ok': False}
    monkeypatch.setattr(critic, '_background_pending', lambda sid: True)
    monkeypatch.setattr(critic, '_looks_trivial', lambda text: True)
    assert critic.validate_final_response(EXACT_HANDOFF, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'


def test_optional_acknowledgment_requires_human_turn_even_with_verified_memory(critic, monkeypatch):
    connect(critic, envelope('acknowledge', verifier_effort='medium'))
    critic._turn_context['s']['internal'] = True
    memory_history(critic, monkeypatch)
    assert critic.validate_final_response(DRAFT, session_id='s') is None
    critic._judge.assert_called_once()
    assert critic._judge.call_args.args[1] == 'max'
    assert not critic._acknowledgments


@pytest.mark.parametrize('disposition', ['accept', 'correct', 'recover'])
def test_optional_judge_policy_still_respects_single_correction_cap(critic, disposition):
    ctx = connect(critic, envelope(disposition, feedback=CHALLENGE['feedback'],
                                  judge_required=True, verifier_effort='medium'))
    critic._max_iterations = 1
    assert critic.validate_final_response(DRAFT, session_id='s', attempt=1) is None
    ctx.dispatch_tool.assert_not_called()
    critic._judge.assert_not_called()
