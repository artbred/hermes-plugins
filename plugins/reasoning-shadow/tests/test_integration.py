"""Integration against the installed Python 3.11 Hermes plugin registry."""
import copy
import json
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from test_collector import human_before, m, payload, drain, row, collector


def test_real_plugin_manager_discovery_and_unload(tmp_path,monkeypatch):
    from hermes_cli.plugins import PluginManager
    from hermes_cli.plugins_discovery import scan_directory
    import hermes_cli.plugins as host
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token=set_hermes_home_override(tmp_path)
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    plugin_dir=tmp_path/'plugins'/'reasoning-shadow'
    shutil.copytree(Path(__file__).resolve().parents[1],plugin_dir,ignore=shutil.ignore_patterns('tests','__pycache__'))
    (tmp_path/'config.yaml').write_text('plugins:\n  enabled: [reasoning-shadow]\n  entries:\n    reasoning-shadow:\n      settings:\n        mode: shadow\n',encoding='utf-8')
    manager=PluginManager(scope_key=str(tmp_path))
    monkeypatch.setattr(manager,'_collect_directory_manifests',lambda:scan_directory(tmp_path/'plugins','user'))
    monkeypatch.setattr(manager,'_scan_entry_points',lambda:[])
    try:
        manager.discover_and_load()
        loaded=manager._plugins['reasoning-shadow']
        assert loaded.enabled and not loaded.error
        expected={'pre_llm_call','pre_api_request','post_api_request','post_tool_call','post_llm_call','on_session_reset'}
        assert expected <= set(manager._hooks)
        assert not manager._plugin_tool_names and not manager._middleware and not manager._system_prompt_sections
        c=manager._hooks['pre_llm_call'][0].__self__
        c.client=SimpleNamespace(decide=lambda state:payload())
        before=copy.deepcopy({'platform':'telegram','session_id':'synthetic-session','turn_id':'synthetic-turn','user_message':'synthetic hello','sender_id':'fixture-human','conversation_history':[{'role':'user','content':'synthetic hello','platform_message_id':'fixture-inbound'}]})
        assert manager.invoke_hook('pre_llm_call',**before)==[]
        assert manager.invoke_hook('pre_api_request',session_id='synthetic-session',turn_id='synthetic-turn',request={'reasoning_effort':'high'},api_request_id='synthetic-api')==[]
        assert manager.invoke_hook('post_api_request',session_id='synthetic-session',turn_id='synthetic-turn',api_request_id='synthetic-api',usage={'input_tokens':3,'output_tokens':2},api_duration=.1)==[]
        assert manager.invoke_hook('post_llm_call',session_id='synthetic-session',turn_id='synthetic-turn',assistant_response='Synthetic completed draft')==[]
        drain(c)
        assert c.pool.report()['total']==1 and row(c)[0]['actual']['wire_effort']=='high'
        assert manager.unload('reasoning-shadow') and c.closed.is_set()
        assert not any(manager._hooks.values())
    finally:
        manager.unload()
        reset_hermes_home_override(token)


def test_worker_disk_latency_cannot_block_callbacks(collector,monkeypatch):
    c=collector(mode='shadow')
    started,release=threading.Event(),threading.Event()
    original=m.Pool.insert
    def blocked(pool,record):
        started.set(); release.wait(3); return original(pool,record)
    monkeypatch.setattr(m.Pool,'insert',blocked)
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    assert started.wait(2)
    now=time.monotonic()
    for fn in (c.pre_api,c.post_api,c.tool,c.after):
        assert fn(session_id='s',turn_id='t',api_request_id='synthetic-api') is None
    assert time.monotonic()-now<.2
    release.set(); drain(c)


def test_parallel_same_tid_no_mixed_usage(collector):
    c=collector(mode='shadow'); barrier=threading.Barrier(6)
    def work(i):
        barrier.wait()
        sid='synthetic-s'+str(i)
        human_before(c, platform='telegram', session_id=sid,turn_id='synthetic-same-turn',user_message='synthetic request '+str(i))
        c.post_api(session_id=sid,turn_id='synthetic-same-turn',api_request_id='synthetic-api'+str(i),usage={'input_tokens':i+1},api_duration=i)
        c.after(session_id=sid,turn_id='synthetic-same-turn')
    threads=[threading.Thread(target=work,args=(i,)) for i in range(6)]
    for t in threads:t.start()
    for t in threads:t.join(3)
    assert not any(t.is_alive() for t in threads)
    drain(c)
    assert c.pool.report()['total']==6
    with c.pool._connection() as db:
        rows=[json.loads(r[0]) for r in db.execute('SELECT record FROM examples')]
    for record in rows:
        i=int(record['request'].split()[-1])
        assert record['actual']['input_tokens']==i+1 and record['actual']['api_calls']==1


def test_cache_bounds_and_reset(collector):
    c=collector(mode='shadow',max_cached_turns=2)
    for i in range(4):
        human_before(c, platform='telegram', session_id='s',turn_id=str(i),user_message='synthetic request '+str(i))
    assert len(c.seen)==2 and len(c.turns)==2
    c.reset(session_id='s')
    assert not c.seen and not c.turns
    drain(c)


def test_before_cutoff_not_execution_end_or_worker_start(collector):
    c=collector(mode='shadow')
    first_started,release=threading.Event(),threading.Event()
    states=[]
    def decide(state):
        states.append(copy.deepcopy(state))
        if len(states)==1:
            first_started.set(); release.wait(3)
        return payload()
    c.client=SimpleNamespace(decide=decide)
    human_before(c, platform='telegram', session_id='synthetic-s',turn_id='first',user_message='synthetic common request')
    assert first_started.wait(2)
    human_before(c, platform='telegram', session_id='synthetic-s',turn_id='second',user_message='synthetic common request second')
    second_capture=c.turns[('synthetic-s','second')]['captured_at']
    # Added after second snapshot while its inference is queued: cannot be a
    # prior example, even with an artificially earlier source timestamp.
    now=time.time()
    c.pool.insert(dict(source_handle='synthetic:future',request='synthetic common FUTURE_EXAMPLE',recent_context=[],captured_at=now,source_at=now-10,status='history_unknown'))
    c.pool.annotate('synthetic:future','verified_success')
    c.after(session_id='synthetic-s',turn_id='second',assistant_response='FUTURE_DRAFT')
    release.set(); drain(c)
    assert states[1]['captured_at']==second_capture
    assert 'FUTURE_EXAMPLE' not in json.dumps(states[1]) and 'FUTURE_DRAFT' not in json.dumps(states)
    assert all(e['source_handle']!='live:'+m.digest('synthetic-s\0second') for e in states[1]['previous_examples'])
    assert all(e['outcome']=='unknown' for e in states[1]['previous_examples'])


def test_profile_isolation_live_and_failure_cache(tmp_path,monkeypatch):
    from hermes_constants import set_hermes_home_override,reset_hermes_home_override
    instances=[]
    try:
        for name in ('synthetic-one','synthetic-two'):
            home=tmp_path/name; token=set_hermes_home_override(home)
            try:
                c=m.Collector(SimpleNamespace(get_config=lambda k,d:'shadow' if k=='mode' else d,state={}))
            finally:
                reset_hermes_home_override(token)
            c.client=SimpleNamespace(decide=lambda s: {'error':'http_error'} if name=='synthetic-one' else payload())
            human_before(c, platform='telegram', session_id='same-s',turn_id='same-t',user_message='synthetic common request')
            drain(c); instances.append(c)
        assert instances[0].home != instances[1].home
        assert instances[0].pool.failure_get()['count']==1 and instances[1].pool.failure_get()=={}
        assert all(c.pool.report()['total']==1 for c in instances)
    finally:
        for c in instances:c.close(); c.worker.join(2)


def test_untrusted_nonfinite_and_bool_usage_ignored(collector):
    c=collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    c.post_api(session_id='s',turn_id='t',api_request_id='one',api_duration=float('nan'),usage={'input_tokens':True,'output_tokens':-1,'total_tokens':'999'})
    c.post_api(session_id='s',turn_id='t',api_request_id='two',api_duration=10**1000)
    drain(c)
    a=row(c)[0]['actual']
    assert a['api_duration_seconds']==0 and a['input_tokens']==0 and a['output_tokens']==0


@pytest.mark.parametrize('mode',['active','low','invalid',True])
def test_only_off_shadow_modes_allowed(collector,mode):
    with pytest.raises(ValueError,match='invalid_shadow_configuration'):
        collector(mode=mode)


def test_all_visible_context_and_request_redacted_before_transport(collector):
    c=collector(mode='shadow'); states=[]
    c.client=SimpleNamespace(decide=lambda s:states.append(s) or payload())
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='password=synthetic-secret-current',conversation_history=[
        {'role':'assistant','content':'<think>DO_NOT_CAPTURE_HIDDEN</think> previous public password=synthetic-secret-history'},
        {'role':'user','content':'password=synthetic-secret-current'}])
    drain(c)
    encoded=json.dumps(states)
    assert 'synthetic-secret' not in encoded and 'DO_NOT_CAPTURE_HIDDEN' not in encoded
    assert states[0]['recent_context'][0]['content'].endswith('[REDACTED]')


def test_real_api_hook_wire_shape():
    from agent.api_request_hooks import ApiRequestHooksMixin
    raw={'model':'synthetic-model','reasoning':{'effort':'high'},'messages':[{'role':'user','content':'synthetic'}],'api_key':'synthetic-sensitive'}
    clean=ApiRequestHooksMixin()._api_request_payload_for_hook(raw)
    assert m.wire_effort(clean)=='high'
    assert clean['body']['api_key'] != 'synthetic-sensitive'


def test_decorated_current_input_not_prior_context(collector):
    c=collector(mode='shadow'); states=[]
    c.client=SimpleNamespace(decide=lambda s:states.append(s) or payload())
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic original input',conversation_history=[
        {'role':'assistant','content':'synthetic previous answer'},
        {'role':'user','content':[{'type':'text','text':'synthetic decorated current'}]},
        {'role':'assistant','content':'FUTURE_MUST_NOT_APPEAR'}])
    drain(c)
    assert states[0]['recent_context']==[{'role':'assistant','content':'synthetic previous answer'}]
    assert 'FUTURE_MUST_NOT_APPEAR' not in json.dumps(states)


def test_metadata_internal_current_with_different_content_excluded(collector):
    c=collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic original',conversation_history=[
        {'role':'user','content':'synthetic decorated notification','display_metadata':{'_verification_nudge':True}}])
    assert c.metrics['captured']==0


def test_invalid_prediction_report_not_hidden_as_unknown(collector):
    c=collector(mode='shadow'); c.client=SimpleNamespace(decide=lambda s:{'answers':{}})
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic'); drain(c)
    report=c.pool.report()
    assert report['by_prediction_status']=={'invalid_response':1}
    assert report['prediction_counts']['invalid']==1
    assert sum(report['prediction_comparison'].values())==report['total']
    assert c.pool.failure_get()['count']==1


def test_enormous_choice_numeric_abstains():
    p=payload(); p['answers']['effort']['confidence']=10**1000
    assert m.recommendation(p)['status']=='invalid_response'


def test_callback_errors_never_escape_or_log_raw_metadata(collector,caplog):
    c=collector(mode='shadow')
    human_before(c, platform='telegram', session_id='s',turn_id='t',user_message='synthetic')
    assert c.tool(session_id='s',turn_id='t',tool_call_id='synthetic-malformed',status={'raw':'NEVER_LOG_THIS'}) is None
    assert c.metrics['errors']==1 and not caplog.records
    drain(c)
