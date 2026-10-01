# Main reasoning shadow engine

## Scope

`reasoning-shadow` is separate from the active outcome-review pair. Default mode is off; its only other mode is shadow. It collects suggestions for main-agent low/high/max without applying them. Main execution, approvals, tools, reply delivery and reviewer high/max policy are unchanged. There is no active routing mode, model override or replay executor.

## Causal inputs

The authenticated Telegram pre-LLM hook captures one immutable, redacted request plus an explicit last-six-visible-message context window. Positive host sender and inbound-message provenance is required; scheduled/background/child/verification/internal/generated inputs do not become human examples. An explicit synthetic-debug capture mode exists only for isolated tests; production leaves it disabled. Human quotations are still data, not instructions.

A bounded worker—not a delivery-blocking hook—selects up to five complete earlier request examples from local Unicode FTS5. Source/capture timestamps and versioned annotations must precede the captured request. Synthetic examples are excluded by default. The current draft, tool evidence, later usage and reviewer verdict are never selector inputs. Overlarge full requests produce hash/length diagnostics, not predictions from silently truncated requests.

The worker asks Jev `typesafe/jev-1.13` for family, risk and main effort using the typed Decisions endpoint. This sends redacted request/context/selected prior examples to the same external advisor provider; it is not wholly local inference. Low-confidence/uncertain results stay conservative. Confidence is distribution concentration, not measured success probability; `live_eligible` remains false even for confident low recommendations.

## Private pool and analysis

`$HERMES_HOME/reasoning-shadow/pool.db` is private to the active profile. Directory mode 0700 and DB/WAL/SHM 0600 protect content from other local users; they are not encryption or protection from the host root account. Default retention is 2,000 examples and 90 days. Personal requests never belong in this public repository.

The offline importer reads explicit columns from state.db, not hidden reasoning, tool bodies or session secret configuration. Historical primary Telegram requests are unverified examples, not demonstrations of successful execution. Failed/corrected/verified-success annotations require explicit operator input. An assistant acknowledgment or reviewer pass does not assign success.

Metadata-only reports show counts, recommendations versus actual observed wire effort, prediction statuses and usage/API duration aggregates. A mismatch only means the recommendation differed from the run; it does not show that the recommendation was wrong. Unknown historical usage/effort stays unknown. Private source handles allow authorized future inspection/annotation without publishing contents.

The queue is bounded, does not wait on production delivery, and may drop observations under saturation; in-memory diagnostics track drops/errors. Provider technical failures back off persistently; ordinary uncertainty is not a transport failure. Cancellation fences durable writes at unload and late network results cannot persist. This is a diagnostic observer, not a guaranteed-lossless event ledger.

## Checks exercised

- All three plugin suites: 725 tests passed after provenance, synthetic exclusion, unload-race, independent-writer concurrency and bounded-history regression fixes.
- Real installed PluginManager discovery and callback dispatch are covered; tests also exercise actual host-staged Telegram inputs and sanitized wire-request shape.
- An isolated public synthetic-debug request used real Jev. It returned a valid low-risk conversation/low recommendation with 0.99 concentration; the result remained `live_eligible: false`. The callback took approximately one millisecond in this single smoke run; this is not a production latency guarantee.
- The current future-output marker was absent from the selector transport and private record. No task, external write, replay or agent was executed by that smoke.
- Real history import was exercised offline in a scratch pool; imported records retained unknown outcomes and private permissions.

## What is not established

Shadow suggestions do not establish correctness or speed at low effort. A future controlled comparison must run matched low/high cases with frozen/read-only tools, explicit task-result checks and no repeated external writes. Measure total latency including selector overhead, failure recovery and quality—not just model inference duration. Only after that evidence and a supported authenticated per-turn effort mechanism should narrow live request-family routing be considered.

See [plugin controls and CLI](../plugins/reasoning-shadow/README.md) and [main-reasoning design](main-reasoning-pool.md).
