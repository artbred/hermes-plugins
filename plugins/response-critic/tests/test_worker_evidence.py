"""Tool evidence remains visible when the verifier runs on a hook worker."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace


def test_worker_reads_live_session_tool_results(monkeypatch):
    path = Path(__file__).resolve().parents[1] / '__init__.py'
    spec = importlib.util.spec_from_file_location('critic_worker_test', path)
    critic = importlib.util.module_from_spec(spec); spec.loader.exec_module(critic)
    rows = [
        {'role': 'user', 'content': 'Run the test suite.'},
        {'role': 'tool', 'name': 'terminal', 'content': '33 passed; core checkout clean.'},
    ]
    agent = SimpleNamespace(_session_messages=rows)
    monkeypatch.setattr(critic, '_get_agent', lambda: agent)
    with ThreadPoolExecutor(max_workers=1) as pool:
        digest = pool.submit(critic._evidence_appendix).result(timeout=5)
    assert '33 passed; core checkout clean.' in digest
    assert 'Run the test suite.' in digest
