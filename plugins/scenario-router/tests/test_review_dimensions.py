"""Regressions from live API diagnostics. All responses here are synthetic."""
import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('dimension_review',Path(__file__).resolve().parents[1]/'__init__.py')
r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)


def response(outcome,verdict='ready',memory='not_applicable',vconfidence=.45,mconfidence=.52):
    labels={'outcome':(outcome,.99),'verdict':(verdict,vconfidence),'memory_evidence':(memory,mconfidence), 'judge_required':('required',.99),'judge_effort':('max',.99)}
    return {'answers':{name:{'type':'choice','choice':label,'confidence':confidence,'probabilities':{k:int(k==label) for k in r.QUESTIONS[name]['criteria']}} for name,(label,confidence) in labels.items()}}


def state(pending=False):
    return {'user_message':'Synthetic request','assistant_response':'Synthetic result','evidence':'Synthetic evidence','internal':False,'pending_background':pending}


@pytest.mark.parametrize('outcome,disposition',[('technical_failure','recover'),('missing_input','correct'),('safety_refusal','refusal'),('async_handoff','handoff')])
def test_irrelevant_dimensions_do_not_erase_confident_outcome(outcome,disposition):
    result=r.review_envelope(response(outcome),state(outcome=='async_handoff'),r.DEFAULTS)
    assert result['ok'] and result['review']['scenario']==outcome
    assert result['review']['disposition']==disposition
    assert result['review']['confidence']==.99


def test_low_readiness_does_not_accept_normal_answer():
    result=r.review_envelope(response('normal_answer'),state(),r.DEFAULTS)
    assert not result['ok'] and result['review']['disposition']=='uncertain'


def test_no_memory_question_gate_for_nonmemory_acceptance():
    result=r.review_envelope(response('normal_answer',vconfidence=.99),state(),r.DEFAULTS)
    assert result['ok'] and result['review']['disposition']=='accept'


def test_note_threshold_is_not_relaxed_for_live_pass_rate():
    result=r.review_envelope(response('brain_dump_added',memory='confirmed',vconfidence=.99,mconfidence=.94),state(),r.DEFAULTS)
    assert not result['ok'] and result['review']['disposition']=='uncertain'
    assert result['review']['acknowledgment']==''
