# Active optional-review validation

## Execution

The full agent runs first. The active critic obtains one Jev outcome decision through the public scoped tool. Five typed questions independently describe outcome, readiness, memory evidence, judge-needed policy and judge effort. Jev does not have its own generative reasoning setting.

The full agent always runs first. Reviewer policy now has only `high` and `max`; `max` resolves to the highest supported API effort, preferring `max`, otherwise `xhigh`, otherwise `high`. No reviewer request may use medium/low/none. Main-session thinking is still separate and is not selected by this outcome-only advisor.

Only explicit, high-confidence skip proposals for supported ready outcomes bypass generative verification. A required-judge decision and effort remain usable even if the independent scenario/readiness dimension abstains. Invalid/unavailable policy defaults to required/max. An accept disposition alone never skips. Verified-note acknowledgments also require local current-turn matching storage evidence; background handoffs require actual pending runtime work. Refusals cannot authorize provider bypass.

## Failure circuits

The critic persists failure counts and cooldown metadata under a hashed provider/model/base identity in profile-owned plugin state. HTTP 401/403/429 opens the circuit immediately. Repeated technical/network/schema failures open it after a configurable threshold (default two). Cooldown starts at 300 seconds, doubles after failed probes, caps at 3600 seconds and honors bounded Retry-After. A valid verdict, including `passed:false`, resets failure counts. In-flight probe leasing and transactional file locks protect independent sessions/reloads; late workers cannot change expired caller decisions.

The Jev plugin persists its own transport/HTTP/schema backoff: default 30 seconds, doubling to 300 seconds. A valid low-confidence classification is not cached as a provider failure. No credential, prompt, raw provider body or note is written to circuit state.

## Exercised checks

- Combined Python 3.11.15 regression run: **565 passed**, including real Hermes PluginManager discovery/dispatch, forced-reload persistence, profile isolation, concurrent sessions and adapter request payloads.
- Six paired cross-plugin regressions use explicit synthetic typed decisions to verify that required high/max survives low outcome or readiness confidence through the actual envelope boundary and is not bypassed by a recovery disposition.
- Unmocked live public-input smoke used the real PluginManager and active pre_verify callback. Jev returned the new two-level schema; its skip-policy confidence was below 0.97, so conservative required/max behavior remained in place.
- Real OpenRouter fallback calls at both high and max returned valid passing verdicts for a public synthetic arithmetic check. Profile-state audit readback recorded actual requested/effective levels. This establishes transport support, not comprehensive factual correctness or scenario calibration.
- Capability tests cover max, xhigh-only, high-only, missing/unusable metadata and OpenRouter's documented explicit-null/all-gateway-levels metadata. A bounded 256-event private audit supports future review analysis without recording raw requests.

No production memory write, task creation or external side effect is part of these smoke inputs. Offline synthetic fixtures do not establish live model accuracy.

## Limits

Main-model thinking and audio delivery are unchanged. This policy is outcome-only, not incoming routing or an agent-skipping fast path. Review sends redacted text to external providers; redaction is not complete anonymization. Contributor-tier training terms still apply. If all generative reviewers are unavailable, existing bounded fail-open behavior delivers the original draft. One correction remains configured in the deployed profile; correction generation is outside the five-minute inference wait budget.
