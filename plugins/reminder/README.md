# reminder

Gatekeeper ledger for Hermes. Derives checklists from request verbs, records
tool evidence per task, and injects open checks back into the run while it
is still fixable.

## How it works

- `llm_request` middleware reads the latest user message, detects verbs
  (`remove`, `install`, `secret`, `schedule`, `update`), and opens one
  checklist obligation per verb — once per task (content hash).
- `tool_execution` middleware records every tool call (tool, argument
  summary, ok/error) against the current task.
- On the next turn the open checks are injected as a `[reminder]` system
  block: verify-with-evidence items, capped at 8 lines.

Storage is local SQLite (`~/.hermes/reminder/ledger.db`): tasks, obligations,
touches. No network, no Jev calls, no added latency. All middleware bodies
are exception-swallowed — the plugin can never break a run.

The long-lived system map (components, data flow, past failures) lives in
Graphiti group `infra` on Neo4j; this ledger is the working set. `flush.py`
syncs selectively (new tasks, verified resolutions, tool failures — never
routine touches, max 8 triplets per run) via system cron every 30 minutes.

## Install

```bash
hermes plugins install <git-url> --enable
```

(or copy `plugins/reminder/` into `$HERMES_HOME/plugins/` per this repo's README)

## Configure

No credentials. Optional: point at another DB by editing `ledger.default_db`.

## Tests

```bash
python3 -c "import sys; sys.path.insert(0, 'tests'); import test_ledger"  # or pytest
```

Pure-function tests only (temp SQLite, no Hermes, no network).
