"""Registry-driven advisory classification; stdlib only and no import-time I/O."""
from __future__ import annotations

import http.client
import importlib
import json
import logging
import math
import os
from pathlib import Path
import re
import threading
import time
import urllib.request


logger = logging.getLogger(__name__)
API = "https://openrouter.ai/api/v1/systemone"
MODEL = "jev-latest"
KEY_ENV = "OPENROUTER_API_KEY"
REGISTRY_PATH = Path(__file__).with_name("scenarios.yaml")
CONNECT_SECONDS = 3
TOTAL_SECONDS = 8
MAX_TEXT_CHARS = 4000
MAX_TEXT_BYTES = 16384
MAX_REGISTRY_BYTES = 256 * 1024
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024

# A timed-out DNS/read cannot be forcibly killed by stdlib threads. Keep its
# single slot occupied until it exits: subsequent turns abstain, not spawn more.
_TRANSPORT_SLOT = threading.BoundedSemaphore(1)

# Small mapping/scalar-only YAML reader; intentionally not a general YAML loader.
_KEY = re.compile(r'^("(?:[^"\\]|\\.)*"|[A-Za-z0-9_][A-Za-z0-9_-]*)[ ]*:(?:[ ]+(.*))?$')
_INT = re.compile(r"^[-+]?[0-9]+$")
_FLOAT = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$")
_BOOLS = {v: True for v in ("true", "yes", "on")}
_BOOLS.update({v: False for v in ("false", "no", "off")})
_PLAIN_START = set("-?:,[]{}#&*!|>'\"%@`")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _plain(text: str, where: str):
    if not text or text[0] in _PLAIN_START or ": " in text or " #" in text or text.endswith(":"):
        raise ValueError(f"{where}: unsupported plain scalar")
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
                raise ValueError(f"{where}: expected key: value")
            raw_key, rest = match.group(1), (match.group(2) or "")
            key = _quoted(raw_key, where)[0] if raw_key.startswith('"') else _plain(raw_key, where)
            if key in result:
                raise ValueError(f"{where}: duplicate key")
            i += 1
            if not rest or rest.startswith("#"):
                nxt = self._skip(i)
                if nxt < len(self.lines) and _indent(self.lines[nxt]) > indent:
                    value, i = self._mapping(nxt, _indent(self.lines[nxt]))
                else:
                    value = None
            elif rest.startswith(">-"):
                if not _comment_only(rest[2:]):
                    raise ValueError(f"{where}: only >- folded scalars are supported")
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
    """Parse mapping/scalar YAML; unsupported syntax raises ValueError."""
    return _SubsetParser(text).parse()


_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOP_KEYS = {"version", "untrusted_data_clause", "scenarios"}
_SCENARIO_KEYS = {"threshold", "max_probability_must_win", "questions", "on_match", "blocks"}
_QUESTION_KEYS = {"type", "instructions", "criteria"}


def _text(value, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where}: must be a non-empty string")
    return value.strip()


def _mapping(value, where: str, allowed=None) -> dict:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{where}: must be a non-empty mapping")
    if allowed is not None and set(value) - allowed:
        raise ValueError(f"{where}: unknown keys")
    return value


def _name(value, where: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{where}: invalid name (quote yes/no/on/off)")
    return value


def _probability(value) -> bool:
    # Compare first: math.isfinite converts integers to floats and can overflow.
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= 1 and math.isfinite(value))


def build_registry(data) -> dict:
    """Validate the entire registry; no scenario names are special to Python."""
    data = _mapping(data, "registry", _TOP_KEYS)
    if type(data.get("version")) is not int or data["version"] != 1:
        raise ValueError("registry: version must be 1")
    clause = _text(data.get("untrusted_data_clause"), "untrusted_data_clause")
    scenarios, seen_questions = [], set()
    for raw_name, raw in _mapping(data.get("scenarios"), "scenarios").items():
        name = _name(raw_name, "scenario")
        where = f"scenario {name}"
        raw = _mapping(raw, where, _SCENARIO_KEYS)
        threshold = raw.get("threshold", 0.90)
        if not _probability(threshold) or threshold == 0:
            raise ValueError(f"{where}: threshold must be in (0, 1]")
        must_win = raw.get("max_probability_must_win", True)
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
                criteria[_name(raw_c, f"{qwhere} choice")] = _text(meaning, qwhere)
            if len(criteria) < 2 or not any(c in criteria for c in ("none", "no")):
                raise ValueError(f"{qwhere}: needs at least two choices, including none or no")
            questions[qname] = {
                "type": "choice",
                "instructions": f"{clause} {_text(question.get('instructions'), qwhere)}",
                "criteria": criteria,
            }
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
                raise ValueError(f"{where}.on_match: not a choice of every question")
            if block_id is not None:
                _name(block_id, f"{where}.on_match block")
                if block_id not in blocks:
                    raise ValueError(f"{where}.on_match: unknown block")
            on_match[value] = block_id
        if set(blocks) - set(on_match.values()):
            raise ValueError(f"{where}.blocks: unreferenced blocks")
        scenarios.append({"name": name, "threshold": float(threshold),
                          "max_probability_must_win": must_win,
                          "questions": questions, "on_match": on_match, "blocks": blocks})
    return {"scenarios": scenarios}


def load_registry(path=None):
    """Read only the bounded registry file; invalid configuration disables routing."""
    try:
        with open(path or REGISTRY_PATH, "rb") as handle:
            raw = handle.read(MAX_REGISTRY_BYTES + 1)
        if len(raw) > MAX_REGISTRY_BYTES:
            raise ValueError("registry size")
        return build_registry(parse_subset(raw.decode("utf-8")))
    except Exception:
        logger.warning("scenarios: invalid_registry")
        return None


def _api_key() -> str:
    try:
        native = importlib.import_module("agent.secret_scope")
    except ModuleNotFoundError as error:
        # Missing dependencies *inside* the native API are not standalone mode.
        if error.name not in ("agent", "agent.secret_scope"):
            raise
        value = os.environ.get(KEY_ENV, "")
    else:
        value = native.get_secret(KEY_ENV)
    return value.strip() if isinstance(value, str) else ""


def _bounded_text(text) -> bool:
    return (isinstance(text, str) and 0 < len(text) <= MAX_TEXT_CHARS
            and bool(text.strip()) and len(text.encode("utf-8")) <= MAX_TEXT_BYTES)


class _Connection(http.client.HTTPSConnection):
    def connect(self):
        self.timeout = min(CONNECT_SECONDS, TOTAL_SECONDS)
        super().connect()
        self.sock.settimeout(TOTAL_SECONDS)


class _Handler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_Connection, request, context=self._context)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urlopen(request, *, timeout):
    """Construct the transport lazily; ignore ambient proxies and all redirects."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(), _Handler()
    ).open(request, timeout=timeout)


def _unique_fields(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _fetch(body: bytes, key: str):
    request = urllib.request.Request(API, data=body, method="POST", headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "Accept": "application/json", "Accept-Encoding": "identity",
    })
    with urlopen(request, timeout=min(CONNECT_SECONDS, TOTAL_SECONDS)) as response:
        if response.status != 200:
            return None
        encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
        if encoding != "identity":
            return None
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
            return None
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        return None
    return json.loads(raw, object_pairs_hook=_unique_fields, parse_constant=_invalid_constant)


def _request(body: bytes, key: str):
    if not _TRANSPORT_SLOT.acquire(blocking=False):
        return None
    result = {}
    deadline = time.monotonic() + TOTAL_SECONDS

    def work():
        try:
            result["payload"] = _fetch(body, key)
        except Exception:
            # Never include exception text: it can contain the request or key.
            logger.warning("scenarios: transport_error")
        finally:
            _TRANSPORT_SLOT.release()

    worker = threading.Thread(target=work, name="scenarios-classifier", daemon=True)
    try:
        worker.start()
    except Exception:
        _TRANSPORT_SLOT.release()
        raise
    worker.join(max(0, deadline - time.monotonic()))
    if worker.is_alive() or time.monotonic() > deadline:
        logger.warning("scenarios: transport_timeout")
        return None
    return result.get("payload")


def _valid_answer(answer, choices):
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        return None
    choice = answer.get("choice")
    confidence = answer.get("confidence")
    probabilities = answer.get("probabilities")
    if (not isinstance(choice, str) or choice not in choices or not _probability(confidence)
            or not isinstance(probabilities, dict) or set(probabilities) != set(choices)
            or not all(_probability(value) for value in probabilities.values())):
        return None
    if abs(sum(probabilities.values()) - 1) >= 0.01:
        return None
    return choice, confidence, probabilities


def _injections(payload, registry: dict) -> list[str]:
    answers = payload.get("answers") if isinstance(payload, dict) else None
    expected = {name: question for scenario in registry["scenarios"]
                for name, question in scenario["questions"].items()}
    if not isinstance(answers, dict) or set(answers) != set(expected):
        return []
    validated = {}
    for name, question in expected.items():
        value = _valid_answer(answers[name], question["criteria"])
        if value is None:
            return []
        validated[name] = value
    blocks = []
    for scenario in registry["scenarios"]:
        strong = {}
        for name in scenario["questions"]:
            choice, confidence, probabilities = validated[name]
            if confidence < scenario["threshold"]:
                continue
            if (scenario["max_probability_must_win"]
                    and probabilities[choice] < max(probabilities.values())):
                continue
            strong[name] = choice
        for choice, block_id in scenario["on_match"].items():
            if block_id and all(strong.get(name) == choice for name in scenario["questions"]):
                blocks.append(scenario["blocks"][block_id])
                logger.info("scenarios: matched %s", scenario["name"])
    return blocks


def classify(text: str) -> list[str]:
    """One bounded request, returning only matched registry guidance or []."""
    try:
        if not _bounded_text(text):
            return []
        registry = load_registry()
        if not registry:
            return []
        # Resolve per-turn profile context before crossing the worker boundary.
        key = _api_key()
        if not key:
            return []
        redactor = importlib.import_module("agent.redact")
        clean = redactor.redact_for_egress(text)  # Native egress always force-redacts.
        if clean == getattr(redactor, "REDACTION_UNAVAILABLE", None) or not _bounded_text(clean):
            return []
        questions = {name: question for scenario in registry["scenarios"]
                     for name, question in scenario["questions"].items()}
        body = json.dumps({"model": MODEL, "state": {"text": clean},
                           "questions": questions}, ensure_ascii=False).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            return []
        return _injections(_request(body, key), registry)
    except Exception:
        logger.warning("scenarios: classification_unavailable")
        return []
