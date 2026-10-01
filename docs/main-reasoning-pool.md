# Main-agent adaptive reasoning: separate future design

## Current boundary

The implemented advisor is outcome-only. It sees a completed full-agent run and controls review policy, not the reasoning already spent by that agent. Changing reviewer high/max levels does not make initial main-model execution faster. Jev itself is a typed decision model, not a generative model with a thinking-level knob.

A model/provider configured in Hermes can expose a main-session reasoning level; the installed profile presently configures high. This is not a learned per-request selector. Do not describe a configured reviewer policy or a historical example pool as live main-agent routing.

A separate passive `reasoning-shadow` plugin now implements pre-input recommendations and a private SQLite/FTS5 example pool. It does not apply recommendations or lower main reasoning; see [the shadow engine](shadow-engine.md) for its boundaries and checks. The request-family activation design below remains a later phase.

## Proposed request-family pool

If main-agent selection is later authorized, keep an incoming decision separate from outcome review, and retain normal agent execution and tool approvals. Retrieve a few similar *verified* examples plus relevant recent conversation, then ask for main-effort choice/confidence. Use low only for high-confidence familiar low-risk requests; novel, ambiguous, consequential or previously failing families must escalate. The full agent still executes the real action and verifies its effect. Previously successful storage does not authorize a new success acknowledgment.

Useful examples include routine task capture, personal-note capture, ongoing discussion and complex technical work. Mixed notes/questions must retain the question. A match by vocabulary is not proof of identical intent, authorization or risk. Include failures/corrections and user feedback, not just successes, and evaluate false low-effort routing as a separate failure class.

Store any content-bearing pool privately and profile-scoped, not in this public repository. Preserve complete original context/evidence and source handles in the existing private session store; consent/egress boundaries still apply before forwarding retrieved private examples to an external model. Metadata-only audit history cannot itself serve as a semantic request pool.

## Future analysis

Track requested policy and actual provider wire effort separately. Record timestamps, provider/model identity hashes, confidence, latency, outcome, fallback/cooldown use and corrections without storing raw provider bodies in audit logs. Correlate with private session records when a real scenario review is requested. Compare matched request families and unseen holdouts; keep token/cost and total task latency distinct from classifier/reviewer latency. A reviewer pass is not independent proof that an external action succeeded.

Before enabling automatic low-effort main execution, exercise the actual authenticated pre-agent hook/request boundary, supported provider levels, per-turn isolation, multimodal provenance, and explicit user overrides. Capture before/after performance against the same held-out requests. No autonomous self-training or request-family routing is implemented by this design document.
