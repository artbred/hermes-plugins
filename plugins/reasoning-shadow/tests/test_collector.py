"""All request/IDs/transport responses here are explicitly synthetic fixtures."""
import copy
import contextvars
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('synthetic_reasoning_shadow', ROOT / '__init__.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def payload(family='conversation', risk='low', effort='low', confidence=.99):
    labels=dict(family=family,risk=risk,effort=effort)
    return {'answers': {name: {'type':'choice','choice':labels[name],'confidence':confidence,
                               'probabilities':{label:float(label==labels[name]) for label in q['criteria']}}
                        for name,q in m.QUESTIONS.items()}}


@pytest.fixture
def collector(tmp_path,monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override
    token=set_hermes_home_override(tmp_path)
    instances=[]
    def make(**settings):
        ctx=SimpleNamespace(get_config=lambda key,default:settings.get(key,default),state={})
        c=m.Collector(ctx)
        c.client=SimpleNamespace(decide=lambda state:payload())
        instances.append(c)
        return c
    yield make
    for c in instances:
        c.close()
        if c.worker:
            c.worker.join(2)
    reset_hermes_home_override(token)


def human_before(c, **kwargs):
    """Explicit test-only host event. Production never fills missing provenance."""
    kwargs = copy.deepcopy(kwargs)
    kwargs['sender_id'] = 'fixture-human-sender'
    history = kwargs.get('conversation_history', [])
    current = next((item for item in reversed(history) if isinstance(item, dict) and item.get('role') == 'user'), None)
    if current is None:
        current = {'role': 'user', 'content': kwargs.get('user_message', '')}
        history.append(current)
    current['platform_message_id'] = 'fixture-inbound-' + str(kwargs.get('turn_id', ''))
    kwargs['conversation_history'] = history
    return c.before(**kwargs)


def drain(c):
    deadline=time.monotonic()+3
    while c.jobs.unfinished_tasks and time.monotonic()<deadline:
        time.sleep(.005)
    assert c.jobs.unfinished_tasks == 0


def row(c):
    with sqlite3.connect(c.home/'reasoning-shadow'/'pool.db') as db:
        found=db.execute('SELECT record,outcome FROM examples ORDER BY id DESC LIMIT 1').fetchone()
    return json.loads(found[0]),found[1]


def test_off_strong_nonmutation_no_pool(collector):
    c=collector()
    source={'reasoning':{'effort':'high'},'messages':[{'role':'user','content':'synthetic'}]}
    original=copy.deepcopy(source)
    for fn in (c.before,c.pre_api,c.post_api,c.tool,c.after,c.reset):
        assert fn(session_id='fixture-session',turn_id='fixture-turn',user_message='synthetic',request=source) is None
    assert source==original and c.pool is None and c.worker is None
    assert not (c.home/'reasoning-shadow').exists()


def test_async_snapshot_causality_and_context(collector):
    c=collector(mode='shadow')
    started,release=threading.Event(),threading.Event()
    captured=[]
    def decide(state):
        captured.append(copy.deepcopy(state)); started.set(); release.wait(2)
        return payload()
    c.client=SimpleNamespace(decide=decide)
    history=[{'role':'system','content':'never-system'}, {'role':'user','content':'previous user'},
             {'role':'assistant','content':'previous visible answer','reasoning_content':'never-hidden'},
             {'role':'user','content':'current synthetic request'}]
    original=copy.deepcopy(history)
    before=time.monotonic()
    assert human_before(c, platform='telegram', session_id='fixture-session',turn_id='fixture-turn',user_message='current synthetic request',conversation_history=history) is None
    assert time.monotonic()-before<.2
    assert started.wait(2)
    history[1]['content']='MUTATED_AFTER_CAPTURE'
    c.tool(session_id='fixture-session',turn_id='fixture-turn',tool_call_id='fixture-tool',args={'secret':'FUTURE_ARGS'},result='FUTURE_TOOL_OUTPUT')
    c.after(session_id='fixture-session',turn_id='fixture-turn',assistant_response='FUTURE_ASSISTANT_OUTPUT',critic_verdict='FUTURE_CRITIC')
    encoded=json.dumps(captured)
    assert 'never-system' not in encoded and 'never-hidden' not in encoded
    assert all(x not in encoded for x in ('FUTURE_ARGS','FUTURE_TOOL_OUTPUT','FUTURE_ASSISTANT_OUTPUT','FUTURE_CRITIC','MUTATED_AFTER_CAPTURE'))
    assert captured[0]['recent_context']==[{k:r[k] for k in ('role','content')} for r in original[1:3]]
    release.set(); drain(c)
    record,outcome=row(c)
    assert record['request']=='current synthetic request' and outcome=='unknown'
    assert record['prediction']['main_effort']=='low' and not record['prediction']['live_eligible']
    assert record['actual']['tool_calls']==1 and record['actual']['wire_effort']=='unknown'


@pytest.mark.parametrize('kind', ['internal_notification','internal'])
def test_internal_and_child_exclusion(collector,kind):
    c=collector(mode='shadow')
    for extras in ({'parent_session_id':'synthetic-parent'}, {'internal':True}, {'_verification_nudge':True},{'_pre_verify_hook_continue':True}):
        human_before(c, platform='telegram', session_id='fixture-session',turn_id='fixture-turn',user_message='machine',**extras)
    human_before(c, platform='telegram', session_id='fixture-session',turn_id='fixture-turn',user_message='machine',conversation_history=[{'role':'user','content':'machine','display_kind':kind}])
    assert c.metrics['captured']==0
    assert human_before(c, platform='telegram', session_id='fixture-session',turn_id='fixture-turn',user_message='Quote [INTERNAL NOTIFICATION] literally') is None
    drain(c); assert c.metrics['captured']==1


@pytest.mark.parametrize('sid,tid',[('', 't'),('s',''),(None,'t'),('s',None)])
def test_missing_ids_abstains(collector,sid,tid):
    c=collector(mode='shadow'); human_before(c, platform='telegram', session_id=sid,turn_id=tid,user_message='synthetic')
    assert not c.jobs.unfinished_tasks and not c.turns


def test_last_six_only_and_never_silent_request_truncation(collector):
    c=collector(mode='shadow')
    history=[{'role':'user' if i%2 else 'assistant','content':str(i)} for i in range(12)]
    history.append({'role':'user','content':'synthetic full request'})
    calls=[]; c.client=SimpleNamespace(decide=lambda s: calls.append(s) or payload())
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic full request',conversation_history=history)
    drain(c)
    assert calls[0]['recent_context']==history[-7:-1]
    assert calls[0]['user_message']=='synthetic full request'


@pytest.mark.parametrize('context', [False,True])
def test_oversize_diagnostic_not_partial_classification(collector,context):
    c=collector(mode='shadow',max_input_characters=200)
    prompt='synthetic small request' if context else 'x'*1000
    history=[{'role':'assistant','content':'z'*1000},{'role':'user','content':prompt}] if context else []
    calls=[]; c.client=SimpleNamespace(decide=lambda s:calls.append(s) or payload())
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message=prompt,conversation_history=history)
    drain(c)
    record,outcome=row(c)
    assert not calls and record['status']=='input_too_large' and record['request']=='' and record['recent_context']==[]
    assert record['request_hash']==m.digest(prompt) and outcome=='unknown'


def test_dedupe_epoch_after_end_and_compaction(collector):
    c=collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='changed later')
    c.pre_api(session_id='compacted-s',turn_id='t',request={'reasoning_effort':'high'},api_request_id='synthetic-api')
    c.after(session_id='compacted-s',turn_id='t')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='repeat after end')
    drain(c)
    assert c.pool.report()['total']==1 and row(c)[0]['actual']['wire_effort']=='high'


def test_ambiguous_compaction_does_not_mix_sessions(collector):
    c=collector(mode='shadow')
    # Genuine concurrent equal tids are distinguished by sid. Insert second
    # directly into map to exercise conservative ambiguous correlation.
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    with c.lock:
        c.turns[('other-s','t')]=copy.deepcopy(c.turns[('s','t')])
    c.post_api(session_id='rotated-s',turn_id='t',api_request_id='synthetic',api_duration=5)
    assert all(v['actual']['api_calls']==0 for v in c.turns.values())
    drain(c)


def test_actual_wire_effort_usage_metadata_dedup(collector):
    c=collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    request={'reasoning':{'effort':'xhigh'},'messages':[{'content':'NEVER_CAPTURE_API_BODY'}]}
    original=copy.deepcopy(request)
    for _ in range(2):
        c.pre_api(session_id='s',turn_id='t',request=request,model='synthetic-model',provider='synthetic',api_request_id='api-one')
        c.post_api(session_id='s',turn_id='t',api_request_id='api-one',api_duration=.25,usage={'prompt_tokens':10,'completion_tokens':5,'total_tokens':15},response={'reasoning':'NEVER_CAPTURE_RESPONSE'})
        c.tool(session_id='s',turn_id='t',tool_call_id='tool-one',status='failed',args={'anything':'DO_NOT_STORE'},result='DO_NOT_STORE')
    c.post_api(session_id='s',turn_id='t',api_request_id='api-two',api_duration=.5,usage={'input_tokens':3,'output_tokens':2,'total_tokens':5})
    c.after(session_id='s',turn_id='t',assistant_response='Added')
    drain(c)
    r,outcome=row(c); a=r['actual']
    assert a['wire_effort']=='xhigh' and a['api_calls']==2 and a['api_duration_seconds']==.75
    assert (a['input_tokens'],a['output_tokens'],a['total_tokens'])==(13,7,20)
    assert a['tool_calls']==1 and a['tool_errors']==1 and outcome=='unknown'
    assert request==original and all(x not in json.dumps(r) for x in ('DO_NOT_STORE','NEVER_CAPTURE','Added'))


def test_composite_event_dedupe_and_bound(collector):
    c=collector(mode='shadow',max_event_keys=2)
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    c.post_api(session_id='s',turn_id='t',api_call_count=1,started_at=1,api_duration=1)
    c.post_api(session_id='s',turn_id='t',api_call_count=1,started_at=1,api_duration=1)
    c.post_api(session_id='s',turn_id='t',api_call_count=2,started_at=2,api_duration=1)
    c.post_api(session_id='s',turn_id='t',api_call_count=3,started_at=3,api_duration=1)
    assert c.turns[('s','t')]['actual']['api_calls']==2
    assert c.turns[('s','t')]['actual']['observation_incomplete']
    drain(c)


def test_queue_bounded_and_hooks_nonblocking_under_network(collector):
    c=collector(mode='shadow',queue_size=2)
    started,release=threading.Event(),threading.Event()
    c.client=SimpleNamespace(decide=lambda s: started.set() or release.wait(2) or payload())
    human_before(c, platform='telegram', session_id='s',turn_id='one',user_message='synthetic one')
    assert started.wait(2)
    now=time.monotonic()
    for i in range(20):
        human_before(c, platform='telegram', session_id='s'+str(i),turn_id=str(i),user_message='synthetic')
    assert time.monotonic()-now<.3 and c.jobs.qsize()<=2 and c.metrics['dropped']>0
    release.set(); drain(c)


def test_late_network_return_discarded_and_close_does_not_join(collector):
    c=collector(mode='shadow')
    started,release=threading.Event(),threading.Event()
    def decide(s):
        started.set(); release.wait(3); return payload()
    c.client=SimpleNamespace(decide=decide)
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    assert started.wait(2)
    now=time.monotonic(); c.close(); assert time.monotonic()-now<.2
    release.set(); c.worker.join(2)
    assert row(c)[0]['status']=='pending'


def test_failure_cache_persists_profile_and_reload(collector):
    c=collector(mode='shadow')
    c.client=SimpleNamespace(decide=lambda s:{'error':'transport_error','body':'never-persist-secret'})
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic'); drain(c)
    f=c.pool.failure_get(); assert f['count']==1 and 29< f['until']-time.time()<=30
    c.close(); c.worker.join(2)
    second=collector(mode='shadow'); calls=[]
    second.client=SimpleNamespace(decide=lambda s:calls.append(s) or payload())
    human_before(second, platform='telegram', session_id='s',turn_id='next',user_message='synthetic'); drain(second)
    assert not calls and row(second)[0]['status']=='failure_cooldown'
    assert 'never-persist-secret' not in second.pool.path.read_bytes().decode('latin1')


def test_low_confidence_not_transport_failure(collector):
    c=collector(mode='shadow'); c.client=SimpleNamespace(decide=lambda s:payload(confidence=.4))
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic'); drain(c)
    assert c.pool.failure_get()=={} and row(c)[0]['prediction']['main_effort']=='high'


@pytest.mark.parametrize('mutator',[lambda p:p['answers'].pop('family'),lambda p:p['answers']['effort'].update(type='score'),
    lambda p:p['answers']['effort'].update(confidence=True),lambda p:p['answers']['risk'].update(confidence=float('nan')),
    lambda p:p['answers']['effort']['probabilities'].update(low=True),lambda p:p['answers']['family'].update(choice='unknown-label'),
    lambda p:p['answers']['effort']['probabilities'].update(low=.5)])
def test_strict_answer_validation(mutator):
    p=payload(); mutator(p); result=m.recommendation(p)
    assert result['status']=='invalid_response' and result['main_effort']=='high' and not result['answers']


@pytest.mark.parametrize('family,risk,effort,expected',[('conversation','low','low','low'),('technical_work','low','low','high'),
    ('technical_work','elevated','low','max'),('note_capture','uncertain','low','high'),('research','low','low','high')])
def test_conservative_recommendations(family,risk,effort,expected):
    r=m.recommendation(payload(family,risk,effort))
    assert r['main_effort']==expected and r['raw_effort']==effort and not r['live_eligible']


def test_typed_transport_redaction_and_no_raw_errors(collector,caplog):
    c=collector(mode='shadow')
    captured=[]
    def handle(req):
        captured.append((str(req.url),json.loads(req.content)))
        return httpx.Response(200,json=payload())
    c.client=m.Jev(c.cfg,'synthetic-key',httpx.MockTransport(handle))
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic token=synthetic-sensitive-value')
    drain(c)
    endpoint,state=captured[0]
    assert endpoint==m.ENDPOINT and state['model']=='typesafe/jev-1.13'
    assert set(state)=={'model','state','questions'} and 'synthetic-sensitive-value' not in json.dumps(state)
    assert not caplog.records


def test_contextvars_preserved(collector):
    c=collector(mode='shadow'); marker=contextvars.ContextVar('synthetic_marker',default='not-carried'); seen=[]
    c.client=SimpleNamespace(decide=lambda s:seen.append(marker.get()) or payload())
    token=marker.set('captured-context')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic'); marker.reset(token)
    drain(c); assert seen==['captured-context']


def test_old_jobs_no_network_but_diagnostic_retained(collector,monkeypatch):
    c=collector(mode='shadow',queue_ttl_seconds=1); calls=[]
    c.client=SimpleNamespace(decide=lambda s:calls.append(s) or payload())
    now=time.time()
    record={'source_handle':'synthetic:expired','request':'synthetic','recent_context':[],'captured_at':now,'source_at':now,'status':'pending'}
    c._process('capture',json.dumps(record),time.monotonic()-2)
    assert not calls and row(c)[0]['status']=='expired'


@pytest.mark.parametrize('wire_request,expected',[({},'unknown'),({'reasoning_effort':'high'},'high'),({'reasoning':{'effort':'max'}},'max'),
    ({'extra_body':{'reasoning':{'effort':'low'}}},'low'),({'reasoning_effort':True},'unknown')])
def test_wire_unknown_not_assumed(wire_request,expected):
    assert m.wire_effort(wire_request)==expected
