"""Synthetic policy responses; no live Jev requests or production changes."""
import json

import pytest

from test_router import r, ctx, decision, fixture, pick, state


def policy(required='required', effort='high', rc=.99, ec=.98, **kwargs):
    payload = decision(**kwargs)
    payload['answers']['judge_required'] = pick('judge_required', required, rc)
    payload['answers']['judge_effort'] = pick('judge_effort', effort, ec)
    return payload


def review(payload, **kwargs):
    return r.review_envelope(payload, state(**kwargs), r.DEFAULTS)['review']


def assert_conservative(result):
    assert result['judge_required'] is True
    assert result['verifier_effort'] == 'max'
    assert result['judge_confidence'] == 0


def test_atomic_questions_and_unchanged_five_field_tool():
    assert set(r.QUESTIONS) == {'outcome', 'verdict', 'memory_evidence', 'judge_required', 'judge_effort'}
    assert set(r.QUESTIONS['judge_required']['criteria']) == {'required', 'skip'}
    assert set(r.QUESTIONS['judge_effort']['criteria']) == {'high', 'max'}
    assert set(r.TOOL_SCHEMA['parameters']['properties']) == set(r.STATE_FIELDS)
    assert len(r.STATE_FIELDS) == 5


@pytest.mark.parametrize('effort', ['high', 'max'])
@pytest.mark.parametrize('outcome_confidence', [.4, .93, .99])
def test_required_effort_independent_of_scenario_confidence(effort, outcome_confidence):
    payload = policy(effort=effort)
    payload['answers']['outcome']['confidence'] = outcome_confidence
    envelope = r.review_envelope(payload, state(), r.DEFAULTS)
    result = envelope['review']
    assert result['judge_required'] is True
    assert result['verifier_effort'] == effort
    assert result['judge_confidence'] == .98
    assert envelope['ok'] is (outcome_confidence >= .90)
    assert result['confidence'] == (0 if outcome_confidence < .90 else outcome_confidence)


def test_valid_required_policy_remains_usable_when_scenario_schema_fails():
    payload = policy(effort='high')
    payload['answers'].pop('outcome')
    envelope = r.review_envelope(payload, state(), r.DEFAULTS)
    assert not envelope['ok']
    assert envelope['review']['judge_required'] is True
    assert envelope['review']['verifier_effort'] == 'high'
    assert envelope['review']['judge_confidence'] == .98


@pytest.mark.parametrize('rc,ec', [(.95, .99), (.99, .93), (.9, .9)])
def test_required_policy_confidence_is_minimum_of_policy_dimensions(rc, ec):
    result = review(policy(rc=rc, ec=ec))
    assert result['judge_confidence'] == min(rc, ec)
    assert result['confidence'] == 1  # Scenario confidence is not the policy confidence.


@pytest.mark.parametrize('dimension', ['judge_required', 'judge_effort'])
@pytest.mark.parametrize('mutation', ['missing', 'confidence', 'type', 'label', 'distribution', 'sum', 'not_max'])
def test_invalid_policy_falls_back_without_erasing_valid_scenario(dimension, mutation):
    payload = policy()
    answer = payload['answers'][dimension]
    if mutation == 'missing':
        del payload['answers'][dimension]
    elif mutation == 'confidence':
        answer['confidence'] = float('nan')
    elif mutation == 'type':
        answer['type'] = 'score'
    elif mutation == 'label':
        answer['choice'] = 'xhigh'
    elif mutation == 'distribution':
        answer['probabilities'].pop(answer['choice'])
    elif mutation == 'sum':
        answer['probabilities'] = {k: 1 for k in answer['probabilities']}
    else:
        answer['probabilities'] = {k: .1 if k == answer['choice'] else .9 / (len(answer['probabilities']) - 1) for k in answer['probabilities']}
    result = review(payload)
    assert_conservative(result)
    assert result['disposition'] == 'accept'


@pytest.mark.parametrize('dimension', ['judge_required', 'judge_effort'])
def test_uncertain_required_policy_falls_back_to_max(dimension):
    payload = policy()
    payload['answers'][dimension]['confidence'] = .89
    assert_conservative(review(payload))


@pytest.mark.parametrize('effort', ['medium', 'low', 'none', 'minimal', 'xhigh'])
def test_no_below_high_or_provider_specific_label(effort):
    payload = policy()
    payload['answers']['judge_effort']['choice'] = effort
    assert_conservative(review(payload))


@pytest.mark.parametrize('outcome,extra', [
    ('normal_answer', {}),
    ('brain_dump_added', {'evidence': 'Executed write note-1 + same-target readback note-1'}),
    ('async_handoff', {'pending_background': True, 'evidence': 'Delegation is still pending'}),
])
def test_explicit_high_confidence_skip_for_supported_ready_outcomes(outcome, extra):
    payload = policy(required='skip', outcome=outcome,
                     memory='confirmed' if outcome == 'brain_dump_added' else 'not_applicable')
    result = review(payload, **extra)
    assert result['judge_required'] is False
    assert result['judge_confidence'] == .99
    assert result['verifier_effort'] == 'high'
    assert result['applied'] is False


def test_accept_is_not_automatic_skip():
    assert review(policy())['judge_required'] is True
    payload = decision()
    del payload['answers']['judge_required']
    assert_conservative(review(payload))


def test_skip_does_not_use_irrelevant_effort_confidence():
    result = review(policy(required='skip', ec=.1))
    assert result['judge_required'] is False and result['judge_confidence'] == .99
    payload = policy(required='skip', ec=.1)
    del payload['answers']['judge_effort']
    assert_conservative(review(payload))  # Missing schema is still a failure.


@pytest.mark.parametrize('dimension', ['judge_required', 'outcome', 'verdict'])
def test_skip_requires_point_97_policy_and_evidence_confidence(dimension):
    payload = policy(required='skip')
    payload['answers'][dimension]['confidence'] = .969
    assert_conservative(review(payload))
    payload['answers'][dimension]['confidence'] = .97
    assert review(payload)['judge_required'] is False


@pytest.mark.parametrize('outcome', ['technical_failure', 'brain_dump_failed', 'missing_input', 'safety_refusal', 'ambiguous'])
def test_unsupported_outcomes_never_authorize_skip(outcome):
    assert_conservative(review(policy(required='skip', outcome=outcome)))


def test_unknown_outcome_and_error_never_skip():
    payload = policy(required='skip')
    payload['answers']['outcome']['choice'] = 'invented'
    assert_conservative(review(payload))
    payload = policy(required='skip')
    payload['error'] = 'PRIVATE_RAW_ERROR'
    result = review(payload)
    assert_conservative(result)
    assert 'PRIVATE_RAW_ERROR' not in json.dumps(result)


@pytest.mark.parametrize('extra', [
    {'internal': True}, {'pending_background': True},
    {'evidence': '[evidence digest truncated; not complete verification]'},
    {'evidence': '[evidence unavailable: serialization/redaction failed]'},
])
def test_skip_disallowed_for_internal_pending_normal_or_incomplete_evidence(extra):
    assert_conservative(review(policy(required='skip'), **extra))


@pytest.mark.parametrize('extra', [{}, {'pending_background': True}, {'evidence': 'Pending'}])
def test_handoff_skip_needs_trusted_pending_and_supplied_evidence(extra):
    assert_conservative(review(policy(required='skip', outcome='async_handoff'), **extra))


@pytest.mark.parametrize('memory,evidence,verdict', [
    ('not_confirmed', 'failed write', 'ready'),
    ('confirmed', '', 'ready'),
    ('confirmed', 'write/readback', 'correction_needed'),
])
def test_ack_skip_preserves_memory_gates(memory, evidence, verdict):
    result = review(policy(required='skip', outcome='brain_dump_added', memory=memory, verdict=verdict), evidence=evidence)
    assert_conservative(result)
    assert result['acknowledgment'] == ''


def test_ack_skip_preserves_strict_memory_confidence():
    payload = policy(required='skip', outcome='brain_dump_added', memory='confirmed')
    payload['answers']['memory_evidence']['confidence'] = .96
    envelope = r.review_envelope(payload, state(evidence='write/readback'), r.DEFAULTS)
    assert not envelope['ok']
    assert_conservative(envelope['review'])


def test_configurable_judge_threshold_can_be_tightened_not_weakened():
    config = r.settings(ctx({'judge_threshold': .995}))
    assert_conservative(r.review_envelope(policy(required='skip'), state(), config)['review'])
    for bad in [.96, 0, 1.1, True, '0.97']:
        with pytest.raises(ValueError):
            r.settings(ctx({'judge_threshold': bad}))


@pytest.mark.parametrize('mode', ['shadow', 'active', 'off'])
def test_mode_remains_advice_only_and_off_conservative(monkeypatch, mode):
    reviewer = r.Reviewer(ctx({'mode': mode}))
    calls = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: calls.append(s) or policy(required='skip'))
    result = json.loads(reviewer.handler(state()))
    assert result['mode'] == mode and result['review']['applied'] is False
    assert len(calls) == (0 if mode == 'off' else 1)
    if mode == 'off':
        assert_conservative(result['review'])
    else:
        assert result['review']['judge_required'] is False
    reviewer.close()


def test_capture_marks_dropped_tool_events_incomplete_without_authorizing_skip(monkeypatch):
    reviewer = r.Reviewer(ctx({'max_tool_events': 1}))
    seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or policy(required='skip'))
    reviewer.before(session_id='s', turn_id='t', user_message='Synthetic request')
    for result in ['first result', 'second result']:
        reviewer.tool(session_id='s', turn_id='t', tool_name='terminal', result=result)
    assert reviewer.after(session_id='s', turn_id='t', assistant_response='Synthetic answer') is None
    reviewer.shadow_queue.join()
    assert len(seen) == 1 and '[evidence digest truncated;' in seen[0]['evidence']
    assert_conservative(reviewer.ctx.data['last_decision']['review'])
    reviewer.close()


@pytest.mark.parametrize('expected_policy', [
    {'judge_required': False},
    {'judge_required': 0, 'verifier_effort': 'high'},
    {'judge_required': False, 'verifier_effort': 'low'},
    {'judge_required': False, 'verifier_effort': []},
])
def test_evaluation_rejects_invalid_or_partial_expected_policy(tmp_path, expected_policy):
    row = fixture()
    row['expected'].update(expected_policy)
    path = tmp_path / 'synthetic.jsonl'
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError):
        r.evaluate(path, r.DEFAULTS)


def test_evaluation_policy_expectations_are_independent_and_checked(tmp_path):
    row = fixture()
    row['expected'].update(judge_required=False, verifier_effort='high')
    path = tmp_path / 'synthetic.jsonl'
    path.write_text(json.dumps(row))
    summary, records = r.evaluate(path, r.DEFAULTS)
    assert summary['mismatched'] == 1
    assert records[0]['actual']['judge_required'] is True
    row['decision_response'] = policy(required='skip')
    path.write_text(json.dumps(row))
    summary, records = r.evaluate(path, r.DEFAULTS)
    assert summary['matched'] == summary['judge_skipped'] == 1
    assert records[0]['actual'] == row['expected']
