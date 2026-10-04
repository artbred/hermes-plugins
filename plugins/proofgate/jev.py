"""Bounded, registry-driven Jev turn judgment (stdlib only, no Hermes imports).

Every judgment lives in ``scenarios.yaml`` next to this file: questions,
criteria, the untrusted-data clause, per-scenario thresholds and injection
blocks. This module only loads that registry, asks all of its questions in ONE
request, validates each answer on its own and maps verdicts to blocks.

The registry is read with a small stdlib parser for a documented YAML subset
(``parse_subset``) instead of PyYAML: the Hermes plugin interpreter ships
without PyYAML, and a single parser means a single behavior. The file stays
valid YAML.

Failure policy: an unreadable or invalid registry disables Jev (no request,
empty verdicts); any request failure (no key, network, non-200, timeout,
oversized/malformed body) yields empty verdicts. Nothing here raises to the
caller.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import os
import re
import threading
import urllib.request
from pathlib import Path

try:
    from . import rules
except ImportError:  # imported as a top-level module (tests)
    import rules

logger = logging.getLogger(__name__)

API = "https://openrouter.ai/api/v1/systemone"
MODEL = "jev-latest"
KEY_ENV = "OPENROUTER_API_KEY"
KEY_FILE = "/etc/hermes-speech/jev-key"
REGISTRY_PATH = Path(__file__).with_name("scenarios.yaml")
CONNECT_SECONDS = 3
TOTAL_SECONDS = 8
MAX_RESPONSE_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4000
MAX_REGISTRY_BYTES = 256 * 1024

# Schema defaults (scenarios.yaml normally sets both explicitly).
DEFAULT_THRESHOLD = 0.90
DEFAULT_MAX_PROBABILITY_MUST_WIN = True

# ---------------------------------------------------------------------------
# YAML subset parser
# ---------------------------------------------------------------------------

_KEY = re.compile(r'^("(?:[^"\\]|\\.)*"|[A-Za-z0-9_][A-Za-z0-9_-]*)[ ]*:(?:[ ]+(.*))?$')
_INT = re.compile(r"^[-+]?[0-9]+$")
_FLOAT = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")
_BOOLS = {v: True for v in ("true", "yes", "on")}
_BOOLS.update({v: False for v in ("false", "no", "off")})
_PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _plain(text: str, where: str):
    """Plain scalar: null, bool (YAML 1.1 words), int, float, else string."""
    if not text or text[0] in _PLAIN_START or ": " in text or " #" in text or text.endswith(":"):
        raise ValueError(f"{where}: unsupported plain scalar {text!r} (quote it)")
    if text in ("null", "Null", "NULL", "~"):
        return None
    lowered = text.lower()
    if lowered in _BOOLS and text in (lowered, lowered.title(), lowered.upper()):
        return _BOOLS[lowered]
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


def _quoted(text: str, where: str):
    """(string, rest) for a leading double-quoted scalar."""
    match = re.match(r'"(?:[^"\\]|\\.)*"', text)
    if not match:
        raise ValueError(f"{where}: unterminated string")
    try:
        value = json.loads(match.group(0))
    except ValueError:
        raise ValueError(f"{where}: unsupported escape in string") from None
    return value, text[match.end():]


def _comment_only(rest: str) -> bool:
    return not rest or (rest[0] == " " and rest.lstrip(" ").startswith("#")) or not rest.strip()


class _SubsetParser:
    def __init__(self, text: str):
        self.lines = text.split("\n")

    def _skip(self, i: int) -> int:
        while i < len(self.lines):
            stripped = self.lines[i].strip()
            if stripped and not stripped.startswith("#"):
                break
            i += 1
        return i

    def parse(self) -> dict:
        for number, line in enumerate(self.lines, 1):
            if line.lstrip(" ").startswith("\t"):
                raise ValueError(f"line {number}: tab indentation")
        start = self._skip(0)
        if start < len(self.lines) and _indent(self.lines[start]) != 0:
            raise ValueError(f"line {start + 1}: top level must not be indented")
        result, end = self._mapping(start, 0)
        if self._skip(end) < len(self.lines):
            raise ValueError(f"line {end + 1}: unexpected content")
        return result

    def _mapping(self, i: int, indent: int):
        result = {}
        while True:
            i = self._skip(i)
            if i >= len(self.lines):
                return result, i
            line = self.lines[i]
            where = f"line {i + 1}"
            current = _indent(line)
            if current < indent:
                return result, i
            if current > indent:
                raise ValueError(f"{where}: unexpected indentation")
            match = _KEY.match(line[current:].rstrip(" "))
            if not match:
                raise ValueError(f"{where}: expected `key: value`")
            raw_key, rest = match.group(1), (match.group(2) or "")
            key = _quoted(raw_key, where)[0] if raw_key.startswith('"') else _plain(raw_key, where)
            if key in result:
                raise ValueError(f"{where}: duplicate key {key!r}")
            i += 1
            if not rest or rest.startswith("#"):
                nxt = self._skip(i)
                if nxt < len(self.lines) and _indent(self.lines[nxt]) > indent:
                    value, i = self._mapping(nxt, _indent(self.lines[nxt]))
                else:
                    value = None
            elif rest.startswith(">-"):
                if not _comment_only(rest[2:]):
                    raise ValueError(f"{where}: only `>-` folded scalars are supported")
                value, i = self._folded(i, indent, where)
            elif rest.startswith('"'):
                value, tail = _quoted(rest, where)
                if not _comment_only(tail):
                    raise ValueError(f"{where}: text after closing quote")
            else:
                cut = rest.find(" #")
                value = _plain((rest[:cut] if cut >= 0 else rest).rstrip(" "), where)
            result[key] = value

    def _folded(self, i: int, parent: int, where: str):
        parts, block = [], None
        while i < len(self.lines):
            line = self.lines[i]
            if not line.strip():
                nxt = i
                while nxt < len(self.lines) and not self.lines[nxt].strip():
                    nxt += 1
                if nxt < len(self.lines) and _indent(self.lines[nxt]) > parent:
                    raise ValueError(f"line {i + 1}: blank line inside folded scalar")
                break
            current = _indent(line)
            if current <= parent:
                break
            if block is None:
                block = current
            elif current != block:
                raise ValueError(f"line {i + 1}: folded scalar lines must share one indent")
            if line != line.rstrip():
                raise ValueError(f"line {i + 1}: trailing whitespace in folded scalar")
            parts.append(line[current:])
            i += 1
        if not parts:
            raise ValueError(f"{where}: empty folded scalar")
        return " ".join(parts), i


def parse_subset(text: str) -> dict:
    """Parse the documented YAML subset; raises ValueError on anything else."""
    return _SubsetParser(text).parse()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOP_KEYS = {"version", "untrusted_data_clause", "scenarios"}
_SCENARIO_KEYS = {"threshold", "max_probability_must_win", "questions", "on_match", "blocks"}
_QUESTION_KEYS = {"type", "instructions", "criteria"}
_ABSTAIN = ("none", "no")


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be a non-empty string")
    return value.strip()


def _mapping(value, where: str, allowed=None) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{where}: must be a non-empty mapping")
    if allowed is not None and set(value) - allowed:
        raise ValueError(f"{where}: unknown keys {sorted(set(value) - allowed, key=str)}")
    return value


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        hint = " (quote yes/no/on/off)" if isinstance(value, bool) else ""
        raise ValueError(f"{where}: invalid name {value!r}{hint}")
    return value


def build_registry(data) -> dict:
    """Validate parsed scenarios.yaml; return the normalized registry or raise."""
    data = _mapping(data, "registry", _TOP_KEYS)
    if data.get("version") != 1:
        raise ValueError("registry: version must be 1")
    clause = _text(data.get("untrusted_data_clause"), "untrusted_data_clause")
    scenarios, seen_questions = [], set()
    for raw_name, raw in _mapping(data.get("scenarios"), "scenarios").items():
        name = _name(raw_name, "scenario")
        where = f"scenario {name}"
        raw = _mapping(raw, where, _SCENARIO_KEYS)
        threshold = raw.get("threshold", DEFAULT_THRESHOLD)
        if (isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                or not 0 < threshold <= 1):
            raise ValueError(f"{where}: threshold must be in (0, 1]")
        must_win = raw.get("max_probability_must_win", DEFAULT_MAX_PROBABILITY_MUST_WIN)
        if not isinstance(must_win, bool):
            raise ValueError(f"{where}: max_probability_must_win must be true/false")
        questions = {}
        for raw_q, question in _mapping(raw.get("questions"), f"{where}.questions").items():
            qname = _name(raw_q, f"{where} question")
            qwhere = f"{where}.{qname}"
            if qname in seen_questions:
                raise ValueError(f"{qwhere}: question names must be unique across scenarios")
            seen_questions.add(qname)
            question = _mapping(question, qwhere, _QUESTION_KEYS)
            if question.get("type") != "choice":
                raise ValueError(f"{qwhere}: type must be choice")
            criteria = {}
            for raw_c, meaning in _mapping(question.get("criteria"), f"{qwhere}.criteria").items():
                criteria[_name(raw_c, f"{qwhere} choice")] = _text(meaning, f"{qwhere}.{raw_c}")
            if len(criteria) < 2:
                raise ValueError(f"{qwhere}: needs at least two choices")
            if not any(c in criteria for c in _ABSTAIN):
                raise ValueError(f"{qwhere}: needs an abstain choice (none or no)")
            questions[qname] = {
                "type": "choice",
                "instructions": f"{clause} {_text(question.get('instructions'), qwhere)}",
                "criteria": criteria}
        blocks = {}
        if raw.get("blocks") is not None:
            for raw_b, block in _mapping(raw["blocks"], f"{where}.blocks").items():
                block_id = _name(raw_b, f"{where} block")
                body = _text(block, f"{where}.blocks.{block_id}")
                if "\n" in body:
                    raise ValueError(f"{where}.blocks.{block_id}: must be one line")
                blocks[block_id] = body
        on_match = {}
        for raw_v, block_id in _mapping(raw.get("on_match"), f"{where}.on_match").items():
            value = _name(raw_v, f"{where}.on_match")
            if any(value not in q["criteria"] for q in questions.values()):
                raise ValueError(f"{where}.on_match: {value!r} is not a choice of every question")
            if block_id is not None and block_id not in blocks:
                raise ValueError(f"{where}.on_match: unknown block {block_id!r}")
            on_match[value] = block_id
        unused = set(blocks) - set(on_match.values())
        if unused:
            raise ValueError(f"{where}.blocks: unreferenced {sorted(unused)}")
        scenarios.append({"name": name, "threshold": float(threshold),
                          "max_probability_must_win": must_win,
                          "questions": questions, "on_match": on_match, "blocks": blocks})
    return {"scenarios": scenarios}


_REGISTRY_LOCK = threading.Lock()
_REGISTRY: dict = {}   # path -> registry dict, or None when invalid


def load_registry(path=None):
    """Registry from scenarios.yaml, loaded once per path; None when unusable."""
    path = str(path or REGISTRY_PATH)
    with _REGISTRY_LOCK:
        if path not in _REGISTRY:
            try:
                with open(path, "rb") as handle:
                    raw = handle.read(MAX_REGISTRY_BYTES + 1)
                if len(raw) > MAX_REGISTRY_BYTES:
                    raise ValueError("registry too large")
                _REGISTRY[path] = build_registry(parse_subset(raw.decode("utf-8")))
            except Exception as error:  # unreadable/invalid -> Jev disabled
                logger.warning("proofgate: Jev disabled, scenarios registry invalid: %s",
                               error)
                _REGISTRY[path] = None
        return _REGISTRY[path]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

_KEY_LOCK = threading.Lock()
_KEY_CACHE: dict = {}


def _api_key() -> str:
    """OPENROUTER_API_KEY, else the jev-key file; resolved once. Never logged."""
    with _KEY_LOCK:
        if "value" not in _KEY_CACHE:
            value = os.environ.get(KEY_ENV, "").strip()
            if not value:
                try:
                    with open(KEY_FILE, encoding="utf-8") as handle:
                        value = handle.read(4096).strip()
                except OSError:
                    value = ""
            _KEY_CACHE["value"] = value
        return _KEY_CACHE["value"]


class _Connection(http.client.HTTPSConnection):
    """Connect within CONNECT_SECONDS, then allow reads up to TOTAL_SECONDS."""

    def connect(self):
        self.timeout = CONNECT_SECONDS
        super().connect()
        self.sock.settimeout(TOTAL_SECONDS)


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_Connection, req, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


# No environment proxies, no redirects (mirrors trust_env=False/follow_redirects=False).
urlopen = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect(), _Handler()).open


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate field")
        result[name] = value
    return result


def _fetch(body: bytes, key: str):
    request = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json", "Accept-Encoding": "identity"})
    with urlopen(request, timeout=CONNECT_SECONDS) as response:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status != 200:
            return None
        encoding = (response.headers.get("Content-Encoding") or "identity").lower()
        if encoding != "identity":
            return None
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_RESPONSE_BYTES:
        return None
    return json.loads(raw, object_pairs_hook=_unique_fields)


# ---------------------------------------------------------------------------
# Judgment
# ---------------------------------------------------------------------------

def _probability(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def _valid_answer(answers, name: str, choices):
    """(choice, confidence, probabilities) when well-formed, else None."""
    answer = answers.get(name) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if (not isinstance(choice, str) or choice not in choices or not _probability(confidence)
            or not isinstance(probabilities, dict) or set(probabilities) != set(choices)
            or not all(_probability(v) for v in probabilities.values())):
        return None
    if abs(sum(probabilities.values()) - 1) >= 0.01:
        return None
    return choice, confidence, probabilities


def request_body(text: str, registry: dict) -> bytes:
    """One payload carrying every registry question."""
    questions = {qname: question for scenario in registry["scenarios"]
                 for qname, question in scenario["questions"].items()}
    clean = rules.redact(text)[:MAX_TEXT_CHARS]
    return json.dumps({"model": MODEL, "state": {"text": clean},
                       "questions": questions}).encode("utf-8")


def judge_payload(payload, registry: dict) -> dict:
    """{scenario: {question: (choice, confidence)}} for answers that are
    well-formed and pass their scenario's threshold (and, when required, are
    the most probable choice). Each answer is validated on its own."""
    answers = payload.get("answers") if isinstance(payload, dict) else None
    verdicts = {}
    for scenario in registry["scenarios"]:
        strong = {}
        for qname, question in scenario["questions"].items():
            answer = _valid_answer(answers, qname, question["criteria"])
            if answer is None:
                continue
            choice, confidence, probabilities = answer
            if confidence < scenario["threshold"]:
                continue
            if (scenario["max_probability_must_win"]
                    and probabilities[choice] < max(probabilities.values())):
                continue
            strong[qname] = (choice, confidence)
        if strong:
            verdicts[scenario["name"]] = strong
    return verdicts


def injections(verdicts: dict, registry=None) -> list:
    """Injection blocks fired by `verdicts`, in registry order. A scenario's
    on_match value fires only when every one of its questions chose it."""
    registry = registry if registry is not None else load_registry()
    if not registry or not isinstance(verdicts, dict):
        return []
    blocks = []
    for scenario in registry["scenarios"]:
        got = verdicts.get(scenario["name"]) or {}
        for value, block_id in scenario["on_match"].items():
            if block_id and all(got.get(q, (None,))[0] == value
                                for q in scenario["questions"]):
                blocks.append(scenario["blocks"][block_id])
    return blocks


def judge_turn(text) -> dict:
    """Verdicts from at most one Jev call; {} on any doubt or failure."""
    try:
        if not isinstance(text, str) or not text.strip():
            return {}
        registry = load_registry()
        if not registry:
            return {}
        key = _api_key()
        if not key:
            return {}
        body = request_body(text, registry)
        box: dict = {}

        def work():
            try:
                box["payload"] = _fetch(body, key)
            except BaseException:  # any failure -> fallback; never surfaces
                box["payload"] = None

        # Hard wall-clock bound: the turn waits at most TOTAL_SECONDS.
        worker = threading.Thread(target=work, name="proofgate-jev", daemon=True)
        worker.start()
        worker.join(TOTAL_SECONDS)
        if worker.is_alive() or "payload" not in box:
            return {}
        return judge_payload(box["payload"], registry)
    except Exception:
        return {}
