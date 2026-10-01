"""Paired policy regressions: explicit synthetic Jev responses, no live APIs."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('effort', ['medium', 'high', 'max'])
@pytest.mark.parametrize('weak_dimension', ['outcome', 'verdict', 'none'])
def test_required_effort_survives_real_scenario_abstention(monkeypatch, effort, weak_dimension):
    router = load('paired_router', ROOT / 'scenario-router' / '__init__.py')
    critic = load('paired_critic', ROOT / 'response-critic' / '__init__.py')
    choices = {'outcome': 'normal_answer', 'verdict': 'ready',
               'memory_evidence': 'not_applicable', 'judge_required': 'required',
               'judge_effort': effort}
    if weak_dimension == 'none':
        choices['outcome'] = 'technical_failure'
    answers = {}
    for name, label in choices.items():
        criteria = router.QUESTIONS[name]['criteria']
        confidence = .4 if name == weak_dimension else .99
        answers[name] = {'type': 'choice', 'choice': label, 'confidence': confidence,
                         'probabilities': {k: float(k == label) for k in criteria}}
    state = {'user_message': 'Explain this supplied result.',
             'assistant_response': 'The supplied result follows from the checked evidence.',
             'evidence': 'Explicit synthetic unit-test evidence.',
             'internal': False, 'pending_background': False}
    cfg = {**router.DEFAULTS, 'mode': 'active'}
    decision = router.review_envelope({'answers': answers}, state, cfg)
    assert decision['ok'] is (weak_dimension == 'none')
    if weak_dimension == 'none':
        assert decision['review']['disposition'] == 'recover'
    assert decision['review']['judge_required'] is True
    assert decision['review']['verifier_effort'] == effort
    critic._plugin_context = SimpleNamespace(has_plugin=lambda _: True,
                                            dispatch_tool=lambda *_: json.dumps(decision))
    critic._outcome_review_enabled = True
    critic._outcome_review_mode = 'active'
    critic._critic_mode = 'active'
    monkeypatch.setattr(critic, '_ensure_logging', lambda: None)
    monkeypatch.setattr(critic, '_conversation_from_frames', lambda: [])
    monkeypatch.setattr(critic, '_background_pending', lambda *_: False)
    judge = Mock(return_value=({'passed': True, 'feedback': ''}, 'synthetic-judge'))
    monkeypatch.setattr(critic, '_judge', judge)
    critic.capture_turn_context(session_id='paired', turn_id='t', user_message=state['user_message'])
    assert critic.validate_final_response(state['assistant_response'], session_id='paired') is None
    judge.assert_called_once()
    assert judge.call_args.args[1] == effort
