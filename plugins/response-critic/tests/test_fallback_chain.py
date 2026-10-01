"""Two-stage fallback tests; mocked HTTP is test data, not a live API result."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
import httpx
import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PASS = {'passed': True, 'feedback': ''}
FAIL = {'passed': False, 'feedback': 'Verify the unsupported deployment claim.'}

@pytest.fixture
def critic(monkeypatch):
    spec = importlib.util.spec_from_file_location('critic_fallback_test', PLUGIN / '__init__.py')
    c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
    monkeypatch.setattr(c, '_ensure_logging', lambda: None)
    return c


def chain(c, monkeypatch, kimi=None, router=None):
    seen = []
    for name, result in [('kimi', kimi), ('router', router)]:
        def callback(*args, name=name, result=result):
            seen.append(name)
            if isinstance(result, Exception): raise result
            return result
        monkeypatch.setattr(c, {'kimi':'_judge_kimi','router':'_judge_openrouter'}[name], callback)
    return seen


def test_kimi_success_stops_chain(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=PASS, router=FAIL)
    assert critic._judge('draft', 'high', 'system') == (PASS, 'kimi')
    assert seen == ['kimi']


def test_valid_kimi_challenge_is_not_bypassed(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=FAIL, router=PASS)
    assert critic._judge('draft', 'high', 'system') == (FAIL, 'kimi')
    assert seen == ['kimi']


def test_kimi_unavailable_uses_router_directly(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=None, router=PASS)
    assert critic._judge('draft', 'high', 'system') == (PASS, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_timeout_advances_to_router(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=httpx.ReadTimeout('test timeout'), router=PASS)
    assert critic._judge('draft', 'high', 'system') == (PASS, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_invalid_schema_advances(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi={'passed':'false'}, router=FAIL)
    assert critic._judge('draft', 'high', 'system') == (FAIL, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_all_unavailable_fail_open(critic, monkeypatch):
    seen = chain(critic, monkeypatch)
    assert critic._judge('draft', 'high', 'system') == (None, 'none')
    assert seen == ['kimi', 'router']


def test_disabled_fallback_is_not_called(critic, monkeypatch):
    critic._fallback_enabled = False
    seen = chain(critic, monkeypatch, kimi=None, router=PASS)
    assert critic._judge('draft', 'high', 'system') == (None, 'none')
    assert seen == ['kimi']


def test_subscription_route_has_been_removed(critic):
    assert not hasattr(critic, '_judge_muse_code')
    assert not hasattr(critic, '_muse_subscription_key')


def http_client(c, monkeypatch, response):
    post = Mock(return_value=response)
    client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False); client.post = post
    # Explicit offline capability fixture; capability discovery is tested separately.
    monkeypatch.setattr(c, '_fetch_openrouter_efforts', lambda: ['high', 'max'])
    monkeypatch.setattr(c.httpx, 'Client', lambda **kw: client)
    return post


@pytest.mark.parametrize('effort', ['high', 'max'])
def test_openrouter_preserves_requested_effort_and_contributor(critic, monkeypatch, effort):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter')
    post = http_client(critic, monkeypatch, httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(PASS)}}]}))
    assert critic._judge_openrouter('draft', effort, 'system') == PASS
    payload = post.call_args.kwargs['json']
    assert payload['model'] == 'meta/muse-spark-1.3-contributor'
    assert payload['reasoning']['effort'] == effort
    assert payload['max_tokens'] == 16384
    assert payload['provider']['require_parameters'] is True


@pytest.mark.parametrize('effort,wire_effort', [('high', 'high'), ('max', 'max')])
def test_kimi_preserves_supported_requested_effort(critic, monkeypatch, effort, wire_effort):
    monkeypatch.setenv('KIMI_API_KEY', 'test-kimi')
    post = http_client(critic, monkeypatch, httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(PASS)}}]}))
    assert critic._judge_kimi('draft', effort, 'system') == PASS
    assert post.call_args.kwargs['json']['reasoning_effort'] == wire_effort


@pytest.mark.parametrize('effort', ['high', 'max'])
def test_fallback_chain_forwards_requested_effort_unchanged(critic, monkeypatch, effort):
    primary = Mock(return_value=None)
    fallback = Mock(return_value=PASS)
    monkeypatch.setattr(critic, '_judge_kimi', primary)
    monkeypatch.setattr(critic, '_judge_openrouter', fallback)
    assert critic._judge('draft', effort, 'system') == (PASS, 'openrouter-fallback')
    primary.assert_called_once_with('draft', effort, 'system')
    fallback.assert_called_once_with('draft', effort, 'system')


@pytest.mark.parametrize('effort,canonical', [
    ('medium', 'high'), ('low', 'high'), ('minimal', 'high'), ('xhigh', 'max'),
    ('unknown', 'max'), ('', 'max'), (None, 'max'), (False, 'max'),
])
@pytest.mark.parametrize('provider', ['kimi', 'openrouter'])
def test_transport_effort_is_bounded_even_for_legacy_or_invalid_callers(critic, monkeypatch, effort, canonical, provider):
    monkeypatch.setenv('KIMI_API_KEY', 'test-kimi')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter')
    post = http_client(critic, monkeypatch, httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(PASS)}}]}))
    assert critic._bounded_effort(effort) == canonical
    adapter = critic._judge_kimi if provider == 'kimi' else critic._judge_openrouter
    assert adapter('draft', effort, 'system') == PASS
    payload = post.call_args.kwargs['json']
    if provider == 'kimi':
        assert payload['reasoning_effort'] == ('max' if canonical == 'max' else 'high')
    else:
        assert payload['reasoning']['effort'] == canonical
        assert payload['max_tokens'] == 16384
        assert payload['provider']['require_parameters'] is True


@pytest.mark.parametrize('effort', ['high', 'max'])
def test_rejected_thinking_is_not_retried_without_reasoning(critic, monkeypatch, effort):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter')
    post = http_client(critic, monkeypatch, httpx.Response(400, json={'error':{'message':'test rejection'}}))
    assert critic._judge_openrouter('draft', effort, 'system') is None
    assert post.call_count == 1
    assert post.call_args.kwargs['json']['reasoning']['effort'] == effort


def test_http_providers_share_five_minute_budget(critic):
    assert critic._review_budget_seconds == 300
    assert critic.REQUEST_TIMEOUT == 300
    assert critic.OPENROUTER_TIMEOUT == 300
    token = critic._judge_deadline.set(critic.time.monotonic() + 299)
    try:
        assert 298 < critic._request_timeout(critic.OPENROUTER_TIMEOUT) <= 299
    finally:
        critic._judge_deadline.reset(token)
