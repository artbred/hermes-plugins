"""Asynchronous shadow review must never hold response delivery."""
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def setup():
    s=importlib.util.spec_from_file_location('async_shadow_test',Path(__file__).resolve().parents[1]/'__init__.py')
    r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
    data={}
    c=SimpleNamespace(get_config=lambda k,d:d,state=SimpleNamespace(set=lambda k,v:data.__setitem__(k,v)))
    return r,r.Reviewer(c),data


def test_delivery_hook_returns_while_network_is_blocked():
    r,reviewer,data=setup();release=threading.Event();started=threading.Event()
    def blocked(state):
        started.set();release.wait(2)
        return {'error':'synthetic_timeout'}
    reviewer.client.decide=blocked
    reviewer.before(session_id='s',turn_id='t',user_message='Original full-agent request')
    t=time.monotonic()
    try:
        assert reviewer.after(session_id='s',turn_id='t',assistant_response='Unchanged reply') is None
        assert time.monotonic()-t < .1
        assert started.wait(1)
        assert data=={}
    finally:
        release.set();reviewer.shadow_queue.join();reviewer.close()
    assert data['last_decision']['review']['applied'] is False


def test_unload_discards_late_network_result():
    r,reviewer,data=setup();release=threading.Event();started=threading.Event()
    reviewer.client.decide=lambda state:(started.set(),release.wait(2),{'error':'synthetic_timeout'})[-1]
    reviewer.before(session_id='s',turn_id='t',user_message='request')
    reviewer.after(session_id='s',turn_id='t',assistant_response='reply')
    assert started.wait(1)
    reviewer.close();release.set();reviewer.shadow_queue.join()
    assert data=={}


def test_queue_is_bounded_and_overflow_does_not_start_more_workers():
    r,reviewer,data=setup();reviewer.shadow_worker=object()
    state={'user_message':'test','assistant_response':'test','evidence':'','internal':False,'pending_background':False}
    for _ in range(40):reviewer.enqueue_shadow(state)
    assert reviewer.shadow_queue.qsize()==r.DEFAULTS['shadow_queue_size']
    reviewer.close();assert reviewer.shadow_queue.empty()


def test_no_duplicate_subagent_observation():
    r,reviewer,data=setup();reviewer.client.decide=Mock()
    reviewer.before(session_id='child',turn_id='t',user_message='task',parent_session_id='main')
    reviewer.after(session_id='child',turn_id='t',assistant_response='child result')
    reviewer.client.decide.assert_not_called();assert reviewer.shadow_worker is None
    reviewer.close()


def test_closed_reviewer_does_not_queue_or_persist():
    r,reviewer,data=setup();reviewer.close()
    state={'user_message':'test','assistant_response':'test','evidence':'','internal':False,'pending_background':False}
    reviewer.enqueue_shadow(state)
    assert reviewer.shadow_queue.empty() and data=={}
