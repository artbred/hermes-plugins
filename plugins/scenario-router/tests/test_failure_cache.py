"""Profile-scoped, bounded failure backoff; no raw inputs/errors are cached."""
import json

import pytest

from test_router import r, ctx, decision, state
from test_judge_policy import assert_conservative


@pytest.mark.parametrize('error', ['http_error', 'transport_error', 'invalid_response'])
def test_failure_cached_across_reload_without_raw_inputs(monkeypatch, error):
    clock = [1000.0]
    monkeypatch.setattr(r.time, 'time', lambda: clock[0])
    context = ctx({'mode': 'active'})
    reviewer = r.Reviewer(context)
    calls = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: calls.append(s) or {'error': error, 'body': 'PRIVATE_BODY'})
    first = reviewer.review(state(user_message='PRIVATE_NOTE', evidence='PRIVATE_EVIDENCE'))
    cached = reviewer.review(state())
    assert len(calls) == 1
    assert_conservative(first['review'])
    assert_conservative(cached['review'])
    cache = context.data['jev_failure_cache']
    assert set(cache) == {'provider_ref', 'kind', 'failures', 'retry_after'}
    assert cache['retry_after'] == 1030
    assert cache['failures'] == 1
    assert all(s not in json.dumps(context.data) for s in ['PRIVATE_NOTE', 'PRIVATE_EVIDENCE', 'PRIVATE_BODY'])
    reviewer.close()
    reloaded = r.Reviewer(context)
    monkeypatch.setattr(reloaded.client, 'decide', lambda s: pytest.fail('cached failure repeated after reload'))
    assert_conservative(reloaded.review(state())['review'])
    reloaded.close()


def test_cooldown_exponential_bounded_and_reset_on_success(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(r.time, 'time', lambda: clock[0])
    context = ctx()
    reviewer = r.Reviewer(context)
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: {'error': 'http_error'})
    delays = []
    for _ in range(10):
        reviewer.review(state())
        cache = context.data['jev_failure_cache']
        delays.append(cache['retry_after'] - clock[0])
        assert cache['failures'] <= 8
        clock[0] = cache['retry_after']
    assert delays == [30, 60, 120, 240, 300, 300, 300, 300, 300, 300]
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: decision())
    assert reviewer.review(state())['ok']
    assert context.data['jev_failure_cache'] == {}
    reviewer.close()


@pytest.mark.parametrize('error', ['missing_api_key', 'input_too_large', 'invalid_state', 'unavailable'])
def test_local_failures_not_cached(monkeypatch, error):
    context = ctx()
    reviewer = r.Reviewer(context)
    calls = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: calls.append(s) or {'error': error})
    reviewer.review(state()); reviewer.review(state())
    assert len(calls) == 2
    assert not context.data.get('jev_failure_cache')
    reviewer.close()


def test_schema_failure_cached_but_valid_low_confidence_is_not(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(r.time, 'time', lambda: clock[0])
    context = ctx()
    reviewer = r.Reviewer(context)
    malformed = decision()
    malformed['answers']['judge_effort']['choice'] = 'low'
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: malformed)
    assert_conservative(reviewer.review(state())['review'])
    assert context.data['jev_failure_cache']['kind'] == 'invalid_response'
    clock[0] += 30
    low = decision(confidence=.2)
    calls = []
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: calls.append(s) or low)
    assert not reviewer.review(state())['ok']
    assert not reviewer.review(state())['ok']
    assert len(calls) == 2
    assert context.data['jev_failure_cache'] == {}
    reviewer.close()


def test_profile_and_model_isolation(monkeypatch):
    one, two = ctx(), ctx()
    reviewer = r.Reviewer(one)
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: {'error': 'transport_error'})
    reviewer.review(state())
    other = r.Reviewer(two)
    monkeypatch.setattr(other.client, 'decide', lambda s: decision())
    assert other.review(state())['ok']
    changed = ctx({'judge_model': 'typesafe/other'})
    changed.state = one.state
    new_model = r.Reviewer(changed)
    monkeypatch.setattr(new_model.client, 'decide', lambda s: decision())
    assert new_model.review(state())['ok']
    for item in [reviewer, other, new_model]:
        item.close()


@pytest.mark.parametrize('mutation', ['extra', 'far_future', 'wrong_kind', 'bad_count', 'bad_time'])
def test_invalid_persisted_cache_does_not_suppress_requests(monkeypatch, mutation):
    clock = [1000.0]
    monkeypatch.setattr(r.time, 'time', lambda: clock[0])
    context = ctx()
    reviewer = r.Reviewer(context)
    monkeypatch.setattr(reviewer.client, 'decide', lambda s: {'error': 'http_error'})
    reviewer.review(state())
    cache = context.data['jev_failure_cache']
    if mutation == 'extra':
        cache['raw_error'] = 'PRIVATE'
    elif mutation == 'far_future':
        cache['retry_after'] = 100000000
    elif mutation == 'wrong_kind':
        cache['kind'] = []
    elif mutation == 'bad_count':
        cache['failures'] = True
    else:
        cache['retry_after'] = float('nan')
    reviewer.close()
    reloaded = r.Reviewer(context)
    calls = []
    monkeypatch.setattr(reloaded.client, 'decide', lambda s: calls.append(s) or decision())
    assert reloaded.review(state())['ok'] and len(calls) == 1
    reloaded.close()


@pytest.mark.parametrize('bad', [{'failure_cooldown_seconds': 0}, {'failure_max_cooldown_seconds': 3601},
                                {'failure_cooldown_seconds': '30'}, {'failure_max_cooldown_seconds': True},
                                {'failure_cooldown_seconds': 300, 'failure_max_cooldown_seconds': 30}])
def test_invalid_cooldown_budgets_rejected(bad):
    with pytest.raises(ValueError):
        r.Reviewer(ctx(bad))
