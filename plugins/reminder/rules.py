"""Pure rules for the reminder gatekeeper (no I/O, no Hermes imports).

- detect_kinds(): which checklists a user request opens. Only imperative
  action verbs count (clause start, "and/then <verb>", or an explicit cue like
  "I want you to <verb>"). Questions without an explicit cue open nothing.
- classify()/evaluate(): which tool touches count as the action for a check
  and which count as its verification. A check closes only when a read-only
  verification ran AFTER the latest successful action.
- render()/inject(): the short hint block and how it is attached to the
  outgoing request for each API shape.
"""

from __future__ import annotations

import hashlib
import json
import re

CHECKLISTS = {
    "remove": ("Removal: after removing, show absence with a fresh read-only "
               "check (list/ls/grep/plugins list/crontab -l); check leftovers "
               "(registrations, schedules, files, config refs)."),
    "install": ("Install/setup: show it is installed AND enabled AND configured "
                "AND working (version/health/list output) before claiming done."),
    "update": ("Update: show the version before and after; confirm dependents "
               "are still healthy; know the rollback path."),
    "schedule": ("Schedule: show it is registered in the right scheduler "
                 "(cron list/crontab -l/timers), logs to a known file, and its "
                 "first run or next-run time."),
    "secret": ("Secrets: keep values redacted in output; files private (600); "
               "never commit them — scan staged diffs."),
}

# --------------------------------------------------------------------------
# Request -> checklist kinds
# --------------------------------------------------------------------------

_SOFTWARE = (r"(?:plugins?|skills?|services?|cron\w*|jobs?|timers?|servers?|mcp|"
             r"hooks?|integrations?|packages?|dependenc\w+|containers?|daemons?|"
             r"bots?|webhooks?|endpoints?|hermes|docker|node|python|pip|npm|deps|"
             r"versions?|images?|kernel|system|tools?|apps?|плагин\w*|скилл\w*|"
             r"сервис\w*|крон\w*|сервер\w*|пакет\w*|верси\w*|систем\w*)")

_VERBS = {
    "remove": (r"remove|delete|uninstall|retire|purge|wipe|clean\s?up|"
               r"get\s+rid\s+of|disable|удали(?:ть|те)?|убери(?:те)?|убрать|"
               r"снеси(?:те)?|снести|отключи(?:ть|те)?"),
    "install": (r"install|reinstall|enable|set\s?up|deploy|configure|wire\s+up|"
                r"установи(?:ть|те)?|поставь(?:те)?|поставить|включи(?:ть|те)?|"
                r"настрой(?:те)?|настроить|разверни(?:те)?|развернуть|"
                r"задеплой(?:те|ить)?|"
                r"(?:add|create|добавь(?:те)?|добавить|создай(?:те)?|создать)"
                r"\s+(?:[\w'-]+\s+){0,3}?" + _SOFTWARE),
    "update": (r"upgrade|bump|migrate|мигрируй(?:те)?|мигрировать|"
               r"(?:update|обнови(?:ть|те)?)\s+(?:[\w'-]+\s+){0,3}?" + _SOFTWARE),
    "schedule": (r"schedule|запланируй(?:те)?|запланировать|"
                 r"(?:monitor|мониторь(?:те)?|мониторить|следи(?:те)?\s+за)"),
}

_TOPICS = {
    "secret": (r"\b(?:credentials?|secrets?|api[-_ ]?keys?|tokens?|passwords?|"
               r"ключ\w*|токен\w*|парол\w*|секрет\w*)\b"),
    "schedule": r"\b(?:cron\w*|crontab|timers?|heartbeat|крон\w*|таймер\w*)\b",
}

_LEAD = r"(?:^|[.!?;:\n]\s*|\n\s*(?:[-*•]|\d+[.)])\s*)"
_FILLER = (r"(?:(?:please|pls|now|also|then|and|just|ok(?:ay)?|so|yes,?|"
           r"go\s+ahead\s+and|let'?s|пожалуйста|давай(?:те)?|теперь|также|и|"
           r"потом|просто|ещё|еще|да,?)\s+){0,4}")
_CUE = (r"(?:(?:want|need|would\s+like|'d\s+like)\s+(?:you|u)\s+to|can\s+(?:you|u)|"
        r"could\s+(?:you|u)|would\s+(?:you|u)|will\s+(?:you|u)|please|go\s+ahead\s+and|"
        r"хочу,?\s+чтобы\s+ты|нужно|надо|можешь|сможешь)\s+"
        r"(?:[\w'-]+\s+){0,2}?")
_JOIN = r"\b(?:and|then|и|потом|а\s+также)\s+(?:also\s+|ещё\s+|еще\s+)?"
_INTERNAL = re.compile(
    r"^\s*\[(?:IMPORTANT|SYSTEM|INTERNAL|CONTEXT COMPACTION|ASYNC DELEGATION|"
    r"Your active task list)|\[INTERNAL NOTIFICATION|^\s*\S+@\S+:\S*\s*[#$]\s",
    re.IGNORECASE)


def _compile(prefix: str, verbs: str):
    return re.compile(prefix + r"(?:" + verbs + r")\b", re.IGNORECASE)


_CLAUSE_RX = {k: _compile(_LEAD + _FILLER, v) for k, v in _VERBS.items()}
_CUE_RX = {k: _compile(r"\b" + _CUE, v) for k, v in _VERBS.items()}
_JOIN_RX = {k: _compile(_JOIN, v) for k, v in _VERBS.items()}
_TOPIC_RX = {k: re.compile(v, re.IGNORECASE) for k, v in _TOPICS.items()}


def _is_question(text: str) -> bool:
    return text.rstrip().rstrip(")\"'»”").endswith("?")


def detect_kinds(text: str) -> list:
    """Checklist kinds opened by an imperative request (sorted)."""
    if not text or not text.strip() or _INTERNAL.search(text):
        return []
    cued = {k for k, rx in _CUE_RX.items() if rx.search(text)}
    if cued:
        kinds = set(cued)
    elif _is_question(text):
        return []
    else:
        kinds = set()
    kinds |= {k for k, rx in _CLAUSE_RX.items() if rx.search(text)}
    kinds |= {k for k, rx in _JOIN_RX.items() if rx.search(text)}
    if kinds:
        kinds |= {k for k, rx in _TOPIC_RX.items() if rx.search(text)}
    return sorted(kinds)


# --------------------------------------------------------------------------
# Request payload helpers
# --------------------------------------------------------------------------

MARKER = "[reminder]"


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def latest_user_text(request) -> str:
    """Newest human text in either OpenAI or Anthropic message shape."""
    messages = request.get("messages") if isinstance(request, dict) else None
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            if content.strip() and not content.startswith(MARKER):
                return content
            continue
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"
                     and isinstance(p.get("text"), str)
                     and not p.get("text", "").startswith(MARKER)]
            text = "\n".join(parts).strip()
            if text:
                return text
    return ""


def inject(request: dict, block: str, api_mode: str):
    """Attach the hint without breaking the provider's message contract.

    Anthropic Messages only accepts ``system`` at the top level, so the hint
    becomes an extra text block on the trailing user turn (after any cache
    breakpoints). Chat Completions gets a trailing system message, as before.
    Other API shapes are left untouched (returns None).
    """
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    messages = list(messages)
    mode = api_mode or "chat_completions"
    if mode == "anthropic_messages":
        last = messages[-1]
        if not isinstance(last, dict) or last.get("role") != "user":
            return None
        content = last.get("content")
        if isinstance(content, str):
            parts = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            parts = list(content)
        else:
            return None
        parts.append({"type": "text", "text": block})
        messages[-1] = {**last, "content": parts}
    elif mode == "chat_completions":
        messages.append({"role": "system", "content": block})
    else:
        return None
    new_request = dict(request)
    new_request["messages"] = messages
    return new_request


# --------------------------------------------------------------------------
# Tool touches -> action / verification evidence
# --------------------------------------------------------------------------

TERMINAL_TOOLS = {"terminal"}

_ENV_PREFIX = re.compile(
    r"^(?:sudo\s+(?:-\S+\s+)*|env\s+|time\s+|nohup\s+|exec\s+|"
    r"[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")


_ASSIGN = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)$")
_VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def _expand_vars(command: str) -> str:
    """Substitute simple `NAME=value` assignments made earlier in the same command."""
    values = {}
    out = []
    for part in re.split(r"(\s*(?:;|&&|\|\||\||\n)\s*)", command or ""):
        if values:
            part = _VAR.sub(lambda m: values.get(m.group(1), m.group(0)), part)
        found = _ASSIGN.match(part.strip())
        if found:
            values[found.group(1)] = found.group(2).strip("\"'")
        out.append(part)
    return "".join(out)


def segments(command: str) -> list:
    """Split a shell command into simple-command segments, normalized."""
    out = []
    for part in re.split(r"\s*(?:;|&&|\|\||\||\n)\s*", _expand_vars(command)):
        part = part.strip().lstrip("({ ").strip()
        part = _ENV_PREFIX.sub("", part)
        if not part:
            continue
        head, _, rest = part.partition(" ")
        head = head.rsplit("/", 1)[-1]
        out.append((head + (" " + rest if rest else "")).strip())
    return out


def _rx(pattern: str):
    return re.compile(r"^(?:" + pattern + r")(?:\s|$)", re.IGNORECASE)


_HP = r"hermes\s+(?:plugins?)"
_SC = r"systemctl\s+(?:--user\s+)?"
_EDIT = r"sed\s+(?:-\S+\s+)*-\S*i\S*|perl\s+-\S*i\S*|truncate"

# Related changes that also need re-verification for a kind ("edit" is an
# in-place shell edit or config write; it is not a checklist of its own).
SPILL = {
    "remove": ("schedule", "edit"),
    "install": ("update", "schedule", "edit"),
    "update": ("install", "edit"),
}

TERMINAL_ACTION = {
    "remove": _rx(
        r"rm|rmdir|unlink|shred|mv|trash|git\s+rm|crontab\s+-r|"
        + _HP + r"\s+(?:remove|rm|uninstall|disable)|hermes\s+config\s+unset|"
        r"hermes\s+cron\s+(?:remove|rm|delete|pause)|hermes\s+skills\s+uninstall|"
        r"hermes\s+mcp\s+remove|" + _SC + r"(?:disable|stop|mask)|"
        r"docker\s+(?:rm|rmi|volume\s+rm|compose\s+.*\bdown|stop)|"
        r"pip3?\s+uninstall|uv\s+pip\s+uninstall|apt(?:-get)?\s+(?:remove|purge)|"
        r"npm\s+(?:uninstall|rm)|sqlite3\s+.*\bdelete\b"),
    "install": _rx(
        r"pip3?\s+install|uv\s+(?:pip\s+install|add|sync)|npm\s+(?:i|install|ci)|"
        r"pnpm\s+(?:i|install|add)|apt(?:-get)?\s+install|brew\s+install|"
        r"cargo\s+install|go\s+install|"
        + _HP + r"\s+(?:install|enable|update)|hermes\s+skills\s+install|"
        r"hermes\s+mcp\s+add|hermes\s+config\s+set|hermes\s+cron\s+(?:create|add)|"
        + _SC + r"(?:enable|start|restart|daemon-reload)|"
        r"docker\s+(?:run|start|restart|compose\s+.*\b(?:up|restart))|git\s+clone|"
        r"cp|install|mkdir|ln\s+-s\S*|chmod|chown|tar\s+-?\S*x\S*|unzip|"
        r"make\s+install|" + _EDIT),
    "update": _rx(
        r"pip3?\s+install\s+.*(?:-U|--upgrade)|uv\s+(?:lock\s+--upgrade|sync)|"
        r"npm\s+(?:update|upgrade)|apt(?:-get)?\s+(?:upgrade|dist-upgrade)|"
        r"hermes\s+update|git\s+(?:pull|merge|rebase|checkout|reset)|"
        r"docker\s+(?:pull|compose\s+.*\bpull)|" + _HP + r"\s+update|"
        r"alembic\s+upgrade|\S*migrat\S*"),
    "schedule": _rx(
        r"crontab\s+(?:-e|-|\S+)|hermes\s+cron\s+(?:create|add|edit|resume|pause|remove)|"
        + _SC + r"(?:enable|disable)\s+.*\.timer|systemd-run|"
        r".*(?:>|tee\s|" + _EDIT + r").*(?:/etc/cron\.d/|\.timer\b|crontab)"),
    "secret": _rx(
        r"git\s+(?:add|commit|push)|"
        r".*(?:>|tee\s|" + _EDIT + r").*(?:\.env\b|\.key\b|\.pem\b|secret|token|credential)"),
    "edit": _rx(_EDIT + r"|hermes\s+config\s+(?:set|unset)"),
}

# Listing/status checks: evidence on their own (output shows the state).
_SUBCMD_CHECK = (r"\S+(?:\s+\S+){0,3}?\s+(?:status|check|doctor|health|healthz|verify|"
                 r"list|ls|show|info|ps|get)")
_SCRIPT_CHECK = (r"(?:python3?\s+|bash\s+|sh\s+|node\s+)?\S*(?:verify|check|test|"
                 r"health|smoke|probe)\S*")
TERMINAL_LISTING = {
    "remove": _rx(
        _HP + r"\s+(?:list|ls|show|info)|"
        r"hermes\s+(?:cron|config|mcp|skills)\s+(?:list|ls|get|show)|"
        r"crontab\s+-l|" + _SC + r"(?:status|is-active|is-enabled|list-units|"
        r"list-unit-files|list-timers|cat)|docker\s+(?:ps|images|volume\s+ls)|"
        r"pip3?\s+(?:show|list)|which|command\s+-v|curl|" + _SUBCMD_CHECK),
    "install": _rx(
        r"\S+\s+(?:--version|-V|version)|" + _HP + r"\s+(?:list|ls|show|info|doctor)|"
        r"hermes\s+(?:doctor|status)|hermes\s+config\s+(?:get|check)|"
        r"hermes\s+mcp\s+(?:list|test)|hermes\s+skills\s+list|"
        r"hermes\s+cron\s+(?:list|run|status)|"
        + _SC + r"(?:status|is-active|is-enabled)|journalctl|"
        r"docker\s+(?:ps|logs|inspect|exec)|curl|wget|pytest|"
        r"python3?\s+(?:-m\s+pytest|-c)|which|command\s+-v|sqlite3|"
        + _SUBCMD_CHECK + "|" + _SCRIPT_CHECK),
    "update": _rx(
        r"\S+\s+(?:--version|-V|version)|pip3?\s+show|"
        r"git\s+(?:log|rev-parse|describe|status)|docker\s+(?:inspect|images|ps)|"
        r"hermes\s+(?:status|doctor)|npm\s+(?:ls|list|view)|curl|"
        r"cat\s+\S*version\S*|" + _SUBCMD_CHECK + "|" + _SCRIPT_CHECK),
    "schedule": _rx(
        r"crontab\s+-l|hermes\s+cron\s+(?:list|status|run)|"
        + _SC + r"(?:list-timers|status)|cat\s+/etc/cron\.d\S*|"
        r"ls\s+/etc/cron\.d\S*|journalctl|tail\s+.*\.log"),
    "secret": _rx(
        r"git\s+(?:diff\s+--(?:cached|staged)|status\s+--ignored|check-ignore)|"
        r"stat|chmod\s+(?:0?600|0?700|go-rwx|o-rwx)|.*grep.*(?:KEY|TOKEN|SECRET|sk-)"),
}

# File probes: count only when they name something the action touched.
TERMINAL_PROBE = {
    "remove": _rx(r"ls|test|\[|find|stat|readlink|grep|egrep|rg|cat|head|tail|"
                  r"sqlite3|python3?\s+-c"),
    "schedule": _rx(r"grep|cat|tail|ls"),
}

_STOP = set("""
rm rmdir unlink mv ls cat grep egrep rg find stat test head tail sudo env echo
hermes plugin plugins config unset remove delete uninstall disable enable list show
root home usr bin etc var opt tmp lib local share dev proc run srv the and
docker systemctl user cron crontab pip pip3 npm python python3 git sqlite3 sed
path file_path files file json yaml yml txt log logs type name content pattern
target limit offset mode replace string new_string old_string true false null
none print import exit code output head_limit glob file_glob action
""".split())
_TOKEN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.\-]{2,}")
_GENERIC = set("""
hermes plugin plugins data agent backups backup config cache scripts root
home tmp skills logs state main test tests docs old new bak
""".split())


def _tokens(text: str) -> set:
    """Path/name tokens plus their -_. sub-parts (so `grep critic` names
    `agent-plugin-response-critic`)."""
    found = set()
    for raw in re.split(r"[\s'\"=,;:(){}\[\]|&<>*~]+", text or ""):
        for part in raw.split("/"):
            for tok in _TOKEN.findall(part):
                tok = tok.lower().strip(".-_")
                pieces = [tok] + re.split(r"[-_.]+", tok)
                for piece in pieces:
                    if (len(piece) >= 4 and piece not in _STOP and not piece.isdigit()
                            and piece not in _GENERIC):
                        found.add(piece)
    return found


_ACTION_FIELD = re.compile(r'"action"\s*:\s*"(\w+)"')
_PATH_FIELD = re.compile(r'"(?:path|file_path)"\s*:\s*"([^"]+)"')
_SECRET_PATH = re.compile(r"\.env\b|secret|token|credential|\.key\b|\.pem\b|auth\.json",
                          re.IGNORECASE)
_CRON_PATH = re.compile(r"/cron|\.timer\b|crontab", re.IGNORECASE)
_DOC_PATH = re.compile(r"\.(?:md|txt|rst)$|/docs?/|README", re.IGNORECASE)


def _tool_marks(tool: str, text: str):
    """(actions, listing_verifies, probe_verifies) kind sets for non-terminal tools."""
    action, listing, probe = set(), set(), set()
    name = tool.lower()
    actions = set(_ACTION_FIELD.findall(text))
    paths = _PATH_FIELD.findall(text)
    if "cron" in name:
        if actions & {"remove", "delete", "pause"}:
            action |= {"remove", "schedule"}
        if actions & {"create", "add", "update", "resume", "edit"}:
            action |= {"schedule", "install"}
        if actions & {"list", "status", "run", "get"}:
            listing |= {"schedule", "remove", "install"}
    if name == "skill_manage":
        if actions & {"delete", "remove_file"}:
            action.add("remove")
        if actions & {"create", "patch", "write_file"}:
            action.add("install")
    if name in ("write_file", "patch"):
        if not paths or not all(_DOC_PATH.search(p) for p in paths):
            action.add("install")
        if any(_SECRET_PATH.search(p) for p in paths):
            action.add("secret")
        if any(_CRON_PATH.search(p) for p in paths):
            action.add("schedule")
    if name in ("read_file", "search_files"):
        probe |= {"remove", "schedule"}
    if name == "skill_view":
        listing.add("install")
    return action, listing, probe


def classify(tool: str, text: str):
    """Return (actions, verifies) for one touch.

    actions:  {kind: (position, action_text)}  — earliest per kind
    verifies: {kind: [(position, probe_text_or_None), ...]} — every check in
              order; a probe text only counts if it names what the action touched.
    Terminal positions are segment indexes so `rm x && ls x` verifies in place.
    """
    actions, verifies = {}, {}
    if tool in TERMINAL_TOOLS:
        segs = segments(text)
        for pos, seg in enumerate(segs):
            for kind, rx in TERMINAL_ACTION.items():
                if rx.search(seg):
                    actions.setdefault(kind, (pos, seg))
            for kind, rx in TERMINAL_LISTING.items():
                if rx.search(seg):
                    verifies.setdefault(kind, []).append((pos, None))
            for kind, rx in TERMINAL_PROBE.items():
                if rx.search(seg):
                    # A pipeline such as `ls dir | grep name` names its target
                    # across segments, so a probe sees the rest of the command.
                    verifies.setdefault(kind, []).append((pos, " | ".join(segs[pos:])))
        return actions, verifies
    action, listing, probe = _tool_marks(tool, text)
    for kind in action:
        actions[kind] = (0, text)
    for kind in probe:
        verifies.setdefault(kind, []).append((0, text))
    for kind in listing:
        verifies.setdefault(kind, []).append((0, None))
    return actions, verifies


def evaluate(touches, kinds) -> dict:
    """Per kind: ("open"|"acted"|"verified", detail).

    touches: iterable of (id, tool, text, ran, ok) ordered by id.
    A kind is verified when a verification that actually ran comes after the
    latest successful change for that kind (plus related changes in SPILL).
    File probes must name what was changed.
    """
    state = {k: ("open", "") for k in kinds}
    pending = {}
    for tid, tool, text, ran, ok in touches:
        actions, verifies = classify(tool, text or "")
        if ok:
            for kind in kinds:
                hit = actions.get(kind)
                if hit is None:
                    hits = [v for k, v in actions.items() if k in SPILL.get(kind, ())]
                    hit = min(hits) if hits else None
                if hit is not None:
                    pos, atext = hit
                    prev = pending.get(kind)
                    toks = _tokens(atext) | (prev[3] if prev else set())
                    pending[kind] = (tid, pos, atext, toks)
                    state[kind] = ("acted", _snip(atext, 100))
        if not ran:
            continue
        for kind in list(pending):
            atid, apos, atext, toks = pending[kind]
            for vpos, probe in verifies.get(kind, ()):
                if (tid, vpos) <= (atid, apos):
                    continue
                # With no nameable target (e.g. an unresolved $VAR) any check counts.
                if probe is not None and toks and not (toks & _tokens(probe)):
                    continue
                state[kind] = ("verified",
                               f"verify: {_snip(probe or text, 120)} (after: {_snip(atext, 80)})")
                del pending[kind]
                break
    return state


# --------------------------------------------------------------------------
# Results, signatures, redaction
# --------------------------------------------------------------------------

_PREDICATES = {"grep", "egrep", "fgrep", "rg", "test", "[", "diff", "cmp",
               "pgrep", "which", "command"}


def _is_predicate(command: str) -> bool:
    segs = segments(command)
    return bool(segs) and segs[-1].split(" ", 1)[0] in _PREDICATES


def result_status(result, command: str = ""):
    """(ran, ok): ran=0 on tool-level error; ok=0 also on a meaningful nonzero exit."""
    data = None
    if isinstance(result, dict):
        data = result
    elif isinstance(result, str):
        stripped = result.lstrip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except Exception:
                data = None
    if isinstance(data, dict):
        if data.get("error") or data.get("success") is False:
            return 0, 0
        code = data.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            benign = code == 1 and _is_predicate(command)
            return 1, (1 if benign else 0)
    return 1, 1


def action_text(tool: str, payload) -> str:
    if isinstance(payload, dict):
        if tool in TERMINAL_TOOLS and isinstance(payload.get("command"), str):
            return payload["command"]
        try:
            return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        except Exception:
            return str(payload)
    return "" if payload is None else str(payload)


def signature(tool: str, text: str) -> str:
    segs = segments(text) if tool in TERMINAL_TOOLS else []
    core = segs[0] if segs else (text or "")
    core = re.sub(r"\d+", "#", re.sub(r"\s+", " ", core)).strip()
    return f"{tool}: {core[:80]}"


def result_hash(result) -> str:
    if result is None:
        return ""
    if not isinstance(result, str):
        try:
            result = json.dumps(result, sort_keys=True, default=str)
        except Exception:
            result = str(result)
    return short_hash(result)[:12]


_SECRETISH = [
    re.compile(r"(?i)\b((?:bearer|basic|token)\s+)(?=[A-Za-z0-9._~+/=\-]*\d)"
               r"[A-Za-z0-9._~+/=\-]{6,}"),
    re.compile(r"(?i)\b([A-Za-z_]*(?:key|token|secret|password|passwd|auth)[A-Za-z_]*)"
               r"(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|(?:bearer\s+|basic\s+)?\S+)"),
]
_OPAQUE = re.compile(r"\b(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{32,}\b")


def redact(text: str) -> str:
    """Best-effort local redaction (Hermes' redactor runs first when present)."""
    if not text:
        return ""
    out = _SECRETISH[0].sub(lambda m: m.group(1) + "***", text)
    out = _SECRETISH[1].sub(lambda m: m.group(1) + m.group(2) + "***", out)
    return _OPAQUE.sub("***", out)


def _snip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------
# Hint block
# --------------------------------------------------------------------------

def render(obligations, state: dict, failure=None, max_lines: int = 6) -> str:
    """obligations: [(kind, detail)]; state from evaluate(); failure: (sig, n)."""
    lines = []
    for kind, detail in obligations:
        status, info = state.get(kind, ("open", ""))
        if status == "verified":
            continue
        if status == "acted":
            lines.append(f"- ({kind}) action ran (`{info}`) but is not verified yet: "
                         "run a read-only check after it before claiming done.")
        else:
            lines.append(f"- ({kind}) {detail}")
    if failure:
        sig, count = failure
        lines.append(f"- (method) `{_snip(sig, 100)}` failed {count}x: switch "
                     "method and say in one line what differs from the last attempt.")
    if not lines:
        return ""
    header = (f"{MARKER} Open checks for this request. Verify with fresh tool "
              "output; do not just claim:")
    return "\n".join([header] + lines[: max_lines - 1])
