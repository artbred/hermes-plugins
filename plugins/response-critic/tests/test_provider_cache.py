"""Durable circuit-breaker tests. HTTP responses are explicit offline fixtures."""
import copy
import importlib.util
import json
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

PLUGIN = Path(__file__).resolve().parents[1]
PASS = {'passed': True, 'feedback': ''}
NEGATIVE = {'passed': False, 'feedback': 'Verify the unsupported claim.'}


def load(name='critic_cache_test'):
    spec = importlib.util.spec_from_file_location(name, PLUGIN / '__init__.py')
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    c._ensure_logging = lambda: None
    return c


class FakeState:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.values = {}

    def get(self, key, default=None):
        return copy.deepcopy(self.values.get(key, default))

    def set(self, key, value):
        self.values[key] = copy.deepcopy(value)


def register(c, state, settings=None):
    settings = settings or {}
    ctx = SimpleNamespace(state=state, get_config=lambda key, default: settings.get(key, default),
                          register_hook=Mock())
    c.register(ctx)
    return ctx


@pytest.fixture
def critic(tmp_path, monkeypatch):
    c = load()
    register(c, FakeState(tmp_path / 'profile' / 'plugin-data' / 'response-critic'))
    monkeypatch.setenv('KIMI_API_KEY', 'offline-kimi-key')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'offline-openrouter-key')
    return c


def wire(verdict=PASS):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(verdict)}}]})


def transport(c, monkeypatch, responses):
    post = Mock(side_effect=responses)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post = post
    monkeypatch.setattr(c.httpx, 'Client', lambda **kw: client)
    return post


def row(c, provider='kimi'):
    return c._provider_state.get(c._FAILURE_STATE_KEY, {}).get(c._provider_key(provider), {})


@pytest.mark.parametrize('status', [401, 403, 429])
@pytest.mark.parametrize('provider', ['kimi', 'openrouter-fallback'])
def test_auth_quota_immediately_open_and_survive_reregister_and_reload(critic, monkeypatch, status, provider):
    c = critic
    post = transport(c, monkeypatch, [httpx.Response(status, text='PRIVATE_PROVIDER_BODY')])
    callback = c._judge_kimi if provider == 'kimi' else c._judge_openrouter
    assert callback('draft', 'medium', 'system') is None
    assert row(c, provider)['failures'] == 1
    assert not c._provider_available(provider)
    register(c, c._provider_state)
    reloaded = load('critic_cache_reloaded')
    register(reloaded, c._provider_state)
    assert not reloaded._provider_available(provider)
    callback = reloaded._judge_kimi if provider == 'kimi' else reloaded._judge_openrouter
    assert callback('new draft', 'max', 'system') is None
    assert post.call_count == 1
    serialized = json.dumps(c._provider_state.values)
    for private in ['PRIVATE_PROVIDER_BODY', 'offline-kimi-key', 'offline-openrouter-key',
                    c._judge_model, c._judge_base_url, c._fallback_model]:
        assert private not in serialized
    assert all(len(key) == 64 for key in c._provider_state.get(c._FAILURE_STATE_KEY))


@pytest.mark.parametrize('failure', ['network', '5xx', 'schema', 'bad-json'])
def test_repeated_transient_errors_open_at_threshold_without_double_count(critic, monkeypatch, failure):
    c = critic
    responses = {
        'network': lambda: httpx.ReadTimeout('PRIVATE_EXCEPTION_TEXT'),
        '5xx': lambda: httpx.Response(503, text='PRIVATE_BODY'),
        'schema': lambda: wire({'passed': 'true', 'feedback': ''}),
        'bad-json': lambda: httpx.Response(200, text='PRIVATE_NOT_JSON'),
    }
    post = transport(c, monkeypatch, [responses[failure](), wire(), responses[failure](), wire(), wire()])
    assert c._judge('draft', 'high', 'system')[1] == 'openrouter-fallback'
    assert row(c)['failures'] == 1
    assert c._provider_available('kimi')
    assert c._judge('draft', 'high', 'system')[1] == 'openrouter-fallback'
    assert row(c)['failures'] == 2
    assert not c._provider_available('kimi')
    assert c._judge('draft', 'high', 'system')[1] == 'openrouter-fallback'
    assert row(c)['failures'] == 2  # cached failures are not additional attempts
    assert post.call_count == 5


def test_cooldown_expiry_probe_exponential_bounded_backoff_and_negative_recovery(critic, monkeypatch):
    c = critic
    clock = [1_000_000.0]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    post = transport(c, monkeypatch, [httpx.Response(429) for _ in range(6)] + [wire(NEGATIVE)])
    cooldowns = [300, 600, 1200, 2400, 3600, 3600]
    for expected in cooldowns:
        assert c._judge_kimi('draft', 'max', 'system') is None
        assert row(c)['until'] - clock[0] == expected
        assert not c._provider_available('kimi')
        clock[0] += expected
        assert c._provider_available('kimi')
    assert c._judge_kimi('draft', 'max', 'system') == NEGATIVE
    assert row(c)['failures'] == row(c)['open_count'] == row(c)['until'] == row(c)['probe_until'] == 0
    assert c._provider_available('kimi')
    assert post.call_count == 7


@pytest.mark.parametrize('retry_after,expected', [
    ('900', 900), ('999999', 3600), ('-100', 300), ('garbage', 300),
    ('NaN', 300), ('Infinity', 300), ('0', 300),
])
def test_retry_after_is_validated_and_bounded(critic, monkeypatch, retry_after, expected):
    c = critic
    monkeypatch.setattr(c.time, 'time', lambda: 1_000_000.0)
    transport(c, monkeypatch, [httpx.Response(429, headers={'Retry-After': retry_after})])
    c._judge_kimi('draft', 'medium', 'system')
    assert row(c)['until'] == 1_000_000 + expected


def test_retry_after_http_date_supported(critic, monkeypatch):
    c = critic
    monkeypatch.setattr(c.time, 'time', lambda: 1_000_000.0)
    date = format_datetime(datetime.fromtimestamp(1_000_700, timezone.utc), usegmt=True)
    transport(c, monkeypatch, [httpx.Response(503, headers={'Retry-After': date})] * 2)
    c._judge_kimi('draft', 'max', 'system')
    c._judge_kimi('draft', 'max', 'system')
    assert row(c)['until'] == 1_000_700


@pytest.mark.parametrize('verdict', [PASS, NEGATIVE])
def test_only_valid_verdict_resets_consecutive_failure_count(critic, monkeypatch, verdict):
    c = critic
    transport(c, monkeypatch, [httpx.Response(500), wire(verdict), httpx.Response(500)])
    assert c._judge_kimi('draft', 'medium', 'system') is None
    assert row(c)['failures'] == 1
    assert c._judge_kimi('draft', 'medium', 'system') == verdict
    assert row(c)['failures'] == 0
    assert c._judge_kimi('draft', 'medium', 'system') is None
    assert row(c)['failures'] == 1 and c._provider_available('kimi')


def test_missing_credentials_do_not_count_as_valid_schema_or_reset(critic, monkeypatch):
    c = critic
    c._record_provider_failure('kimi', 'network')
    monkeypatch.delenv('KIMI_API_KEY')
    monkeypatch.delenv('KIMI_CODING_API_KEY', raising=False)
    assert c._judge_kimi('draft', 'max', 'system') is None
    assert row(c)['failures'] == 1


def test_identity_scopes_provider_model_base_and_profile(critic, tmp_path):
    c = critic
    c._cool_provider('kimi')
    assert not c._provider_available('kimi')
    assert c._provider_available('openrouter-fallback')
    old_model, old_url = c._judge_model, c._judge_base_url
    c._judge_model = 'different-model'
    assert c._provider_available('kimi')
    c._judge_model = old_model
    c._judge_base_url = 'https://other-private-origin.invalid/v1'
    assert c._provider_available('kimi')
    c._judge_base_url = old_url
    assert not c._provider_available('kimi')
    other = load('other_profile_cache')
    register(other, FakeState(tmp_path / 'other-profile'))
    assert other._provider_available('kimi')


def test_configurable_threshold_and_cooldown_bounds(critic):
    c = critic
    register(c, c._provider_state, {'provider_failure_threshold': 3,
             'provider_cooldown_seconds': 10, 'provider_max_cooldown_seconds': 30})
    for _ in range(2):
        c._record_provider_failure('kimi', 'schema')
        assert c._provider_available('kimi')
    c._record_provider_failure('kimi', 'schema')
    assert not c._provider_available('kimi')
    assert 9 < row(c)['until'] - time.time() <= 10
    register(c, c._provider_state, {'provider_failure_threshold': True,
             'provider_cooldown_seconds': float('nan'), 'provider_max_cooldown_seconds': float('inf')})
    assert c._provider_failure_threshold == 2
    assert c._provider_cooldown_seconds == 300
    assert c._provider_max_cooldown_seconds == 3600


def test_cache_update_atomic_across_parallel_sessions_and_reload_modules(critic):
    first = critic
    second = load('simultaneous_reloaded_cache')
    register(second, first._provider_state)
    first._provider_failure_threshold = second._provider_failure_threshold = 100
    # Separate module locks cannot alone serialize this fake facade's get+set.
    # Shared advisory lock must preserve every increment across reloads/sessions.
    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(lambda i: (first if i % 2 else second)._record_provider_failure('kimi', 'network'), range(60)))
    assert row(first)['failures'] == 60
    assert first._provider_available('kimi')


def test_expired_open_circuit_leases_only_one_probe_not_healthy_provider_capacity(critic, monkeypatch):
    c = critic
    clock = [1_000_000.0]
    monkeypatch.setattr(c.time, 'time', lambda: clock[0])
    assert all(c._begin_provider('kimi') for _ in range(12))  # healthy sessions remain parallel
    c._cool_provider('kimi')
    clock[0] += 300
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: c._begin_provider('kimi'), range(12)))
    assert results.count(True) == 1
    c._record_provider_success('kimi')
    assert c._provider_available('kimi')


@pytest.mark.parametrize('late_result', [wire(NEGATIVE), httpx.Response(503)])
def test_late_worker_cannot_reset_or_increment_persistent_cache(critic, monkeypatch, late_result):
    c = critic
    c._record_provider_failure('kimi', 'network')
    c._review_budget_seconds = .04
    c._fallback_enabled = False
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    def post(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return late_result
    client.post = post
    monkeypatch.setattr(c.httpx, 'Client', lambda **kw: client)
    original = c._judge_kimi
    def provider(*args):
        try:
            return original(*args)
        finally:
            finished.set()
    c._judge_kimi = provider
    try:
        assert c._judge('draft', 'max', 'system') == (None, 'none')
        assert started.is_set()
        release.set()
        assert finished.wait(2)
        assert row(c)['failures'] == 1
        assert not c._turn_feedback
    finally:
        release.set()


@pytest.mark.parametrize('choice', [
    {'message': {'refusal': 'PRIVATE_REFUSAL', 'content': None}},
    {'message': {'content': ''}, 'finish_reason': 'content_filter'},
])
def test_safety_refusal_not_counted_as_technical_outage_or_bypassed(critic, monkeypatch, choice, caplog):
    c = critic
    c._record_provider_failure('kimi', 'network')
    post = transport(c, monkeypatch, [httpx.Response(200, json={'choices': [choice]})])
    verdict, provider = c._judge('draft', 'medium', 'system')
    assert provider == 'kimi' and verdict['passed'] is False
    assert 'Do not switch' in verdict['feedback']
    assert row(c)['failures'] == 1  # a structured refusal is not a schema verdict reset
    assert post.call_count == 1
    assert 'PRIVATE_REFUSAL' not in caplog.text


def test_real_plugin_manager_state_survives_forced_reload_and_is_profile_scoped(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    home = tmp_path / 'home'
    target = home / 'plugins' / 'response-critic'
    target.mkdir(parents=True)
    for name in ('__init__.py', 'plugin.yaml'):
        shutil.copy2(PLUGIN / name, target / name)
    (home / 'config.yaml').write_text('plugins:\n  enabled: [response-critic]\n')
    token = set_hermes_home_override(home)
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        first = manager._plugins['response-critic'].module
        assert first is not None and first._provider_state is not None
        first._cool_provider('kimi')
        state_path = first._provider_state.path
        saved = json.loads(state_path.read_text())
        assert saved[first._FAILURE_STATE_KEY][first._provider_key('kimi')]['failures'] == 1
        manager.discover_and_load(force=True)
        reloaded = manager._plugins['response-critic'].module
        assert reloaded is not first
        assert not reloaded._provider_available('kimi')
        assert reloaded._provider_state.path == state_path
        profile_token = set_hermes_home_override(tmp_path / 'second-home')
        try:
            assert reloaded._provider_available('kimi')
            assert reloaded._provider_state.path != state_path
        finally:
            reset_hermes_home_override(profile_token)
        assert not reloaded._provider_available('kimi')
    finally:
        manager.unload()
        reset_hermes_home_override(token)


@pytest.mark.parametrize('failure_method', ['get', 'set'])
def test_unavailable_durable_state_retains_safe_profile_local_memory_circuit(critic, monkeypatch, failure_method, caplog):
    c = critic
    method = getattr(c._provider_state, failure_method)
    monkeypatch.setattr(c._provider_state, failure_method, Mock(side_effect=RuntimeError('PRIVATE_STATE_EXCEPTION')))
    c._cool_provider('kimi')
    assert not c._provider_available('kimi')
    assert 'PRIVATE_STATE_EXCEPTION' not in caplog.text
    assert 'provider cache' in caplog.text
    monkeypatch.setattr(c._provider_state, failure_method, method)
    assert not c._provider_available('kimi')  # fallback flushes when state recovers
    assert row(c)['failures'] == 1


@pytest.mark.parametrize('corrupt', [None, 'PRIVATE_CACHE_TEXT', {'until': float('inf')},
                                    {'until': -1}, {'until': time.time() + 100_000}])
def test_corrupt_cache_entry_cannot_cool_down_forever(critic, corrupt):
    c = critic
    c._provider_state.set(c._FAILURE_STATE_KEY, {c._provider_key('kimi'): corrupt})
    assert c._provider_available('kimi')
    c._record_provider_success('kimi')
    assert row(c)['failures'] == 0


def test_provider_identity_retention_is_bounded(critic):
    c = critic
    for i in range(150):
        c._judge_model = 'offline-model-' + str(i)
        c._record_provider_failure('kimi', 'network')
    assert len(c._provider_state.get(c._FAILURE_STATE_KEY)) == 128


@pytest.mark.parametrize('count', [5, 12])
def test_parallel_active_required_effort_http_and_feedback_are_session_isolated(critic, monkeypatch, count):
    c = critic
    c._outcome_review_enabled = True
    c._outcome_review_mode = 'active'
    c._fallback_enabled = False
    c._conversation_from_frames = lambda: []
    sessions = ['session-' + str(i) for i in range(count)]
    choices = {sid: ['medium', 'high', 'max'][i % 3] for i, sid in enumerate(sessions)}
    def dispatch(name, args):
        sid = args['user_message']
        return {'ok': True, 'mode': 'active', 'review': {
            'scenario': 'ordinary', 'disposition': 'uncertain', 'confidence': .1,
            'feedback': '', 'acknowledgment': '', 'applied': False,
            'judge_required': True, 'judge_confidence': .99,
            'verifier_effort': choices[sid],
        }}
    c._plugin_context.has_plugin = lambda name: True
    c._plugin_context.dispatch_tool = dispatch
    barrier = threading.Barrier(count + 1)
    release = threading.Event()
    seen = {}
    lock = threading.Lock()
    def post(url, headers, json):
        sid = c._review_session.get()
        with lock:
            seen[sid] = json['reasoning_effort']
        barrier.wait(timeout=3)
        assert release.wait(3)
        return wire({'passed': False, 'feedback': 'Offline correction for ' + sid})
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.post = post
    monkeypatch.setattr(c.httpx, 'Client', lambda **kw: client)
    for sid in sessions:
        c.capture_turn_context(session_id=sid, user_message=sid)
    with ThreadPoolExecutor(max_workers=count) as pool:
        jobs = {sid: pool.submit(c.validate_final_response, 'Completed task for ' + sid, session_id=sid)
                for sid in sessions}
        try:
            barrier.wait(timeout=3)
            assert seen == {sid: c.KIMI_EFFORT_MAP[choices[sid]] for sid in sessions}
            release.set()
            for sid, job in jobs.items():
                result = job.result(timeout=3)
                assert 'Offline correction for ' + sid in result['message']
                assert c._turn_feedback[sid] == ['Offline correction for ' + sid]
            assert not c._review_workers
            assert row(c)['failures'] == 0
        finally:
            release.set()
            barrier.abort()


@pytest.mark.parametrize('minimum,maximum,expected_min,expected_max', [
    ('low', 'xhigh', 'medium', 'max'),
    ('minimal', 'max', 'medium', 'max'),
    ('medium', 'max', 'medium', 'max'),
    ('unknown', 'none', 'max', 'max'),
])
def test_legacy_config_cannot_request_below_medium_or_above_canonical_max(critic, minimum, maximum, expected_min, expected_max):
    c = critic
    register(c, c._provider_state, {'min_effort': minimum, 'max_effort': maximum})
    assert c._min_effort == expected_min and c._max_effort == expected_max
    assert c._pick_effort('Short draft', 0, False, []) in {'medium', 'high', 'max'}


@pytest.mark.parametrize('operation', ['failure', 'success', 'begin'])
def test_expired_deadline_blocks_cache_or_transport_admission(critic, operation):
    c = critic
    c._record_provider_failure('kimi', 'network')
    token = c._judge_deadline.set(time.monotonic() - 1)
    try:
        if operation == 'failure':
            c._record_provider_failure('kimi', 'network')
        elif operation == 'success':
            c._record_provider_success('kimi')
        else:
            assert c._begin_provider('kimi') is False
        assert row(c)['failures'] == 1
    finally:
        c._judge_deadline.reset(token)
