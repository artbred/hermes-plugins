"""Regression probes for human provenance and durable unload boundaries."""
import copy
import json
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_collector import human_before, collector, drain, m, payload, row


@pytest.mark.parametrize('extras', [
    {}, {'platform': None}, {'platform': 'cron'}, {'platform': 'subagent'},
    {'platform': 'cli'}, {'platform': 'api_server'}, {'platform': 'webhook'},
    {'platform': 'telegram', 'source': 'cron'},
    {'platform': 'telegram', 'background': True},
    {'platform': 'telegram', 'is_background': True},
    {'platform': 'telegram', 'is_subagent': True},
    {'platform': 'telegram', 'display_metadata': {'parent_session_id': 'parent'}},
    {'platform': 'telegram', 'display_metadata': {'background': True}},
    {'platform': 'telegram', 'display_metadata': '{"internal":true}'},
    {'platform': 'telegram', 'synthetic': True},
    {'platform': 'telegram', 'display_metadata': {'synthetic': True}},
])
def test_unknown_machine_and_synthetic_origins_abstain(collector, extras):
    c = collector(mode='shadow')
    history = [{'role': 'user', 'content': 'human-shaped fixture', 'platform_message_id': 'fixture-inbound'}]
    assert c.before(session_id='fixture-s', turn_id='fixture-t', user_message='human-shaped fixture',
                    conversation_history=history, sender_id='fixture-human', **extras) is None
    drain(c)
    assert c.metrics['captured'] == 0
    assert c.pool is None and not c.turns


@pytest.mark.parametrize('metadata', [
    {'synthetic': True}, {'parent_session_id': 'parent'}, {'background': True},
    {'is_subagent': True}, '{"internal":true}',
])
def test_current_row_machine_or_unknown_metadata_abstains(collector, metadata):
    c = collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s', turn_id='t', user_message='fixture',
             conversation_history=[{'role': 'user', 'content': 'fixture', 'display_metadata': metadata}])
    drain(c)
    assert c.metrics['captured'] == 0 and c.pool is None


def test_trusted_human_quotation_keeps_model_and_redacts(collector):
    c = collector(mode='shadow')
    request = 'Quote [INTERNAL NOTIFICATION] and synthetic=true literally'
    human_before(c, platform='telegram', session_id='s', turn_id='t', user_message=request,
             model='fixture-model password=private-model-secret',
             conversation_history=[{'role': 'user', 'content': request}])
    drain(c)
    stored, outcome = row(c)
    assert stored['request'] == request and not stored['synthetic']
    assert stored['provenance'] == 'live:authenticated_telegram_pre_llm'
    assert stored['actual']['model'] == 'fixture-model password=[REDACTED]'
    assert stored['actual']['wire_effort'] == 'unknown' and outcome == 'unknown'


def test_explicit_debug_capture_is_synthetic_and_never_a_default_example(collector):
    c = collector(mode='shadow', allow_synthetic_capture=True)
    states = []
    c.client = SimpleNamespace(decide=lambda state: states.append(copy.deepcopy(state)) or payload())
    c.before(platform='synthetic_debug', synthetic=True, session_id='s', turn_id='debug', user_message='common fixture')
    drain(c)
    stored, _ = row(c)
    assert stored['synthetic'] is True and stored['provenance'] == 'live:synthetic_debug'
    human_before(c, platform='telegram', session_id='s', turn_id='human', user_message='common fixture')
    drain(c)
    assert states[1]['previous_examples'] == []
    assert len(c.pool.similar('common fixture', time.time(), '', include_synthetic=True)) == 2


@pytest.mark.parametrize('extras', [
    {'platform': 'synthetic_debug'}, {'platform': 'telegram', 'synthetic': True},
    {'platform': 'synthetic_debug', 'synthetic': True, 'background': True},
])
def test_debug_optin_does_not_bypass_other_origin_guards(collector, extras):
    c = collector(mode='shadow', allow_synthetic_capture=True)
    c.before(session_id='s', turn_id='t', user_message='fixture', **extras)
    drain(c)
    assert c.pool is None


def test_unload_while_insert_entered_discards_record(collector, monkeypatch):
    c = collector(mode='shadow')
    entered, release = threading.Event(), threading.Event()
    original = m.Pool.insert
    def paused(pool, record):
        entered.set()
        assert release.wait(3)
        return original(pool, record)
    monkeypatch.setattr(m.Pool, 'insert', paused)
    human_before(c, platform='telegram', session_id='s', turn_id='t', user_message='fixture')
    assert entered.wait(2)
    start = time.monotonic()
    c.close()
    assert time.monotonic() - start < .2
    release.set()
    c.worker.join(2)
    assert not c.worker.is_alive()
    with sqlite3.connect(c.home / 'reasoning-shadow' / 'pool.db') as db:
        assert db.execute('SELECT COUNT(*) FROM examples').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM annotation_history').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM requests_fts').fetchone()[0] == 0


def test_unload_before_lazy_pool_initialization_creates_nothing(collector, monkeypatch):
    c = collector(mode='shadow')
    entered, release = threading.Event(), threading.Event()
    original = c._get_pool
    def paused():
        entered.set()
        assert release.wait(3)
        return original()
    monkeypatch.setattr(c, '_get_pool', paused)
    human_before(c, platform='telegram', session_id='s', turn_id='t', user_message='fixture')
    assert entered.wait(2)
    c.close()
    release.set()
    c.worker.join(2)
    assert not c.worker.is_alive() and not (c.home / 'reasoning-shadow').exists()


@pytest.mark.parametrize('operation', ['insert', 'update', 'failure_set'])
def test_unload_during_transaction_rolls_back_all_writes(collector, monkeypatch, operation):
    c = collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s', turn_id='initial', user_message='initial fixture')
    drain(c)
    path = c.pool.path
    def snapshot():
        with sqlite3.connect(path) as db:
            return [db.execute('SELECT * FROM ' + table).fetchall()
                    for table in ('examples', 'requests_fts', 'annotation_history', 'metadata')]
    before = snapshot()
    entered, release = threading.Event(), threading.Event()
    original = c.pool._connection
    @contextmanager
    def paused(*args, **kwargs):
        with original(*args, **kwargs) as db:
            yield db
            entered.set()
            assert release.wait(3)
    monkeypatch.setattr(c.pool, '_connection', paused)
    errors = []
    now = time.time()
    def write():
        try:
            if operation == 'insert':
                c.pool.insert(dict(source_handle='fixture:late', captured_at=now, source_at=now, request='late fixture'))
            elif operation == 'update':
                c.pool.update(row(c)[0]['source_handle'], {'status': 'failed'})
            else:
                c.pool.failure_set({'count': 4, 'until': now + 100})
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=write)
    thread.start()
    assert entered.wait(2)
    start = time.monotonic()
    c.close()
    assert time.monotonic() - start < .2
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == ['pool_cancelled']
    assert snapshot() == before


@pytest.mark.parametrize('sender, history', [
    ('fixture-human', []), ('fixture-human', [{'role': 'user', 'content': 'fixture'}]),
    ('', [{'role': 'user', 'content': 'fixture', 'platform_message_id': 'inbound'}]),
    (None, [{'role': 'user', 'content': 'fixture', 'platform_message_id': 'inbound'}]),
])
def test_telegram_without_positive_current_input_evidence_abstains(collector, sender, history):
    c = collector(mode='shadow')
    c.before(platform='telegram', session_id='s', turn_id='t', sender_id=sender,
             user_message='fixture', conversation_history=history)
    drain(c)
    assert c.pool is None and c.metrics['captured'] == 0


def test_real_host_generated_goal_event_has_no_human_origin(collector, monkeypatch):
    from agent.turn_context import _stage_turn_user_message, _collect_pre_llm_call_context
    from gateway.run_goals import GatewayGoalsMixin
    from gateway.session import SessionSource
    from gateway.config import Platform
    import hermes_cli.lifecycle as lifecycle
    c = collector(mode='shadow')
    source = SessionSource(platform=Platform.TELEGRAM, chat_id='fixture-chat',
                           user_id='fixture-human', message_id='original-inbound')
    event = GatewayGoalsMixin._synthetic_prompt_event(source, 'generated goal continuation')
    assert not event.internal and event.source.message_id is None
    agent = SimpleNamespace(session_id='s', model='fixture-model', platform='telegram',
                            _user_id='fixture-human', _parent_session_id=None)
    current, _ = _stage_turn_user_message(agent, event.text, None, None,
                                          event.source.message_id, None, None)
    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda name, **kwargs: c.before(**kwargs) or [])
    assert _collect_pre_llm_call_context(agent, effective_task_id='task', turn_id='t',
                                         original_user_message=event.text, messages=[current],
                                         conversation_history=[]) == ''
    drain(c)
    assert c.metrics['captured'] == 0 and c.pool is None


def test_cancelled_initialization_cannot_switch_journal_mode(tmp_path, monkeypatch):
    directory = tmp_path / 'reasoning-shadow'
    directory.mkdir()
    path = directory / 'pool.db'
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    lifecycle = m.WriteLifecycle()
    entered, release = threading.Event(), threading.Event()
    original = m.Pool._initialize_journal
    def paused(pool, db):
        entered.set()
        assert release.wait(3)
        return original(pool, db)
    monkeypatch.setattr(m.Pool, '_initialize_journal', paused)
    errors = []
    def initialize():
        try:
            m.Pool(tmp_path, lifecycle=lifecycle)
        except RuntimeError as error:
            errors.append(str(error))
    thread = threading.Thread(target=initialize)
    thread.start()
    assert entered.wait(2)
    lifecycle.cancel()
    release.set()
    thread.join(2)
    assert not thread.is_alive() and errors == ['pool_cancelled']
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
        assert db.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()[0] == 0


def test_real_host_human_input_keeps_positive_origin(collector, monkeypatch):
    from agent.turn_context import _stage_turn_user_message, _collect_pre_llm_call_context
    import hermes_cli.lifecycle as lifecycle
    c = collector(mode='shadow')
    agent = SimpleNamespace(session_id='fixture-s', model='fixture-model', platform='telegram',
                            _user_id='fixture-human', _parent_session_id=None)
    request = 'human fixture quotes [INTERNAL NOTIFICATION]'
    current, _ = _stage_turn_user_message(agent, request, None, None, 'fixture-inbound', None, None)
    original = copy.deepcopy(current)
    monkeypatch.setattr(lifecycle, 'invoke_hook', lambda name, **kwargs: c.before(**kwargs) or [])
    assert _collect_pre_llm_call_context(agent, effective_task_id='task', turn_id='fixture-t',
                                         original_user_message=request, messages=[current],
                                         conversation_history=[]) == ''
    drain(c)
    stored, _ = row(c)
    assert current == original and stored['request'] == request
    assert stored['provenance'] == 'live:authenticated_telegram_pre_llm'
    assert stored['actual']['model'] == 'fixture-model' and not stored['synthetic']
    assert 'fixture-human' not in json.dumps(stored) and 'fixture-inbound' not in json.dumps(stored)


def test_journal_initialization_busy_lock_fails_without_unload_wait(tmp_path):
    directory = tmp_path / 'reasoning-shadow'
    directory.mkdir()
    path = directory / 'pool.db'
    lock = sqlite3.connect(path)
    try:
        lock.execute('BEGIN EXCLUSIVE')
        lifecycle = m.WriteLifecycle()
        errors = []
        def initialize():
            try:
                m.Pool(tmp_path, lifecycle=lifecycle)
            except sqlite3.OperationalError:
                errors.append('busy')
        thread = threading.Thread(target=initialize)
        start = time.monotonic()
        thread.start()
        thread.join(.5)
        assert not thread.is_alive() and errors == ['busy']
        lifecycle.cancel()
        assert time.monotonic() - start < .5
    finally:
        lock.rollback()
        lock.close()


def test_real_manager_provenance_and_synthetic_debug(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_discovery import scan_directory
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(tmp_path)
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    destination = tmp_path / 'plugins' / 'reasoning-shadow'
    shutil.copytree(Path(__file__).resolve().parents[1], destination,
                    ignore=shutil.ignore_patterns('tests', '__pycache__'))
    (tmp_path / 'config.yaml').write_text(
        'plugins:\n  enabled: [reasoning-shadow]\n  entries:\n    reasoning-shadow:\n'
        '      settings:\n        mode: shadow\n        allow_synthetic_capture: true\n', encoding='utf-8')
    manager = PluginManager(scope_key=str(tmp_path))
    monkeypatch.setattr(manager, '_collect_directory_manifests', lambda: scan_directory(tmp_path / 'plugins', 'user'))
    monkeypatch.setattr(manager, '_scan_entry_points', lambda: [])
    try:
        manager.discover_and_load()
        loaded = manager._plugins['reasoning-shadow']
        assert loaded.enabled and not loaded.error
        c = manager._hooks['pre_llm_call'][0].__self__
        c.client = SimpleNamespace(decide=lambda state: payload())
        base = dict(session_id='fixture-s', turn_id='fixture-t', user_message='public fixture',
                    sender_id='fixture-human', conversation_history=[{'role':'user', 'content':'public fixture',
                                                                     'platform_message_id':'fixture-inbound'}])
        for extras in ({}, {'platform': 'cron'}, {'platform': 'api_server'},
                       {'platform': 'telegram', 'synthetic': True}):
            assert manager.invoke_hook('pre_llm_call', **base, **extras) == []
        drain(c)
        assert c.pool is None
        assert manager.invoke_hook('pre_llm_call', **base, platform='synthetic_debug', synthetic=True) == []
        drain(c)
        assert row(c)[0]['synthetic'] is True
        assert row(c)[0]['provenance'] == 'live:synthetic_debug'
        assert manager.unload('reasoning-shadow') and c.closed.is_set()
    finally:
        manager.unload()
        reset_hermes_home_override(token)
