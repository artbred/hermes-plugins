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
    assert critic._judge('draft', 'low', 'system') == (PASS, 'kimi')
    assert seen == ['kimi']


def test_valid_kimi_challenge_is_not_bypassed(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=FAIL, router=PASS)
    assert critic._judge('draft', 'low', 'system') == (FAIL, 'kimi')
    assert seen == ['kimi']


def test_kimi_unavailable_uses_router_directly(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=None, router=PASS)
    assert critic._judge('draft', 'low', 'system') == (PASS, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_timeout_advances_to_router(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi=httpx.ReadTimeout('test timeout'), router=PASS)
    assert critic._judge('draft', 'low', 'system') == (PASS, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_invalid_schema_advances(critic, monkeypatch):
    seen = chain(critic, monkeypatch, kimi={'passed':'false'}, router=FAIL)
    assert critic._judge('draft', 'low', 'system') == (FAIL, 'openrouter-fallback')
    assert seen == ['kimi', 'router']


def test_all_unavailable_fail_open(critic, monkeypatch):
    seen = chain(critic, monkeypatch)
    assert critic._judge('draft', 'low', 'system') == (None, 'none')
    assert seen == ['kimi', 'router']


def test_disabled_fallback_is_not_called(critic, monkeypatch):
    critic._fallback_enabled = False
    seen = chain(critic, monkeypatch, kimi=None, router=PASS)
    assert critic._judge('draft', 'low', 'system') == (None, 'none')
    assert seen == ['kimi']


def test_subscription_route_has_been_removed(critic):
    assert not hasattr(critic, '_judge_muse_code')
    assert not hasattr(critic, '_muse_subscription_key')


def http_client(c, monkeypatch, response):
    post = Mock(return_value=response)
    client = Mock(); client.__enter__ = Mock(return_value=client); client.__exit__ = Mock(return_value=False); client.post = post
    monkeypatch.setattr(c.httpx, 'Client', lambda **kw: client)
    return post


def test_openrouter_always_uses_max_thinking_and_contributor(critic, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter')
    post = http_client(critic, monkeypatch, httpx.Response(200, json={'choices':[{'message':{'content':json.dumps(PASS)}}]}))
    assert critic._judge_openrouter('draft', 'low', 'system') == PASS
    payload = post.call_args.kwargs['json']
    assert payload['model'] == 'meta/muse-spark-1.3-contributor'
    assert payload['reasoning']['effort'] == 'max'
    assert payload['max_tokens'] >= 16384
    assert payload['provider']['require_parameters'] is True


def test_rejected_max_thinking_is_not_retried_without_reasoning(critic, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-openrouter')
    post = http_client(critic, monkeypatch, httpx.Response(400, json={'error':{'message':'test rejection'}}))
    assert critic._judge_openrouter('draft', 'low', 'system') is None
    assert post.call_count == 1
    assert post.call_args.kwargs['json']['reasoning']['effort'] == 'max'


def test_nominal_http_budgets_fit_hook_budget(critic):
    assert critic.REQUEST_TIMEOUT + critic.OPENROUTER_TIMEOUT < 120
