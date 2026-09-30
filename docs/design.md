# Full-agent outcome review

## Execution contract

Always run the authenticated user's request through the normal full agent. Do
not classify incoming messages to choose a shortcut, skip tools, choose a model,
or prevent an agent turn. The capture hook may retain context for review, but it
must not make an incoming classifier call or route the task.

After the agent has a draft outcome, give Jev the current genuine request,
draft, redacted tool evidence and trusted background/internal-turn metadata.
It may review both response quality and reported background action status.
The verifier then accepts, requests a bounded correction/recovery, escalates to
its generative judge, or replaces an unnecessarily long saved-note answer with
a short acknowledgment.

This is **outcome control**, not retrospective side-effect authorization.
Post-run review cannot undo a message already posted, a payment submitted, or
a destructive command. Existing approval and authorization boundaries stay in
place. Review never invents side effects or reports them done without evidence.

## Initial scenarios to develop together

- Normal answer: deliver an adequately supported, instruction-following result.
- Pure brain dump successfully saved: the full agent stores the complete note
  in the configured memory scope and verifies it; deliver only `Added.` (or a
  configured localized acknowledgment), not the model's commentary.
- Brain dump not saved: recover the actual storage problem; do not issue Added.
- Technical inability: distinguish model/tool failure from missing input. Use
  safe authorized tool alternatives or a bounded continued agent turn. A
  language-model switch is not a cure for a missing URL or failed authorization.
- Safety refusal: no alternate-model safety bypass. Preserve the refusal or
  explain an allowed alternative.
- Missing input: ask the necessary question, rather than repeatedly rerun.
- Pending background work: preserve a handoff, not an unsupported completed claim.
- Trusted completion notification: report genuinely new findings without
  duplicating an already-delivered conclusion.
- Unsupported completion: demand evidence or correct the draft.
- Uncertain/error: defer to the full verifier; do not pretend Jev proved success.

The registry is not exhaustive and the classifier is not an independent fact
oracle. Add scenarios incrementally with labeled examples and observed failures.

## Shadow mode

Shadow does not branch execution, start a second agent, or apply its suggested
handling. It runs an auxiliary Jev decision call against the completed outcome
and logs typed labels, confidence, usage and what would change. Existing agent
behavior and verification remain the baseline.

Validation has two layers:
1. Regression tests: schemas, abstention, bounded retries, no Added on missing
   storage evidence, no safety-refusal fallback, no duplicate internal answers,
   no action or provider mutation in shadow mode.
2. Labeled outcome evaluation: consented/anonymized English/Russian requests,
   actual draft/evidence and expected disposition. Inspect mismatches by scenario,
   especially false success acknowledgments, false recovery loops and lost
   questions. Measure errors, latency, API cost and abstention rather than treating
   a confidence value as a measured accuracy rate.

Use synthetic examples for public fixtures. Keep real conversation/evidence
records outside git. Source material and credentials must never be published.
Begin with live observation, correct scenario definitions together, and enable
active behavior one scenario at a time only after its acceptance tests pass.

## Architecture limits

A generative verifier can continue the existing full agent with actionable
feedback. That is not a full main-agent provider switch. Sticky Kimi-to-OpenRouter
execution still needs an adapter that switches client credentials, transport,
context metadata and tool-response normalization together. Do not implement it
by only changing a model string.

Jev does not generate prose, reason traces or explanations, and has no
reasoning-effort knob. Set effort separately for generative verification. Its
probabilities/confidence represent semantic judgments against supplied state,
not successful external writes or independent fact checking.
