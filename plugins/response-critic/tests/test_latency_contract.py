"""Latency/noninterference regressions; API decisions are synthetic fixtures."""
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def load():
    s=importlib.util.spec_from_file_location('critic_latency_test',Path(__file__).resolve().parents[1]/'__init__.py')
    c=importlib.util.module_from_spec(s);s.loader.exec_module(c)
    c._ensure_logging=lambda:None
    return c


@pytest.mark.parametrize('mode',['off','shadow'])
def test_observer_modes_do_not_mutate_default_verify_gate(mode):
    c=load();c._critic_mode=mode
    agent=SimpleNamespace(_turn_file_mutation_paths={'original.py'})
    c._get_agent=lambda:agent
    c._open_verify_gate()
    assert agent._turn_file_mutation_paths=={'original.py'}
    c._judge=Mock()
    assert c.validate_final_response('A response long enough for legacy review.',session_id='s') is None
    c._judge.assert_not_called()


def test_shadow_does_not_dispatch_duplicate_outcome_review():
    c=load();c._outcome_review_enabled=True;c._outcome_review_mode='shadow'
    c._plugin_context=SimpleNamespace(dispatch_tool=Mock(),has_plugin=Mock(return_value=True))
    assert c._review_outcome('draft','s','evidence') is None
    c._plugin_context.dispatch_tool.assert_not_called()


def test_hard_deadline_returns_before_blocked_provider_and_ignores_late_rejection():
    c=load();c._review_budget_seconds=.05
    release=threading.Event();started=threading.Event()
    def blocked(*args):
        started.set();release.wait(2)
        return {'passed':False,'feedback':'LATE_SYNTHETIC_REJECTION'}
    c._judge_kimi=blocked;c._fallback_enabled=False
    t=time.monotonic()
    try:
        assert c._judge('draft','max','system')==(None,'none')
        assert time.monotonic()-t < .3 and started.is_set()
        assert c._turn_feedback=={} and c._acknowledgments=={}
    finally:release.set()


def test_abandoned_worker_capacity_fails_open_without_more_threads():
    c=load();c._judge_slots.acquire();c._judge_slots.acquire()
    c._judge_kimi=Mock()
    t=time.monotonic()
    try:
        assert c._judge('draft','max','system')==(None,'none')
        assert time.monotonic()-t < .1
        c._judge_kimi.assert_not_called()
    finally:c._judge_slots.release();c._judge_slots.release()


def test_quota_cooldown_skips_exhausted_primary_without_lowering_fallback_effort():
    c=load();c._cool_provider('kimi')
    c._judge_kimi=Mock();c._judge_openrouter=Mock(return_value={'passed':True,'feedback':''})
    assert c._judge('draft','max','system')[1]=='openrouter-fallback'
    c._judge_kimi.assert_not_called()
    assert c._fallback_effort=='max'


def test_expired_budget_starts_no_provider():
    c=load();c._judge_kimi=Mock()
    token=c._judge_deadline.set(time.monotonic()-1)
    try:
        assert c._judge('draft','max','system')==(None,'none')
        c._judge_kimi.assert_not_called()
    finally:c._judge_deadline.reset(token)


def test_subagents_do_not_get_duplicate_main_critic_pass():
    c=load();c._turn_context['child']={'parent_session_id':'main'};c._judge=Mock()
    assert c.validate_final_response('Subagent result with execution claims.',session_id='child') is None
    c._judge.assert_not_called()


def test_production_single_correction_cap_is_effective():
    c=load();c._max_iterations=1;c._judge=Mock()
    assert c.validate_final_response('Corrected full-agent result.',session_id='s',attempt=1) is None
    c._judge.assert_not_called()
