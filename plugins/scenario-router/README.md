# Scenario router v0.3.0: outcome-only Jev reviewer

The agent **always runs normally first**, including its own memory storage and
readback. Jev then judges the outcome. There is no incoming scenario classifier,
pre-run deterministic scenario policy, skipped agent run, or initial Jev request.
The historical package name is retained; v0.2 does not route models.

Production enablement/configuration is unchanged by this package update. No core
patches, response-critic edits, provider switches, or external repository writes
are required. Python 3.10+ and `httpx` are the standalone dependencies.

## Implemented behavior

- Atomic OpenRouter Decisions questions (`typesafe/jev-1.13`): `outcome`,
  `verdict`, and `memory_evidence`. Not chat/completions or free-form generated
  feedback. English and Russian synthetic examples are supplied.
- Strict label/distribution/confidence validation; errors, missing keys, invalid
  probabilities, and insufficient relevant confidence abstain toward a full verifier.
  Memory evidence confidence gates note acknowledgments, not unrelated technical
  failure, missing-input, refusal or handoff classifications. Normal-answer
  acceptance additionally requires high-confidence readiness; note acknowledgment
  keeps the stricter thresholds on all three dimensions.
- Fixed actionable recommendations for supported answers, corrections, authorized
  technical recovery, missing input, safe refusals, notes, and async handoffs.
- A note-only success proposes **`Added.`** only with high-confidence semantic
  evidence of an executed memory write **and matching readback**. Nonempty evidence,
  `memory_evidence=confirmed`, `verdict=ready`, and all three confidences at least
  `brain_dump_threshold` are required. Internal events and pending work never get
  this acknowledgment. Notes combined with real questions/tasks require normal
  answers. A verbose generated note essay can be replaced by `Added.` **by an
  authorized consumer after full verification**, not by this plugin.
- The public synchronous tool supports a cooperative verifier. Every recommendation
  has `applied:false` and `verifier_effort:"max"`. These fields are advice, not an
  assertion that another plugin's configuration or delivered response changed.
- A bounded in-memory turn capture records the latest redacted prompt, trusted
  provenance, and actual redacted tool evidence digests. Persisted `last_decision`
  and bounded `review_history` contain only validated labels, fixed feedback,
  probabilities, numeric usage, and hashed session/turn identifiers. No personal
  notes, response text, raw evidence, raw IDs, or unvalidated model fields are
  written to plugin state or plugin logs.
- CLI evaluation against independently authored expected scenario/disposition
  labels, with metadata-only audit JSONL, counts, confusion pairs, and numeric usage.

**Not implemented:** storing notes itself, exact remote memory execution checks,
message suppression/rewrites, launching or canceling background work, continuing
the agent itself, main-model/provider switching, sticky fallback routes, or
universal gateway event interception. Strong execution verification and consumer
integration remain the full verifier's responsibility. Jev confidence measures
probability concentration, not proof or calibrated correctness.

## Public verifier interface

When the plugin is enabled, it registers `scenario_review_outcome` using
`ctx.register_tool` in toolset `scenario-review`. A disabled plugin registers
nothing; no changes are made to global toolsets or the core. Example consumer:

```python
import json
result = json.loads(ctx.dispatch_tool("scenario_review_outcome", {
    "user_message": original_user_message,       # full current original prompt
    "assistant_response": final_agent_draft,    # after its actual tool run
    "evidence": redacted_actual_execution_digest,
    "internal": trusted_internal_provenance,
    "pending_background": trusted_pending_state,
}))
# Shadow/off results must not affect response delivery or pending background work.
# Active results remain proposals; the cooperative full verifier owns any action.
```

All five arguments are required, exactly: the first three are strings and the
last two are booleans. The handler returns a **JSON string** with this envelope:

```json
{
  "ok": true,
  "mode": "shadow",
  "review": {
    "disposition": "acknowledge",
    "scenario": "brain_dump_added",
    "confidence": 0.99,
    "acknowledgment": "Added.",
    "feedback": "fixed allowlisted actionable text",
    "verifier_effort": "max",
    "applied": false
  },
  "answers": {},
  "usage": {}
}
```

The values above illustrate the shape, **not a live API response**. `answers`
contains only validated Choice fields (`type`, `choice`, `probabilities`,
`confidence`); unvalidated extras are dropped. `usage` contains only finite
nonnegative numeric `input_tokens`, `output_tokens`, `cost`, when supplied.
`acknowledgment` is otherwise the empty string. `ok=false` means unavailable,
transport/validation failure, or insufficient confidence, not task completion.
A valid ambiguous result can have `ok=true` but disposition `uncertain`.
Consumers must check **mode and disposition**, not just `ok`.

Outcome labels:
`normal_answer`, `brain_dump_added`, `brain_dump_failed`, `technical_failure`,
`missing_input`, `safety_refusal`, `async_handoff`, `ambiguous`.

Verdict labels: `ready`, `correction_needed`, `uncertain`.
Memory-evidence labels: `confirmed`, `not_confirmed`, `not_applicable`.
Dispositions: `accept`, `correct`, `recover`, `acknowledge`, `handoff`,
`uncertain`, `refusal`. Local failures use scenario `uncertain`.

Refusal feedback requires a safe alternative while preserving the boundary;
there is **no alternate-model safety bypass**. Technical recovery recommends an
appropriate authorized tool/backend alternative and verified results, not a main
model switch. Missing authorization must be requested, not manufactured. A
cooperative verifier may continue the agent with these recommendations; this
plugin never dispatches agent-action tools itself.

## Modes, hooks, privacy, and budgets

Settings belong under `plugins.entries.scenario-router.settings` if an operator
later chooses to configure the plugin. Updating files does not enable it.

```yaml
mode: shadow                      # shadow | active | 'off' (quote YAML off)
judge_model: typesafe/jev-1.13
confidence_threshold: 0.90         # provisional, not calibrated
brain_dump_threshold: 0.97
timeout_seconds: 8
max_input_characters: 48000        # full serialized redacted state, characters
max_cached_turns: 128
max_review_history: 64
max_tool_events: 32
max_tool_characters: 12000         # per-event evidence digest cap
```

- `shadow` (default): `pre_llm_call` **captures only**, `post_tool_call`
  collects redacted actual evidence, and `post_llm_call` enqueues observation then
  returns without waiting for the API. One profile-scoped worker records metadata.
  Queue size is bounded (default 8), observations older than 30 seconds are dropped,
  unload closes the worker without waiting for HTTP and discards late results.
  Child/subagent observations are skipped; the main result carries their evidence.
  Every hook returns `None`; no context injection,
  delivered-text rewrite, agent nudge, verifier control, or background control.
  Explicit tool reviews also return `mode=shadow` and must remain observational.
- `active`: explicit synchronous tool reviews are intended for an opt-in
  cooperative verifier. No automatic post-LLM review is made, avoiding a duplicate
  after active verification. Hooks remain capture-only; no plugin-owned actions.
- `'off'`: tool availability check is false and a direct dispatch returns an
  unavailable/uncertain envelope without a Jev request. Disabled plugin vs
  enabled plugin in off mode are distinct.

No decision cache is shared between hooks and explicit tool calls. The critic
does not dispatch the outcome tool in shadow mode, avoiding a duplicate inline
request. An operator's explicit tool review still makes a separate synchronous
request; automatic shadow observation is asynchronous and never starts an agent.
A pre-run capture never requests Jev. Reviewer output is excluded from evidence.

Internal notification status comes from trusted current history metadata
(`display_kind=internal_notification` or host verifier-nudge flags), not matching
bracketed words in human text. Captured internal/subagent flows are observable;
there is no universal promise of coverage for events that never reach public
hooks. Unique turn-ID fallback handles session-ID rotation during compaction;
ambiguous concurrent matches are not joined. Explicit session reset drops only
that session's pending captures. The evidence hook requires runtime session/turn
correlation; events without matching IDs are not guessed into a turn.

The host `agent.redact.redact_sensitive_text` is used with forced secret and URL
credential redaction. Standalone CLI use without Hermes has a conservative regex
fallback, **not comprehensive PII removal**. Redaction failure drops evidence.
Tool digests can be shortened with an explicit incomplete-evidence marker;
original notes and final drafts are **never silently truncated**. Oversized full
state abstains before HTTP. A cap is not a context-token guarantee. Missing key,
HTTP/transport errors, malformed responses, and low confidence return uncertainty.

The original prompt, draft, and redacted evidence are sent to OpenRouter/TypeSafe
when a real review is requested. Secret redaction is not consent or anonymization;
apply the operator's privacy and external-data consent rules before enabling or
passing private text. Plugin logs/state do not include it, but the host's general
session history may record tool calls according to its own policy.

Migration from v0.1: incoming intent/complexity classification and all input-policy
functions are removed. `classifier_model` becomes `judge_model`; reasoning,
fallback model, and verifier-effort settings no longer apply. Verifier effort is
fixed to the recommendation `max`. Legacy `last_decision` metadata is replaced on
the next review; old decision logs are not rewritten.

## Replay/evaluation without production enablement

From the plugin directory, with dependencies available:

```sh
# Deterministic replay of explicitly SYNTHETIC test Decisions; no API call.
python __init__.py --evaluate examples/synthetic-outcomes.jsonl \
  --output examples/replay-audit.jsonl

# One synthetic note state + explicitly stored synthetic decision.
python __init__.py --state-file examples/note.json \
  --decision-file examples/synthetic-note-decision.json

# Explicitly billed live evaluation; API key must already be in the environment.
python __init__.py --evaluate examples/synthetic-outcomes.jsonl --live \
  --output examples/live-audit.jsonl

# One real supplied outcome; the agent has already executed the work.
python __init__.py --state-file path/to/outcome-state.json --live
```

`--output` creates a **new** metadata-only audit file and refuses overwrite. The
CLI does not load Hermes plugins, write configuration, or start an agent.
Use portable paths of your choosing; do not put API keys in state/fixture files.

Each nonempty fixture JSONL line requires:

```json
{
  "id": "unique-example-id",
  "state": {
    "user_message": "complete prompt",
    "assistant_response": "final draft",
    "evidence": "redacted actual results",
    "internal": false,
    "pending_background": false
  },
  "expected": {"scenario": "normal_answer", "disposition": "accept"},
  "decision_origin": "stored_live",
  "decision_response": {"answers": {}}
}
```

This minimal shape is illustrative; the empty answers above deliberately do not
constitute a valid decision. Offline replay requires a supplied stored Decisions
response; it **never fabricates one**. `--live` ignores stored responses and makes
real Jev requests. Origins are `synthetic_unit_test`, `stored_live`, or
`stored_unspecified`; origins do not independently authenticate caller files.
Bundled decisions are clearly marked `synthetic_unit_test` and validate code
behavior, **not live Jev accuracy**. Expected labels are separately authored;
they are never derived from actual model decisions. Ground-truth changes should
be reviewed independently when calibrating live thresholds.

Reports include total/matched/mismatched, valid reviews, abstentions, failed
requests, malformed responses, scenario-confusion pairs, usage, and whether any
replayed response is synthetic. Expected low-confidence abstention can match
successfully. Exit status: `0` for matching evaluation without request/malformed
response failures (or valid single review), `1` for mismatch/review failure,
`2` for invalid input/CLI usage. No cached or synthetic fallback replaces failed
live API calls. Audit rows hash fixture IDs and omit all state text. Audits retain
validated Decisions answers and expected/actual labels; replay also needs the
original explicitly supplied state fixtures, not an audit alone.

## Tests

```sh
PYTHONPATH=path/to/hermes-agent python -m pytest tests -o addopts='' -q
```

Tests cover public schema, mocked HTTP transport at the real Decisions endpoint,
confidence/error abstention, write/readback semantics, acknowledgment gating,
mixed-note answer behavior, refusal/no-bypass recommendations, pending/internal
handling, redaction and metadata-only state, cache/history bounds, reset and
session rotation, offline/live evaluation paths, CLI audit output, and real
Hermes `PluginManager` discovery + `ctx.dispatch_tool` in isolated test homes.
No test invokes live Jev or enables the installed production plugin. Dependency
mocking in tests is explicitly synthetic. Live evaluation is a separate opt-in
command and cannot establish accuracy from synthetic replay results.

References:
- https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/
- https://openrouter.ai/docs/guides/community/jev
- https://docs.typesafe.ai/confidence
