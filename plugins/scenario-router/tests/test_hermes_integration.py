"""Real Hermes registration/dispatch in isolated homes; no live network or enablement."""
import json
import shutil
from pathlib import Path

import pytest


@pytest.mark.parametrize('mode', ['shadow', 'active', 'off'])
def test_real_plugin_manager_discovery_and_dispatch(tmp_path, monkeypatch, mode):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    plugin = Path(__file__).resolve().parents[1]
    home = tmp_path / 'home'; home.mkdir()
    target = home / 'plugins' / 'scenario-router'
    shutil.copytree(plugin, target, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    (home / 'config.yaml').write_text('plugins:\n  enabled: [scenario-router]\n  entries:\n    scenario-router:\n      settings:\n        mode: ' + json.dumps(mode) + '\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        loaded = manager._plugins['scenario-router']
        assert not loaded.error
        callback = next(cb for cb in manager._hooks['pre_llm_call'] if getattr(getattr(cb, '__self__', None), 'cfg', {}).get('judge_model') == 'typesafe/jev-1.13')
        reviewer = callback.__self__
        calls = []
        def decide(state):
            calls.append(state)
            # Explicit synthetic test response; never a live Jev result.
            labels = {'outcome': 'normal_answer', 'verdict': 'ready', 'memory_evidence': 'not_applicable'}
            # Questions imported via the plugin function's module before patching.
            return {'answers': {n: {'type': 'choice', 'choice': label, 'confidence': 1,
                                    'probabilities': {k: int(k == label) for k in q[n]['criteria']}}
                                for n, label in labels.items()}}
        q = reviewer.client.decide.__globals__['QUESTIONS']
        monkeypatch.setattr(reviewer.client, 'decide', decide)
        assert manager.invoke_hook('pre_llm_call', session_id='test-session', turn_id='test-turn', user_message='PRIVATE_REQUEST') == []
        assert calls == []  # No pre-run classifier call.
        manager.invoke_hook('post_tool_call', session_id='test-session', turn_id='test-turn', tool_name='terminal',
                            args={'command': 'test command'}, result='PRIVATE_TOOL_RESULT', status='ok')
        assert manager.invoke_hook('post_llm_call', session_id='test-session', turn_id='test-turn', assistant_response='PRIVATE_RESPONSE') == []
        assert len(calls) == (1 if mode == 'shadow' else 0)
        entry = registry.get_entry('scenario_review_outcome', scope=str(home))
        assert entry is not None
        # Cooperative verifier's public interface, through actual PluginContext dispatch.
        result = json.loads(reviewer.ctx.dispatch_tool('scenario_review_outcome', {
            'user_message': 'PRIVATE_REQUEST', 'assistant_response': 'PRIVATE_RESPONSE',
            'evidence': 'PRIVATE_TOOL_RESULT', 'internal': False, 'pending_background': False}))
        assert result['mode'] == mode
        assert result['ok'] is (mode != 'off')
        assert result['review']['disposition'] == ('uncertain' if mode == 'off' else 'accept')
        assert result['review']['applied'] is False and result['review']['verifier_effort'] == 'max'
        record = reviewer.ctx.state.get('last_decision')
        assert record['source'] == 'tool'
        serialized = json.dumps(record) + json.dumps(reviewer.ctx.state.get('review_history'))
        assert not any(s in serialized for s in ['PRIVATE_REQUEST', 'PRIVATE_RESPONSE', 'PRIVATE_TOOL_RESULT'])
    finally:
        manager.unload()
    assert registry.get_entry('scenario_review_outcome', scope=str(home)) is None


def test_disabled_plugin_not_registered(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry
    home = tmp_path / 'home'; home.mkdir()
    target = home / 'plugins' / 'scenario-router'
    shutil.copytree(Path(__file__).resolve().parents[1], target, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    (home / 'config.yaml').write_text('plugins:\n  enabled: []\n')
    monkeypatch.setenv('HERMES_HOME', str(home))
    manager = PluginManager(scope_key=str(home))
    try:
        manager.discover_and_load()
        assert registry.get_entry('scenario_review_outcome', scope=str(home)) is None
        assert not manager._hooks.get('pre_llm_call')
    finally:
        manager.unload()
