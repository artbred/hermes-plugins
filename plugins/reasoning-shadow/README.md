# reasoning-shadow

**Retired on 2026-10-02.** Explicitly disabled locally and on `kuzin`; code and private example stores are preserved for reference. The configuration and commands below describe the archived implementation, not recommended activation.

Independent, **passive** main-agent reasoning calibration. Defaults to `off`;
`shadow` is the only other mode. It does not change main effort, provider,
reviewers, tools, delivery, system/user prompts or agent execution. All lifecycle
callbacks return `None`; there are no tools, transforms or middleware.

## Data and timing

At the public `pre_llm_call` seam, one immutable redacted human-input
snapshot is queued per `(session_id, turn_id)` **only for audited Telegram
human inputs**. The host must provide `platform=telegram`, a nonempty
`sender_id`, and a current user history row with nonempty `platform_message_id`.
Missing provenance, cron, API, CLI, webhook and child origins abstain; inheriting
a Telegram session identity is insufficient (goal/loop resumes have no inbound
ID). Internal/background/parent/verifier and synthetic metadata are excluded;
serialized or malformed display metadata abstains. These are host metadata
checks, never text matching: human quotations of machine markers remain human.
Both session/turn IDs are required, and raw sender/inbound IDs are not retained. The snapshot contains the full request and
last six visible user/assistant messages **before** the current input; never
system/tool bodies, hidden reasoning, current outputs or critic verdicts.

One daemon worker uses OpenRouter's typed Decisions API
`https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13`, to recommend
family/risk/main effort. Credentials resolve via Hermes' profile-scoped
`agent.secret_scope.get_secret` during registration; no keys are copied into
plugin settings. Like the existing Jev advisor, shadow mode sends redacted input
and selected earlier examples to this external provider. Enabling it requires
that privacy/consent decision. No embeddings, agents, replay or tool execution.

The queue is bounded at 64, its inference TTL at 600 seconds, and the provider
request timeout at 12 seconds. Hooks never perform network, filesystem or FTS
operations. A blocked worker does not block them. Unload does not join a network
call. A shared cancellation lifecycle fences private file creation, persistent
journal setup and COMMIT, and rolls back in-flight example/FTS/annotation/
metadata/schema writes. Paused insert/update workers cannot commit after unload
returns; late network predictions are discarded. The narrow journal-mode gate
uses zero SQLite busy timeout rather than blocking unload behind a DB lock. Metadata events preserve queue order;
saturation may drop observations, reflected in the collector's in-memory
`metrics`. Actual API/tool counts are deduplicated with bounded event keys.

Requests/context over the configured 48,000-character limit produce a private
hash/length diagnostic, **not a classified partial request**. Similar examples
are complete, bounded to five/12,000 characters, and must have both source and
capture timestamps before the snapshot cutoff. Operator labels are selected
as-of that cutoff. Future annotations, outputs and the current source are not
examples. Records marked `synthetic: true` are excluded from default FTS
retrieval; local explicit inspection may use `Pool.similar(...,
include_synthetic=True)`. Unknown historical outcomes remain explicitly unverified.

Recommendations are audit only (`live_eligible: false`). Low confidence,
uncertain risk and malformed responses conservatively recommend high; elevated
risk recommends max. Raw effort and validated choice distributions are retained
separately. Confidence measures Jev's distribution concentration, **not empirical
correctness or proof that lower effort preserves quality**.

## Private storage

The worker lazily initializes `$HERMES_HOME/reasoning-shadow/pool.db`, never
another profile. Directory mode is `0700`; DB/WAL/SHM modes are `0600`. SQLite WAL,
transactions, operation-local connections, request-only Unicode61 FTS5 and
atomic retention support concurrent collection/CLI use. Defaults retain at most
2,000 examples and 90 days. Independent observer/CLI pools reserve SQLite's
writer before reading, so concurrent metadata and operator updates serialize
without holding the unload gate during lock waits. Annotation history has a
fixed budget of 32 changes per example per kind (prediction status and operator
outcome), at most 64 history rows per example. Identical consecutive labels and
sources coalesce without advancing their annotation timestamp; actual/prediction
metadata still updates. Initialization and retention also cap legacy histories.
Cutoffs before the oldest retained kind return `unknown`, never a newer label.
Technical failure backoff persists in this private
DB (30 to 300 seconds); low-confidence valid answers are not transport failures.

The pre-input snapshot records the redacted target model, with wire effort
initially unknown. Post-API observers retain only model/provider, actual requested wire effort,
API durations and token counts. Post-tool observers retain counts/errors, not
arguments/results. Post-LLM records wall duration/completion timestamp without
retaining a final response. Wire effort is `unknown` when unavailable; it is not
assumed high. Nothing automatically assigns success from `Added`, an assistant
claim, or a critic pass. Only explicit operator annotation assigns
`verified_success`, `failed` or `corrected`.

## Offline CLI

Use the installed Hermes Python environment so forced host redaction is present:

```sh
python plugins/reasoning-shadow/scripts/pool_cli.py report
python plugins/reasoning-shadow/scripts/pool_cli.py import-history --limit 1000
python plugins/reasoning-shadow/scripts/pool_cli.py import-history --source "$HERMES_HOME/state.db" --limit 1500
python plugins/reasoning-shadow/scripts/pool_cli.py annotate SOURCE_HANDLE --outcome verified_success
python plugins/reasoning-shadow/scripts/pool_cli.py annotate SOURCE_HANDLE --outcome failed
python plugins/reasoning-shadow/scripts/pool_cli.py annotate SOURCE_HANDLE --outcome corrected
```

`report` initializes the pool explicitly and prints aggregate metadata only.
The importer is strictly offline/read-only for the source, uses explicit SQL
columns (never hidden reasoning or session `model_config`), and defaults to
Telegram human primary sessions within the active profile/90-day window. It
preserves genuine `active=0` originals after compaction. Explicit machine
metadata, compressed summaries and legacy anchored machine envelopes without
human provenance are excluded; human quotations of markers remain eligible.
`--sources telegram cli` explicitly broadens eligible sources. `--source` or
`--home` explicitly authorizes that path; defaults never scan other profiles.
Unknown provenance is excluded and reported. Imports are deterministic/deduped,
with original source timestamps and strictly earlier same-session context.
No source trace/raw log is emitted. Reports do not expose private handles or
requests: an authorized local reader can obtain handles from the private DB to
annotate. Every imported example starts with outcome `unknown`.

## Settings

All settings are under `plugins.entries.reasoning-shadow.settings`:
`mode`, `classifier_model`, `timeout_seconds`, `max_input_characters`,
`confidence_threshold`, `queue_size`, `queue_ttl_seconds`, `max_examples`,
`retention_days`, `max_cached_turns`, `max_event_keys`, `allow_synthetic_capture`.
The last setting defaults to false and is only for isolated manual smoke
harnesses: opt in, then call `pre_llm_call` with `platform=synthetic_debug` and
`synthetic=True`. Such records use `live:synthetic_debug` provenance and are
never labeled authenticated human or used as default prior examples. Opt-in
does not permit synthetic Telegram messages or bypass internal/parent guards.
Do not enable this setting in the live profile. Budgets cannot exceed
manifest defaults, and the confidence floor is 0.90. No `active` mode exists.

## Verification

```sh
PYTHONPATH=/usr/local/lib/hermes-agent:/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages \
/root/.hermes/cache/scratch/jev-active-test-env/bin/python -m pytest \
plugins/reasoning-shadow/tests -o addopts='' -q
```

Tests use synthetic requests/IDs and stored synthetic decisions or mock HTTP
transports. They exercise real installed Python 3.11 PluginManager discovery,
hook registration/dispatch/unload and sanitized wire-request shape, plus pool,
CLI, causal cutoff, privacy, retention, permissions, concurrency, failure cache,
nonblocking hooks and late-return cases, plus installed-host goal/human event
staging and cancellation inside insert/transaction/initialization boundaries. They do not constitute a live quality
or speed comparison between reasoning efforts.

Integration helpers: `Collector(ctx)`, `collector.before/pre_api/post_api/tool/
after/reset`, `collector.client` (`Jev` or a fixture), `collector.jobs`,
`collector.metrics`, `Pool(home)`, `Pool.report/similar/annotate`,
`recommendation(payload)`, and CLI `import_history(home, source, limit)`.
Production must never call `jobs.join()`; only tests/smoke harnesses drain it.
