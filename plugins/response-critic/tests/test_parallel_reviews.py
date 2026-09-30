"""Parallel execution regressions: synthetic providers, real hook callback."""
import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock
import pytest


def load(name='parallel_critic'):
    spec=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[1]/'__init__.py')
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    c._ensure_logging=lambda:None
    c._triage_enabled=False
    c._conversation_from_frames=lambda:[]
    c._evidence_appendix=lambda **kw:''
    return c


@pytest.mark.parametrize('count',[5,12])
def test_full_verifier_callbacks_run_concurrently_with_isolated_feedback(count):
    c=load();barrier=threading.Barrier(count+1);seen={};lock=threading.Lock();release=threading.Event()
    def provider(message,effort,system):
        sid=c._review_session.get()
        with lock:seen[sid]=c._judge_deadline.get()
        barrier.wait(timeout=3)
        assert release.wait(3)
        return {'passed':False,'feedback':'Synthetic correction for '+sid}
    c._judge_kimi=provider;c._fallback_enabled=False
    sessions=['chat-'+str(i) for i in range(count)]
    for sid in sessions:
        c.capture_turn_context(session_id=sid,turn_id='turn-'+sid,user_message='Task for '+sid)
    pool=ThreadPoolExecutor(max_workers=count)
    try:
        jobs={sid:pool.submit(c.validate_final_response,'A completed nontrivial draft for '+sid,session_id=sid) for sid in sessions}
        barrier.wait(timeout=3)  # All provider calls must be active BEFORE any completes.
        assert set(seen)==set(sessions)
        assert len(c._review_workers)==count
        release.set()
        for sid,job in jobs.items():
            reply=job.result(timeout=3)
            assert reply['action']=='continue'
            assert 'Synthetic correction for '+sid in reply['message']
            assert c._turn_feedback[sid]==['Synthetic correction for '+sid]
        assert c._review_workers=={}
        assert c._review_session.get() is None
        assert all(deadline>time.monotonic() for deadline in seen.values())
    finally:
        release.set();barrier.abort();pool.shutdown(wait=True)


def test_missing_session_ids_do_not_share_a_default_capacity_bucket():
    c=load();barrier=threading.Barrier(6)
    def provider(*args):
        barrier.wait(timeout=3)
        return {'passed':True,'feedback':''}
    c._judge_kimi=provider;c._fallback_enabled=False
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs=[pool.submit(c._judge,'draft','max','system') for _ in range(5)]
        try:
            barrier.wait(timeout=3)
            assert all(job.result(timeout=3)[1]=='kimi' for job in jobs)
            assert not c._review_workers
        finally:barrier.abort()


def test_profile_modules_do_not_share_session_worker_registry():
    first,second=load('first_profile'),load('second_profile')
    first._review_workers[('same-chat','judge')]=object()
    second._judge_kimi=Mock(return_value={'passed':True,'feedback':''})
    token=second._review_session.set('same-chat')
    try:
        assert second._judge('draft','max','system')[1]=='kimi'
        assert second._review_workers=={}
        assert ('same-chat','judge') in first._review_workers
    finally:second._review_session.reset(token)


def test_judge_can_start_while_same_session_outcome_worker_is_abandoned():
    c=load();c._review_workers[('chat','outcome')]=object()
    c._judge_kimi=Mock(return_value={'passed':True,'feedback':''})
    token=c._review_session.set('chat')
    try:
        assert c._judge('draft','max','system')[1]=='kimi'
        assert set(c._review_workers)=={('chat','outcome')}
    finally:c._review_session.reset(token)


def test_five_cooperative_outcome_dispatches_have_independent_session_capacity():
    from types import SimpleNamespace
    c=load();barrier=threading.Barrier(6);seen=set();lock=threading.Lock()
    c._outcome_review_enabled=True;c._outcome_review_mode='active'
    def dispatch(name,args):
        assert name=='scenario_review_outcome'
        with lock:seen.add(args['user_message'])
        barrier.wait(timeout=3)
        return {'ok':True,'mode':'active','review':{'scenario':'normal_answer','disposition':'accept','confidence':1.0,'memory_verified':False,'feedback':'','verifier_effort':'max','applied':False,'acknowledgment':None}}
    c._plugin_context=SimpleNamespace(has_plugin=lambda name:True,dispatch_tool=dispatch)
    for i in range(5):
        c.capture_turn_context(session_id='chat-'+str(i),turn_id='turn-'+str(i),user_message='Task '+str(i))
    with ThreadPoolExecutor(max_workers=5) as pool:
        jobs=[pool.submit(c._review_outcome,'draft','chat-'+str(i),'evidence') for i in range(5)]
        try:
            barrier.wait(timeout=3)
            assert seen=={'Task '+str(i) for i in range(5)}
            for job in jobs:
                assert job.result(timeout=3)['review']['disposition']=='accept'
            assert not c._review_workers
        finally:barrier.abort()


def test_thread_start_failure_releases_only_its_own_session_key(monkeypatch):
    c=load();c._review_workers[('other-chat','judge')]=object()
    monkeypatch.setattr(c.threading.Thread,'start',Mock(side_effect=RuntimeError('synthetic start failure')))
    token=c._review_session.set('chat')
    try:
        assert c._judge('draft','max','system')==(None,'none')
        assert set(c._review_workers)=={('other-chat','judge')}
    finally:c._review_session.reset(token)
