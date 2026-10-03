# reminder

Asks for proof before "done". When a request changes something (remove,
install/set up, update, schedule), reminder opens a short checklist for that
turn, watches the tool calls, and nudges the model until a read-only check
shows the result.

## How it works

1. **Request.** The `llm_request` middleware reads the turn's user message.
   Only imperative requests open checks ("remove X", "I want you to install
   Y", "и удали Z"). Questions, design discussion, quoted terminal output and
   internal/background notices open nothing.
2. **Evidence.** The `tool_execution` middleware records every tool call for
   the turn: tool, redacted arguments, whether it ran and succeeded, a
   signature and a result hash. Keys come from the Hermes `turn_id`, so two
   chats or two turns with the same text never share state.
3. **Hint.** Before each model call, open checks are injected as a short
   `[reminder]` block: "action ran but not verified yet — run a read-only
   check". On Anthropic Messages the block is appended as a text part of the
   last user turn (a `system` entry inside `messages` would replace the whole
   system prompt); Chat Completions gets a trailing system message; other API
   shapes are left untouched.
4. **Auto-close.** A check closes when a verification that actually ran comes
   after the latest successful change: a status/list command (`hermes plugins
   list`, `crontab -l`, `systemctl status`, `… verify.py`), or a file probe
   (`ls`, `grep`, `read_file`) that names what was changed. Shell variables
   assigned earlier in the same command are expanded. A later related change
   reopens the check.
5. **Stuck loops.** The same call failing twice adds "switch method and say
   what differs".

It only nudges: it never blocks a tool call or edits the reply. Local SQLite
and regexes only — no network, no model calls; every middleware body swallows
its own errors.

## Storage

`$HERMES_HOME/reminder/ledger.db` (directory 0700, file 0600): `tasks`,
`obligations` (open/done/expired), `touches`. Opening a 0.2 ledger migrates it
in place and marks its old never-closed checks `expired`. Touches of turns
that opened no checklist are pruned after 14 days. `REMINDER_DB` overrides
the path.

`flush.py` (system cron, every 30 min) syncs a few meaningful rows — new
checklists, verified resolutions, tool failures inside checklist turns — to
the Graphiti `infra` group, at most 8 triplets per run.

## Install

```bash
cp -r plugins/reminder "$HERMES_HOME/plugins/"
hermes plugins enable reminder
```

No credentials or settings.

## Tests

```bash
python3 plugins/reminder/tests/test_ledger.py
```

Pure tests (temp SQLite, no Hermes, no network): request detection, evidence
rules, injection shapes per API mode, ledger migration and the middleware
end to end.
