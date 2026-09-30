"""Synthetic test Decisions are explicitly mocks, never claimed live API results."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

P = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('scenario_reviewer_unit', P / '__init__.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def pick(name, label, confidence=1):
    return {'type': 'choice', 'choice': label, 'confidence': confidence,
            'probabilities': {k: int(k == label) for k in r.QUESTIONS[name]['criteria']}}


def decision(outcome='normal_answer', verdict='ready', memory='not_applicable', confidence=1):
    return {'answers': {name: pick(name, label, confidence) for name, label in
                        [('outcome', outcome), ('verdict', verdict), ('memory_evidence', memory)]}}


def state(**kw):
    return {'user_message': 'Synthetic request', 'assistant_response': 'Synthetic answer',
            'evidence': '', 'internal': False, 'pending_background': False, **kw}


def ctx(cfg=None):
    data = {}
    obj = SimpleNamespace(get_config=lambda k, default: (cfg or {}).get(k, default),
                          state=SimpleNamespace(set=lambda k, v: data.__setitem__(k, v)),
                          data=data, hooks={}, tools={})
    obj.register_hook = lambda name, fn: obj.hooks.__setitem__(name, fn)
    obj.register_tool = lambda **kw: obj.tools.__setitem__(kw['name'], kw)
    return obj


def reviewed(outcome, verdict='ready', memory='not_applicable', **kw):
    return r.review_envelope(decision(outcome, verdict, memory), state(**kw), r.DEFAULTS)


@pytest.mark.parametrize('outcome,disposition', [
    ('normal_answer', 'accept'), ('brain_dump_failed', 'recover'),
    ('technical_failure', 'recover'), ('missing_input', 'correct'),
    ('safety_refusal', 'refusal'), ('async_handoff', 'handoff'), ('ambiguous', 'uncertain')])
def test_outcome_mapping(outcome, disposition):
    result = reviewed(outcome)
    assert result['ok']
    assert result['mode'] == 'shadow'
    assert result['review']['disposition'] == disposition
    assert result['review']['applied'] is False
    assert result['review']['verifier_effort'] == 'max'
    assert result['review']['acknowledgment'] == ''


def test_note_success_requires_confirmed_evidence_and_strict_confidence():
    result = reviewed('brain_dump_added', memory='confirmed', evidence='Actual memory write + matching document readback')
    assert result['review']['disposition'] == 'acknowledge'
    assert result['review']['acknowledgment'] == 'Added.'


@pytest.mark.parametrize('memory,evidence', [('not_confirmed', 'failed write'), ('not_applicable', 'irrelevant'), ('confirmed', ''), ('confirmed', '  ')])
def test_note_never_acknowledged_without_supplied_confirmation(memory, evidence):
    result = reviewed('brain_dump_added', memory=memory, evidence=evidence)
    assert result['review']['disposition'] == 'correct'
    assert result['review']['acknowledgment'] == ''


def test_high_threshold_for_all_memory_decisions():
    for name in r.QUESTIONS:
        payload = decision('brain_dump_added', memory='confirmed')
        payload['answers'][name]['confidence'] = .96
        result = r.review_envelope(payload, state(evidence='write/readback'), r.DEFAULTS)
        assert not result['ok'] and result['review']['disposition'] == 'uncertain'
        assert result['review']['acknowledgment'] == ''


def test_verbose_note_can_be_acknowledged_only_after_agent_run():
    result = reviewed('brain_dump_added', memory='confirmed', evidence='write/readback', assistant_response='A long generated essay about these notes.')
    assert result['review']['disposition'] == 'acknowledge'
    assert result['review']['acknowledgment'] == 'Added.'


def test_mixed_note_question_is_normal_answer():
    result = reviewed('normal_answer', user_message='My note: avoid context switching. How can I organize work?')
    assert result['review']['disposition'] == 'accept'
    assert result['review']['acknowledgment'] == ''


def test_unsupported_success_requires_correction():
    assert reviewed('normal_answer', 'correction_needed')['review']['disposition'] == 'correct'
    assert reviewed('brain_dump_added', 'correction_needed', 'confirmed', evidence='write/readback')['review']['disposition'] == 'correct'


@pytest.mark.parametrize('outcome', ['normal_answer', 'brain_dump_added', 'technical_failure'])
def test_pending_never_claims_final(outcome):
    result = reviewed(outcome, memory='confirmed', evidence='write/readback', pending_background=True)
    assert result['review']['disposition'] == 'handoff'
    assert result['review']['acknowledgment'] == ''


def test_internal_note_must_not_be_shortened():
    assert reviewed('brain_dump_added', memory='confirmed', evidence='write/readback', internal=True)['review']['disposition'] == 'uncertain'


@pytest.mark.parametrize('verdict', ['ready', 'correction_needed'])
def test_safety_refusal_no_bypass_even_pending(verdict):
    result = reviewed('safety_refusal', verdict, pending_background=True)
    assert result['review']['disposition'] == 'refusal'
    assert 'safe alternative' in result['review']['feedback']
    assert 'another model' in result['review']['feedback']
    assert 'fallback_model' not in json.dumps(result)


def test_technical_recovery_no_model_switch():
    result = reviewed('technical_failure')
    assert 'authorized tool/backend alternative' in result['review']['feedback']
    assert 'switch the main model automatically' in result['review']['feedback']


@pytest.mark.parametrize('bad', [None, True, '1', float('nan'), float('inf'), -1, 2, {}, []])
def test_invalid_confidence_abstains(bad):
    payload = decision()
    payload['answers']['outcome']['confidence'] = bad
    result = r.review_envelope(payload, state(), r.DEFAULTS)
    assert not result['ok'] and result['review']['disposition'] == 'uncertain'
    assert 'outcome' not in result['answers']


@pytest.mark.parametrize('mutation', ['missing_distribution', 'bad_sum', 'not_maximum', 'invalid_label', 'missing_question', 'wrong_type', 'list_label', 'low_confidence'])
def test_invalid_or_uncertain_decisions(mutation):
    payload = decision()
    a = payload['answers']['outcome']
    if mutation == 'missing_distribution':
        a.pop('probabilities')
    elif mutation == 'bad_sum':
        a['probabilities']['technical_failure'] = 1
    elif mutation == 'not_maximum':
        a['probabilities']['normal_answer'] = .1
        a['probabilities']['technical_failure'] = .9
    elif mutation == 'invalid_label':
        a['choice'] = 'unknown'
    elif mutation == 'missing_question':
        payload['answers'].pop('verdict')
    elif mutation == 'wrong_type':
        a['type'] = 'noul'
    elif mutation == 'list_label':
        a['choice'] = []
    else:
        a['confidence'] = .89
    result = r.review_envelope(payload, state(), r.DEFAULTS)
    assert not result['ok'] and result['review']['disposition'] == 'uncertain'


@pytest.mark.parametrize('payload', [None, [], {}, {'answers': []}, {'error': 'SECRET', 'answers': {}}])
def test_bad_payload_no_crash(payload):
    assert r.review_envelope(payload, state(), r.DEFAULTS)['review']['disposition'] == 'uncertain'


def test_only_validated_answers_and_numeric_usage_returned():
    payload = decision()
    payload['answers']['outcome']['prose'] = 'PRIVATE'
    payload['answers']['injected'] = 'PRIVATE'
    payload['usage'] = {'input_tokens': 123, 'output_tokens': 0, 'cost': .01, 'secret': 'PRIVATE'}
    result = r.review_envelope(payload, state(), r.DEFAULTS)
    assert 'PRIVATE' not in json.dumps(result)
    assert result['usage'] == {'input_tokens': 123, 'output_tokens': 0, 'cost': .01}
    for bad in [True, -1, float('inf'), float('nan'), '1']:
        assert r.numeric_usage({'usage': {'cost': bad}}) == {}


@pytest.mark.parametrize('bad', [{'mode': 'enforce'}, {'mode': 'live'}, {'brain_dump_threshold': .8}, {'confidence_threshold': 0}, {'timeout_seconds': True}, {'max_tool_events': 65}, {'judge_model': ''}])
def test_invalid_settings_rejected(bad):
    with pytest.raises(ValueError):
        r.Reviewer(ctx(bad))


def test_capture_no_api_or_policy(monkeypatch):
    c = ctx(); reviewer = r.Reviewer(c)
    monkeypatch.setattr(reviewer.client, 'decide', lambda *a: pytest.fail('pre-run API is forbidden'))
    for _ in range(2):
        assert reviewer.before(session_id='s', turn_id='t', user_message='note') is None
    assert len(reviewer.turns) == 1
    assert c.data == {}


def test_shadow_all_flows_and_redacted_actual_evidence(monkeypatch, caplog):
    c = ctx(); reviewer = r.Reviewer(c); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    caplog.set_level('INFO', logger='scenario-router')
    reviewer.before(session_id='PRIVATE_SESSION', turn_id='t', user_message='PRIVATE_NOTE',
                    conversation_history=[{'role': 'user', 'display_kind': 'internal_notification'}], parent_session_id='parent')
    reviewer.tool(session_id='PRIVATE_SESSION', turn_id='t', tool_name='memory', status='ok',
                  args={'note': 'PRIVATE_NOTE'}, result={'api_key': 'sk-abcdefghijklmnopqrstuvwxyz123456', 'document': 'PRIVATE_READBACK'})
    assert reviewer.after(session_id='PRIVATE_SESSION', turn_id='t', assistant_response='PRIVATE_ANSWER') is None
    assert len(seen) == 1 and seen[0]['internal'] is True
    assert 'PRIVATE_READBACK' in seen[0]['evidence']
    assert 'sk-abcdefghijklmnopqrstuvwxyz123456' not in seen[0]['evidence']
    assert not reviewer.turns
    persisted = json.dumps(c.data) + caplog.text
    for secret in ['PRIVATE_NOTE', 'PRIVATE_READBACK', 'PRIVATE_ANSWER', 'PRIVATE_SESSION', 'sk-abcdefghijklmnopqrstuvwxyz123456']:
        assert secret not in persisted
    assert c.data['last_decision']['source'] == 'post_llm_shadow'


def test_human_machine_looking_text_stays_human(monkeypatch):
    reviewer = r.Reviewer(ctx()); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    reviewer.before(session_id='s', turn_id='t', user_message='[INTERNAL NOTIFICATION] explain this',
                    conversation_history=[{'role': 'user', 'content': '[INTERNAL NOTIFICATION] explain this'}])
    reviewer.after(session_id='s', turn_id='t', assistant_response='Explanation')
    assert seen[0]['internal'] is False


def test_changed_prompt_resets_evidence_epoch():
    reviewer = r.Reviewer(ctx())
    reviewer.before(session_id='s', turn_id='t', user_message='old')
    reviewer.tool(session_id='s', turn_id='t', tool_name='terminal', result='old result')
    reviewer.before(session_id='s', turn_id='t', user_message='old')
    assert reviewer.turns[('s', 't')]['tools']
    reviewer.before(session_id='s', turn_id='t', user_message='new')
    assert not reviewer.turns[('s', 't')]['tools']


def test_reset_scoped_and_turn_cache_bounded():
    reviewer = r.Reviewer(ctx({'max_cached_turns': 2}))
    for s in ['a', 'b', 'c']:
        reviewer.before(session_id=s, turn_id='1', user_message='request')
    assert list(reviewer.turns) == [('b', '1'), ('c', '1')]
    reviewer.reset(session_id='new', old_session_id='b')
    assert list(reviewer.turns) == [('c', '1')]


def test_history_bounded_and_reviewer_tool_is_not_evidence(monkeypatch):
    c = ctx({'max_review_history': 2}); reviewer = r.Reviewer(c)
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: decision())
    for _ in range(4):
        reviewer.handler(state())
    assert len(c.data['review_history']) == 2
    reviewer.before(session_id='s', turn_id='t', user_message='request')
    reviewer.tool(session_id='s', turn_id='t', tool_name=r.TOOL_NAME, result='no self-confirmation')
    assert reviewer.turns[('s', 't')]['tools'] == []


def test_tool_digest_bounded_and_explicitly_incomplete():
    reviewer = r.Reviewer(ctx({'max_tool_characters': 8, 'max_tool_events': 2}))
    reviewer.before(session_id='s', turn_id='t', user_message='request')
    for _ in range(4):
        reviewer.tool(session_id='s', turn_id='t', tool_name='terminal', result='whole output')
    tools = reviewer.turns[('s', 't')]['tools']
    assert len(tools) == 2 and all('truncated; not complete verification' in t for t in tools)


def test_redaction_failure_drops_evidence(monkeypatch):
    reviewer = r.Reviewer(ctx())
    reviewer.before(session_id='s', turn_id='t', user_message='request')
    monkeypatch.setattr(r, 'redact', lambda _: (_ for _ in ()).throw(ValueError('SECRET')))
    reviewer.tool(session_id='s', turn_id='t', tool_name='memory', result='SECRET')
    assert reviewer.turns[('s', 't')]['tools'] == ['[evidence unavailable: serialization/redaction failed]']


def test_compaction_unique_turn_match(monkeypatch):
    reviewer = r.Reviewer(ctx()); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    reviewer.before(session_id='old', turn_id='t', user_message='request')
    reviewer.after(session_id='new', turn_id='t', assistant_response='answer')
    assert len(seen) == 1 and not reviewer.turns


def test_compaction_does_not_cross_ambiguous_sessions(monkeypatch):
    reviewer = r.Reviewer(ctx())
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: pytest.fail('ambiguous join'))
    for sid in ['a', 'b']:
        reviewer.before(session_id=sid, turn_id='t', user_message='request')
    reviewer.after(session_id='new', turn_id='t', assistant_response='answer')


@pytest.mark.parametrize('mode', ['shadow', 'active', 'off'])
def test_registration_public_interface(mode):
    c = ctx({'mode': mode}); r.register(c)
    assert set(c.hooks) == {'pre_llm_call', 'post_llm_call', 'post_tool_call', 'on_session_reset'}
    tool = c.tools[r.TOOL_NAME]
    assert tool['schema'] == r.TOOL_SCHEMA
    assert tool['check_fn']() is (mode != 'off')


def test_active_only_cooperative_review_not_duplicate_posthook(monkeypatch):
    reviewer = r.Reviewer(ctx({'mode': 'active'})); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    reviewer.before(session_id='s', turn_id='t', user_message='request')
    assert reviewer.after(session_id='s', turn_id='t', assistant_response='answer') is None
    assert not seen
    result = json.loads(reviewer.handler(state()))
    assert result['ok'] and result['mode'] == 'active' and len(seen) == 1


def test_shadow_explicit_and_posthook_independent_calls(monkeypatch):
    reviewer = r.Reviewer(ctx()); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    reviewer.before(session_id='s', turn_id='t', user_message='request')
    reviewer.handler(state())
    reviewer.after(session_id='s', turn_id='t', assistant_response='answer')
    assert len(seen) == 2


def test_off_handler_uncertain_no_network(monkeypatch):
    reviewer = r.Reviewer(ctx({'mode': 'off'}))
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: pytest.fail('off network'))
    result = json.loads(reviewer.handler(state()))
    assert not result['ok'] and result['mode'] == 'off'
    assert result['review']['disposition'] == 'uncertain'


@pytest.mark.parametrize('bad_state', [{}, state(internal=1), state(evidence=[]), {**state(), 'unexpected': 'PRIVATE'}, []])
def test_invalid_tool_args_safe_uncertain(bad_state):
    reviewer = r.Reviewer(ctx())
    result = json.loads(reviewer.handler(bad_state))
    assert not result['ok'] and result['review']['disposition'] == 'uncertain'
    assert 'PRIVATE' not in json.dumps(result)


def test_key_missing(monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    assert r.Jev(r.DEFAULTS).decide(state()) == {'error': 'missing_api_key'}


def test_mock_transport_actual_endpoint_and_schema(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'unit-test-not-real')
    def respond(req):
        assert str(req.url) == r.ENDPOINT
        body = json.loads(req.content)
        assert body == {'model': 'typesafe/jev-1.13', 'state': state(), 'questions': r.QUESTIONS}
        return httpx.Response(200, json=decision())
    result = r.Jev(r.DEFAULTS, httpx.MockTransport(respond)).decide(state())
    assert result['answers']['outcome']['choice'] == 'normal_answer'


@pytest.mark.parametrize('code', [400, 401, 429, 503])
def test_http_errors_no_body_leak(monkeypatch, code):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'unit-test-not-real')
    client = r.Jev(r.DEFAULTS, httpx.MockTransport(lambda req: httpx.Response(code, json={'error': 'PRIVATE'})))
    assert client.decide(state()) == {'error': 'http_error'}


@pytest.mark.parametrize('body', [b'not JSON PRIVATE', b'[]', b'{"answers": []}'])
def test_invalid_json_safe_abstention(monkeypatch, body):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'unit-test-not-real')
    client = r.Jev(r.DEFAULTS, httpx.MockTransport(lambda req: httpx.Response(200, content=body)))
    result = client.decide(state())
    assert result.get('error') and 'PRIVATE' not in json.dumps(result)


def test_transport_timeout_safe(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'unit-test-not-real')
    def fail(req):
        raise httpx.ReadTimeout('PRIVATE')
    assert r.Jev(r.DEFAULTS, httpx.MockTransport(fail)).decide(state()) == {'error': 'transport_error'}


def test_oversized_note_abstains_without_truncation(monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'unit-test-not-real')
    client = r.Jev({**r.DEFAULTS, 'max_input_characters': 1}, httpx.MockTransport(lambda req: pytest.fail('oversized request')))
    assert client.decide(state(user_message='whole original note')) == {'error': 'input_too_large'}


def test_complete_original_note_and_draft_forwarded(monkeypatch):
    reviewer = r.Reviewer(ctx()); seen = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: seen.append(s) or decision())
    original = 'Это полный текст заметки. ' * 100
    reviewer.handler(state(user_message=original, assistant_response='whole draft'))
    assert seen[0]['user_message'] == original
    assert seen[0]['assistant_response'] == 'whole draft'


def fixture(outcome='normal_answer', expected_disposition='accept'):
    return {'id': 'synthetic-private-id', 'state': state(user_message='PRIVATE_NOTE'),
            'expected': {'scenario': outcome, 'disposition': expected_disposition},
            'decision_origin': 'synthetic_unit_test', 'decision_response': decision(outcome)}


def test_stored_replay_independently_checked_and_metadata_only(tmp_path):
    f = tmp_path / 'fixtures.jsonl'
    rows = [fixture(), {**fixture('technical_failure', 'recover'), 'id': 'two'}]
    f.write_text('\n'.join(json.dumps(row) for row in rows))
    summary, records = r.evaluate(f, r.DEFAULTS)
    assert summary['total'] == summary['matched'] == summary['valid_reviews'] == 2
    assert summary['synthetic_replay'] is True
    assert 'PRIVATE_NOTE' not in json.dumps(records)
    assert 'synthetic-private-id' not in json.dumps(records)
    assert records[0]['decision_origin'] == 'synthetic_unit_test'


def test_replay_expected_labels_not_generated_from_decisions(tmp_path):
    row = fixture(); row['expected']['disposition'] = 'correct'
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row))
    summary, _ = r.evaluate(f, r.DEFAULTS)
    assert summary['mismatched'] == 1 and summary['match_rate'] == 0


def test_offline_missing_decisions_requires_live(tmp_path):
    row = fixture(); row.pop('decision_response')
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='requires a stored'):
        r.evaluate(f, r.DEFAULTS)
    seen = []
    client = SimpleNamespace(decide=lambda s: seen.append(s) or decision())
    summary, records = r.evaluate(f, r.DEFAULTS, live=True, client=client)
    assert len(seen) == 1 and summary['evaluation_mode'] == 'live'
    assert not summary['synthetic_replay'] and records[0]['decision_origin'] == 'live'


@pytest.mark.parametrize('mutation', ['empty', 'duplicate', 'invalid_expected', 'missing_expected', 'wrong_state', 'origin_text'])
def test_invalid_fixture_rejected(tmp_path, mutation):
    row = fixture(); rows = [row]
    if mutation == 'empty': rows = []
    if mutation == 'duplicate': rows.append(row)
    if mutation == 'invalid_expected': row['expected']['scenario'] = 'PRIVATE'
    if mutation == 'missing_expected': row.pop('expected')
    if mutation == 'wrong_state': row['state']['pending_background'] = 1
    if mutation == 'origin_text': row['decision_origin'] = 'PRIVATE'
    f = tmp_path / 'f.jsonl'; f.write_text('\n'.join(json.dumps(v) for v in rows))
    with pytest.raises(ValueError):
        r.evaluate(f, r.DEFAULTS)


def test_cli_actual_replay_and_audit(tmp_path, capsys):
    f = tmp_path / 'fixtures.jsonl'; f.write_text(json.dumps(fixture()))
    out = tmp_path / 'audit.jsonl'
    assert r.main(['--evaluate', str(f), '--output', str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary['total'] == 1
    assert 'PRIVATE_NOTE' not in out.read_text()
    assert json.loads(out.read_text())['matched'] is True
    with pytest.raises(SystemExit) as exc:
        r.main(['--evaluate', str(f), '--output', str(out)])
    assert exc.value.code == 2


def test_cli_requires_explicit_live_or_stored_decision(tmp_path):
    f = tmp_path / 'state.json'; f.write_text(json.dumps(state()))
    with pytest.raises(SystemExit) as exc:
        r.main(['--state-file', str(f)])
    assert exc.value.code == 2
    stored = tmp_path / 'decision.json'; stored.write_text(json.dumps(decision()))
    assert r.main(['--state-file', str(f), '--decision-file', str(stored)]) == 0


def test_cli_failure_exits_nonzero(tmp_path, capsys):
    row = fixture(); row['decision_response'] = {'error': 'missing_api_key'}
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row))
    assert r.main(['--evaluate', str(f)]) == 1
    assert json.loads(capsys.readouterr().out)['valid_reviews'] == 0


def test_bundled_english_russian_fixtures():
    summary, records = r.evaluate(P / 'examples' / 'synthetic-outcomes.jsonl', r.DEFAULTS)
    assert summary['matched'] == summary['total']
    assert len(records) >= 8
    assert summary['synthetic_replay'] is True


def test_oversized_state_replay_abstains_too():
    result = r.review_envelope(decision(), state(user_message='full original note'),
                               {**r.DEFAULTS, 'max_input_characters': 1})
    assert not result['ok'] and result['review']['disposition'] == 'uncertain'


def test_extreme_numeric_values_never_crash():
    enormous = 10 ** 500
    assert not r.unit(enormous)
    assert r.numeric_usage({'usage': {'cost': enormous}}) == {}
    payload = decision(); payload['answers']['outcome']['confidence'] = enormous
    assert not r.review_envelope(payload, state(), r.DEFAULTS)['ok']


@pytest.mark.parametrize('field', ['scenario', 'disposition', 'decision_origin'])
def test_nonstring_fixture_labels_rejected(tmp_path, field):
    row = fixture()
    if field == 'decision_origin':
        row[field] = []
    else:
        row['expected'][field] = []
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row))
    with pytest.raises(ValueError):
        r.evaluate(f, r.DEFAULTS)


def test_expected_abstention_cli_success(tmp_path, capsys):
    row = fixture(); row['expected'] = {'scenario': 'uncertain', 'disposition': 'uncertain'}
    row['decision_response']['answers']['outcome']['confidence'] = .1
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row))
    assert r.main(['--evaluate', str(f)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['matched'] == report['abstained'] == 1
    assert report['valid_reviews'] == 0 and report['failed_requests'] == 0


def test_usage_aggregation_overflow_rejected(tmp_path):
    row = fixture(); row['decision_response']['usage'] = {'cost': 1e308}
    f = tmp_path / 'f.jsonl'; f.write_text(json.dumps(row) + '\n' + json.dumps({**row, 'id': 'two'}))
    with pytest.raises(ValueError, match='out of range'):
        r.evaluate(f, r.DEFAULTS)


def test_cli_live_missing_key_no_synthetic_fallback(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    f = tmp_path / 'state.json'; f.write_text(json.dumps(state()))
    assert r.main(['--state-file', str(f), '--live']) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False and result['answers'] == {}
    assert result['review']['disposition'] == 'uncertain'
