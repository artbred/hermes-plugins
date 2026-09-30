"""Unknown target identity must not authorize a saved-note acknowledgment."""
import importlib.util
import json
from pathlib import Path
import pytest

@pytest.mark.parametrize('write_id,read_id', [('note-1',None),(None,'note-1'),(None,None),('note-1','note-2')])
def test_readback_requires_explicit_matching_ids(monkeypatch,write_id,read_id):
    spec=importlib.util.spec_from_file_location('memory_target_critic',Path(__file__).resolve().parents[1]/'__init__.py')
    c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
    write={'success':True};read={'success':True,'content':'Synthetic note'}
    if write_id is not None:write['document_id']=write_id
    if read_id is not None:read['document_id']=read_id
    rows=[{'role':'user','content':'Remember this synthetic note.'},
          {'role':'tool','name':'mcp__hindsight__sync_retain','content':json.dumps(write)},
          {'role':'tool','name':'mcp__hindsight__get_document','content':json.dumps(read)}]
    monkeypatch.setattr(c,'_conversation_from_frames',lambda:rows)
    assert c._memory_confirmed('test-session') is False
