# Response Critic

A standalone Hermes plugin for bounded, pre-delivery verification. Install under
`$HERMES_HOME/plugins/response-critic/` and enable through `plugins.enabled`.
It does not replace Hermes source, agent models, tool permissions, or gateway send methods.
The original agent always runs with its normal toolset before outcome review.

## Cooperative outcome review (1.5.0)

Settings live under `plugins.entries.response-critic.settings`:

```yaml
outcome_review_enabled: false # standalone default; no scenario-router dependency
outcome_review_mode: shadow   # shadow or active
critic_mode: active           # active (default), shadow, or off
```

When enabled, the critic probes `ctx.has_plugin("scenario-router")` at review time
and invokes only the public, profile-scoped
`ctx.dispatch_tool("scenario_review_outcome", arguments)` interface. It does not
import scenario-router internals or issue a second direct classifier HTTP request.
The arguments are `user_message`, `assistant_response`, `evidence`, `internal`, and
`pending_background`. The tool may return a JSON string or a decoded object.

- **Outcome shadow:** do not call Jev from the delivery-critical verification hook.
  The scenario plugin observes asynchronously after the completed run. The active
  standalone Kimi/OpenRouter critic keeps its own verification behavior; Jev never
  changes delivery, requests a continuation or starts a new agent in shadow mode.
- **Active:** application requires critic mode, local outcome-review mode, and the
  tool's returned mode all to be `active`, plus confidence of at least **0.97**.
  `accept` can skip full verification. `handoff` additionally requires actual
  session-scoped pending runtime work, never a promise alone.
- `correct` and `recover` return specific feedback through the existing `pre_verify`
  continuation gate to the **original agent with full tools**. They share the existing
  iteration cap. Recovery guidance requires inspecting failure, authorization, and
  current external state before retrying; already-executed external actions must not
  be automatically repeated. This is guidance, not a new pre-execution action gate.
- `acknowledge` may transform the final answer to exactly **`Added.`** only for a
  human turn, after high-confidence Jev approval and current-turn structured memory
  write/readback evidence (or a successful write tool's explicit `verified: true`).
  Failed, staged, historical, or unknown-format memory results do not authorize it.
  Missing evidence falls back to full verification, not a success acknowledgment.
- Uncertain, low-confidence, missing, malformed, or failed outcome results fall back
  to full maximum-effort verification in active cooperative mode. `refusal` never
  triggers recovery or alternate-provider bypass. Verifier availability failures
  retain the original fail-open behavior.
- `critic_mode: shadow` and `off` perform no inline judge calls, continuations,
  output rewrites or verification-sentinel mutations. Use the separate scenario
  plugin for asynchronous shadow observation.

This is an **outcome-only** integration. It does not short-circuit initial execution,
store notes itself, classify requests before execution, limit the agent's tools,
change a sticky provider, or guarantee prevention of tool side effects. Other plugins'
`pre_verify` hooks are independent and may still request a continuation.

## Judge fallback chain

1. Kimi K3 on the Kimi Coding chat-completions wire.
2. OpenRouter: `meta/muse-spark-1.3-contributor`, `reasoning.effort: max`.

Quota, authentication, network errors, and invalid verdicts advance directly from
Kimi to OpenRouter. A valid `passed: false` challenge is not bypassed. The OpenRouter
request requires parameter support and reserves **16,384 output tokens**; unsupported
maximum reasoning is not retried at lower effort. Non-maximum `fallback_effort`
settings are normalized to `max`. No subscription login helper or credential store
is used. Standalone Kimi effort selection remains controlled by `min_effort` and
`max_effort`; active cooperative abstentions use `max`.

The Contributor tier permits Meta to use prompts and outputs for model improvement.
Review sends redacted request, draft, and evidence text to external providers; enable
this only where that privacy boundary is acceptable. Caller wait is capped by
`review_budget_seconds` (default 30 seconds), including active cooperative review.
Late inference results cannot produce a continuation. Two slots bound abandoned
network workers; saturation fails open instead of spawning unlimited workers.
Quota/auth HTTP 401/403/429 responses cool down that judge for
`provider_cooldown_seconds` (default 300); reasoning remains `max` on OpenRouter.
The installed profile permits one correction instead of five. This does not bound
the original agent's tool/model work or the time spent producing a corrected draft.
Main-agent results cover subagents; duplicate child critic passes are skipped.

## Hooks and compatibility

- `pre_gateway_dispatch`: observes a legacy exact-match internal provenance bridge;
  never skips dispatch or authorization. Modern internal wakes can bypass this hook.
- `pre_llm_call`: captures the current request, turn identity, and host-authored current
  history `display_kind=internal_notification`. Older notifications and machine-looking
  human quotations do not make a human turn internal.
- `pre_api_request`: compatibility adapter opens Hermes' existing `pre_verify` gate
  for non-file turns by inserting an in-memory `.md` sentinel. This creates no file,
  but accesses the **private** `_turn_file_mutation_paths` attribute; it is not a fully
  public universal verification seam. Incompatible host versions can disable this gate.
- `pre_verify`: reviews a completed draft and returns bounded continuation feedback.
- `transform_llm_output`: turns exact trusted internal duplicates into `NO_REPLY`, or
  applies an authorized `Added.` acknowledgment. State is scoped to session/turn/draft,
  consumed at transformation, reset on next input, and bounded. Both observer-before-
  transform and transform-before-observer ordering are covered by regressions.
- `post_llm_call`: remembers final answers, not pending-work handoffs. It intentionally
  preserves current-turn context until transformation, avoiding the old cleanup race.

Evidence is current-turn only and excludes synthetic verifier nudges and host-authored
internal notices when selecting the latest human request. Actual mid-turn user steering
wins; text prefixes alone are not discarded. The evidence adapter uses a stack/agent
history compatibility fallback when the hook itself carries no conversation.

Short refactor/test/research handoffs are accepted only when the runtime delegation
registry confirms pending work for that session. When runtime reports no pending work,
those promises receive normal verification. After an internal completion, exact repeats
are suppressed; paraphrased duplicates remain judge-assisted, not a semantic guarantee.

## Privacy and tests

The host's `agent.redact.redact_for_egress` scrubs full draft, request, evidence, and
prior-feedback strings **before truncation or model egress**. Redaction failure never
returns the raw string. Logs contain verdict metadata and HTTP status/error types,
not provider response bodies, tool output, raw classifier answers, or feedback snippets.
Redaction is a defense, not a claim that arbitrary personal information is removed.

```bash
PYTHONPATH=/path/to/hermes-agent /path/to/test-venv/bin/python \
  -m pytest /path/to/response-critic/tests -o addopts='' -q
```

Tests are offline: judge responses, memory results, and runtime pending status are
explicit mocks, not live provider or persistence results. Real PluginManager tests
load this plugin and a test-only cooperative tool into temporary profile scopes.
Coverage includes standalone fallback behavior, mode intersection, malformed envelopes,
maximum-effort abstention, bounded recovery, verified/unverified acknowledgments,
redaction, current-history provenance, hook ordering, genuine human follow-ups, and
the exact refactor/tests-running handoff that previously triggered a false challenge.

Installation/configuration changes and plugin reloads are operator actions. Updating
these files alone does not establish activation in an already-running gateway.
