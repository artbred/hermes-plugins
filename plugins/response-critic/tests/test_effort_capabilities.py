"""Capability metadata and audit tests use explicit offline fixtures, never live verdicts."""
import copy
import hashlib
import importlib.util
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PASS = {'passed': True, 'feedback': ''}
FAIL = {'passed': False, 'feedback': 'PRIVATE_FEEDBACK'}


def load():
    spec = importlib.util.spec_from_file_location('critic_capabilities', PLUGIN / '__init__.py')
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    c._ensure_logging = lambda: None
    return c


class State:
    def __init__(self, directory):
        self.data_dir = directory
        self.values = {}

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = copy.deepcopy(value)


@pytest.fixture
def critic(tmp_path):
    c = load()
    c._provider_state = State(tmp_path / 'profile')
    return c


@pytest.mark.parametrize('requested', ['high', 'max'])
@pytest.mark.parametrize('supported,expected_high,expected_max', [
    (['max', 'xhigh', 'high', 'medium'], 'high', 'max'),
    (['xhigh', 'high'], 'high', 'xhigh'),
    (['high'], 'high', 'high'),
    (['max'], 'max', 'max'),
    (['xhigh'], 'xhigh', 'xhigh'),
    (['medium', 'low', 'none', 'minimal'], None, None),
    ([], None, None), (None, None, None), ('max', None, None),
])
def test_resolver_never_invents_or_weakens_reasoning(critic, requested, supported, expected_high, expected_max):
    assert critic._resolve_effort(requested, supported) == (expected_high if requested == 'high' else expected_max)


@pytest.mark.parametrize('value,expected', [('medium', 'high'), ('low', 'high'),
    ('minimal', 'high'), ('xhigh', 'max'), ('none', 'max'), (False, 'max')])
def test_legacy_canonical_boundary(critic, value, expected):
    assert critic.EFFORT_LADDER == ['high', 'max']
    assert critic.DEFAULT_MIN_EFFORT == 'high'
    assert critic._bounded_effort(value) == expected


def wire(verdict=PASS):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(verdict)}}]})


def transport(c, monkeypatch, post_response=PASS, metadata=None):
    metadata = metadata if metadata is not None else {'data': [{
        'id': c._fallback_model, 'reasoning': {'supported_efforts': ['max', 'high']}}]}
    response = httpx.Response(200, json=metadata, request=httpx.Request('GET', 'https://openrouter.ai/api/v1/models'))
    stream = Mock()
    stream.__enter__ = Mock(return_value=response)
    stream.__exit__ = Mock(return_value=False)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.stream.return_value = stream
    client.post.return_value = wire(post_response)
    factory = Mock(return_value=client)
    monkeypatch.setattr(c.httpx, 'Client', factory)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'PRIVATE_TEST_KEY')
    monkeypatch.delenv('KIMI_API_KEY', raising=False)
    monkeypatch.delenv('KIMI_CODING_API_KEY', raising=False)
    return client, response, factory


@pytest.mark.parametrize('effort', ['high', 'max'])
@pytest.mark.parametrize('supported', [['high', 'max'], ['high', 'xhigh'], ['high'], ['xhigh']])
def test_fallback_payload_and_audit_record_canonical_and_effective(critic, monkeypatch, effort, supported):
    c = critic
    client, _, _ = transport(c, monkeypatch, metadata={'data': [
        {'id': c._fallback_model, 'reasoning': {'supported_efforts': supported}}]})
    token = c._review_session.set('PRIVATE_SESSION')
    try:
        assert c._judge('PRIVATE_DRAFT', effort, 'PRIVATE_SYSTEM') == (PASS, 'openrouter-fallback')
    finally:
        c._review_session.reset(token)
    payload = client.post.call_args.kwargs['json']
    effective = c._resolve_effort(effort, supported)
    assert payload['reasoning']['effort'] == effective
    assert payload['provider'] == {'require_parameters': True}
    assert payload['max_tokens'] == 16384
    audit = list(c._provider_state.get(c._AUDIT_STATE_KEY).values())
    assert len(audit) == 2
    assert audit[-1]['requested_effort'] == effort
    assert audit[-1]['effective_effort'] == effective
    assert audit[-1]['verdict'] == 'pass'
    assert audit[-1]['session_hash'] == hashlib.sha256(b'PRIVATE_SESSION').hexdigest()
    assert audit[-1]['provider_model_hash'] == c._provider_key('openrouter-fallback')
    stored = json.dumps(c._provider_state.values)
    for marker in ['PRIVATE_SESSION', 'PRIVATE_DRAFT', 'PRIVATE_SYSTEM', 'PRIVATE_TEST_KEY', c._fallback_model]:
        assert marker not in stored
    client.stream.assert_called_once_with('GET', 'https://openrouter.ai/api/v1/models')


@pytest.mark.parametrize('effort', ['high', 'max'])
def test_explicit_null_capabilities_accept_all_gateway_efforts(critic, monkeypatch, effort):
    client, _, _ = transport(critic, monkeypatch, metadata={'data': [{
        'id': critic._fallback_model, 'reasoning': {'supported_efforts': None}}]})
    assert critic._judge_openrouter('draft', effort, 'system') == PASS
    assert client.post.call_args.kwargs['json']['reasoning']['effort'] == effort


@pytest.mark.parametrize('metadata', [{'data': []}, {'data': [{'id': 'other'}]},
    {'data': [{'id': 'meta/muse-spark-1.3-contributor'}]},
    {'data': [{'id': 'meta/muse-spark-1.3-contributor', 'reasoning': {'supported_efforts': ['medium']}}]},
    {'data': 'invalid'}, {}])
def test_missing_or_no_usable_capabilities_is_unavailable_not_a_weakened_post(critic, monkeypatch, metadata):
    client, _, _ = transport(critic, monkeypatch, metadata=metadata)
    assert critic._judge_openrouter('draft', 'max', 'system') is None
    client.post.assert_not_called()
    assert critic._provider_state.get(critic._FAILURE_STATE_KEY)[critic._provider_key('openrouter-fallback')]['failures'] == 1


def test_capability_cache_ttl_reload_profile_and_model_scope(critic, monkeypatch, tmp_path):
    c = critic
    clock = [1000000.0]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    fetch = Mock(return_value=['xhigh', 'high'])
    monkeypatch.setattr(c, '_fetch_openrouter_efforts', fetch)
    assert c._openrouter_efforts() == ['xhigh', 'high']
    assert c._openrouter_efforts() == ['xhigh', 'high']
    assert fetch.call_count == 1
    other = load()
    other._provider_state = c._provider_state
    monkeypatch.setattr(other, '_fetch_openrouter_efforts', fetch)
    assert other._openrouter_efforts() == ['xhigh', 'high']
    assert fetch.call_count == 1
    clock[0] += c.CAPABILITY_TTL + 1
    assert other._openrouter_efforts() == ['xhigh', 'high']
    assert fetch.call_count == 2
    other._fallback_model = 'another-model'
    other._openrouter_efforts()
    assert fetch.call_count == 3
    other._provider_state = State(tmp_path / 'another-profile')
    other._openrouter_efforts()
    assert fetch.call_count == 4
    serialized = json.dumps(c._provider_state.values)
    assert c._fallback_model not in serialized
    assert all(len(k) == 64 for k in c._provider_state.get(c._CAPABILITY_STATE_KEY))


def test_failed_metadata_is_short_cached_without_leaking_exception(critic, monkeypatch):
    c = critic
    clock = [1000000.0]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    fetch = Mock(side_effect=httpx.ReadTimeout('PRIVATE_EXCEPTION'))
    monkeypatch.setattr(c, '_fetch_openrouter_efforts', fetch)
    assert c._openrouter_efforts() == []
    assert c._openrouter_efforts() == []
    assert fetch.call_count == 1
    clock[0] += c.CAPABILITY_FAILURE_TTL + 1
    assert c._openrouter_efforts() == []
    assert fetch.call_count == 2
    assert 'PRIVATE_EXCEPTION' not in json.dumps(c._provider_state.values)


def test_metadata_fetch_size_and_timeouts_are_bounded(critic, monkeypatch):
    client, response, factory = transport(critic, monkeypatch)
    monkeypatch.setattr(response, 'iter_bytes', lambda: iter([b'x' * 8_000_001]))
    with pytest.raises(ValueError):
        critic._fetch_openrouter_efforts()
    assert 0 < factory.call_args.kwargs['timeout'] <= 10
    client.post.assert_not_called()


def test_unusable_kimi_capability_advances_to_highest_available_fallback(critic, monkeypatch):
    c = critic
    client, _, _ = transport(c, monkeypatch, metadata={'data': [{
        'id': c._fallback_model, 'reasoning': {'supported_efforts': ['high']}}]})
    monkeypatch.setenv('KIMI_API_KEY', 'PRIVATE_TEST_KEY')
    c._kimi_supported_efforts = ['medium', 'none']
    assert c._judge('draft', 'max', 'system') == (PASS, 'openrouter-fallback')
    assert client.post.call_count == 1
    assert client.post.call_args.kwargs['json']['reasoning']['effort'] == 'high'
    audits = list(c._provider_state.get(c._AUDIT_STATE_KEY).values())
    assert audits[0]['effective_effort'] is None and audits[0]['verdict'] == 'unavailable'
    assert audits[1]['effective_effort'] == 'high' and audits[1]['requested_effort'] == 'max'


@pytest.mark.parametrize('supported', [None, [], ['none', 'medium']])
def test_nonstandard_kimi_route_requires_explicit_usable_capabilities(critic, monkeypatch, supported):
    c = critic
    c._judge_base_url = 'https://custom.invalid/v1'
    c._kimi_supported_efforts = supported
    monkeypatch.setenv('KIMI_API_KEY', 'PRIVATE_TEST_KEY')
    post = Mock()
    monkeypatch.setattr(c, '_judge_http', post)
    assert c._judge_kimi('draft', 'max', 'system') is None
    post.assert_not_called()


def test_explicit_nonstandard_kimi_capabilities_map_max_to_xhigh(critic, monkeypatch):
    c = critic
    c.register(SimpleNamespace(state=c._provider_state, register_hook=Mock(),
        get_config=lambda k, d: {'judge_base_url': 'https://custom.invalid/v1',
                                'judge_supported_efforts': ['xhigh', 'high']}.get(k, d)))
    monkeypatch.setenv('KIMI_API_KEY', 'PRIVATE_TEST_KEY')
    judge = Mock(return_value=PASS)
    monkeypatch.setattr(c, '_judge_http', judge)
    assert c._judge_kimi('draft', 'max', 'system') == PASS
    assert judge.call_args.args[3]['reasoning_effort'] == 'xhigh'


@pytest.mark.parametrize('verdict,expected', [(PASS, 'pass'), (FAIL, 'fail'), (None, 'unavailable')])
def test_judge_audit_verdict_labels(critic, monkeypatch, verdict, expected):
    c = critic
    c._fallback_enabled = False
    monkeypatch.setattr(c, '_judge_kimi', lambda *_: verdict)
    c._judge('PRIVATE_DRAFT', 'max', 'PRIVATE_SYSTEM')
    row = next(iter(c._provider_state.get(c._AUDIT_STATE_KEY).values()))
    assert row['verdict'] == expected
    assert 'PRIVATE_' not in json.dumps(c._provider_state.values)


def test_audit_parallel_atomicity_reload_and_bounded_retention(critic):
    c = critic
    other = load()
    other._provider_state = c._provider_state
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: (c if i % 2 else other)._audit_decision(
            'session-' + str(i), 'max', 'xhigh', 'openrouter-fallback', .01, 'pass'), range(300)))
    audit = c._provider_state.get(c._AUDIT_STATE_KEY)
    assert len(audit) == 256
    assert all(len(row['session_hash']) == 64 for row in audit.values())
    other._audit_decision('new-session', skip='trivial')
    assert len(other._provider_state.get(other._AUDIT_STATE_KEY)) == 256
    assert 'new-session' not in json.dumps(audit)


def test_late_provider_cannot_append_decision_audit(critic, monkeypatch):
    c = critic
    c._review_budget_seconds = .02
    c._fallback_enabled = False
    release, finished = threading.Event(), threading.Event()
    def blocked(*_):
        release.wait(1)
        return PASS
    monkeypatch.setattr(c, '_judge_kimi', blocked)
    original = c._judge_chain
    def chain(*args):
        try:
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(c, '_judge_chain', chain)
    assert c._judge('draft', 'max', 'system') == (None, 'none')
    release.set()
    assert finished.wait(1)
    assert c._provider_state.get(c._AUDIT_STATE_KEY, {}) == {}


def test_circuit_and_local_triage_skips_are_audited(critic, monkeypatch):
    c = critic
    c._record_provider_failure('kimi', 'http', status=429)
    c._fallback_enabled = False
    c._judge('draft', 'high', 'system')
    monkeypatch.setattr(c, '_judge', Mock())
    c.validate_final_response('Short', session_id='PRIVATE_SESSION')
    rows = list(c._provider_state.get(c._AUDIT_STATE_KEY).values())
    assert [row['skip'] for row in rows] == ['circuit', 'short']
    assert rows[0]['requested_effort'] == 'high'
    assert rows[1]['session_hash'] == hashlib.sha256(b'PRIVATE_SESSION').hexdigest()
    c._judge.assert_not_called()
