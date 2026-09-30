# Validation results and release limits

## Execution tests

Parent verification ran both complete plugin suites against the compatible Hermes checkout at commit `5912ed81ed945a784f67ca13c1096e2c122cd307` using Python 3.14 and isolated test dependencies.

Result: **218 passed in 3.51s**.

Tests cover schema validation, no incoming Jev call, real isolated plugin discovery and scoped cooperative tool dispatch, shadow noninterference, mode intersections, bounded continuations, redaction, internal provenance, handoffs, transform ordering and memory-target identity.

Classifier/judge decisions and memory responses in these tests are explicitly synthetic. Passing tests do not establish live model accuracy, real memory persistence, prevention of external side effects, or sticky provider switching.

## Live Jev checks

Real OpenRouter Decisions calls were made against **14 public synthetic outcome states**. Execution evidence inside those states is fictional test input, not a claim that memory writes, tool runs or refusals were performed by a production agent.

Initial evaluation exposed an over-coupled confidence gate: uncertainty on an irrelevant memory question erased otherwise confident technical/refusal/handoff classifications. That gate was corrected without lowering acceptance or note-acknowledgment thresholds.

After the correction, the live evaluator returned:

- Total: **14**
- Expected scenario/disposition pairs matched: **5**
- Mismatched: **9**
- Valid actionable reviews: **4**
- Abstentions: **10**
- Failed requests: **0**
- Invalid response schemas: **0**
- Input tokens: **14,989**
- Output tokens: **2,523**
- Reported API cost: **$0.000629538**

The evaluator exited **1**, correctly reporting incomplete validation. The four actionable cases recognized technical failure, missing input, safety refusal and async handoff. Most other states deferred conservatively because relevant confidence did not reach the configured thresholds. No uncertain note was authorized as Added.

This tiny dataset is not a representative accuracy benchmark. It demonstrates that the live API and abstention path work and that active response handling is **not yet validated**. Do not market the offline synthetic replay's matching outcomes as live accuracy.

## Deployment posture

The installed plugins are suitable for **shadow observation**. The full agent runs normally; the existing generative verifier remains the baseline. Jev decisions are logged as metadata for scenario refinement, not applied to delivery or background actions.

Active acknowledgment/correction/recovery code has regression coverage but still needs per-scenario evaluation using actual supported evidence formats. Live note persistence/readback integration and main-provider switching have not been demonstrated by these tests. The plugins never own note storage or main-provider switching.

## Next acceptance checks

- Collect labeled, consented outcomes per scenario and per language.
- Distinguish intent/outcome classification accuracy from readiness/evidence confidence.
- Verify genuine memory-tool write/readback envelopes and matching document identity.
- Measure false Added acknowledgments, unnecessary corrections, refusal misrouting and handoff duplication.
- Observe timeouts, token cost and abstention rates on real turn sizes.
- Activate one narrowly validated response behavior at a time, with rollback available.
