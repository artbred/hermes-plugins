# Latency and compatibility review

## Timing diagnosis

The gateway's Working elapsed counter starts inside one `_run_agent` invocation. A background completion resumes through another gateway invocation with a new counter. It does not measure an entire multi-run job. The activity label reports the last recorded main-agent activity; it need not describe an auxiliary verifier's HTTP wait.

A measured delegated batch lasted 1,584.531 seconds. Nine verifier log intervals before this change ranged from 5.555 to 37.808 seconds (median 25.103 seconds), measured from a rejected primary call to the verdict. These are not whole-task durations. Background-agent/model work, rather than a three-minute counter, accounts for the much longer job.

## Changes

- `response-critic` 1.5.0: hard caller-wait deadline (default 30 seconds) shared with active outcome review; at most two abandoned inference workers; no late inference verdict may request a continuation. Capacity exhaustion/errors/timeouts fail open.
- The installed profile allows one critic correction rather than five. Correction generation is real agent work, not included in the inference-wait cap.
- Auth/quota HTTP 401/403/429 causes a 300-second judge cooldown. The OpenRouter fallback retains maximum reasoning.
- `critic_mode: off` and `shadow` no longer mutate the private verification sentinel or perform inline judge calls. Asynchronous outcome observation is provided by the separate scenario plugin.
- Critic outcome shadow mode no longer dispatches a duplicate inline Jev call.
- `scenario-router` 0.3.0: post-LLM shadow hook enqueues and returns. One worker, eight queued jobs maximum, 30-second stale-job threshold, unload cleanup and late-result discard. Profile-owned state is resolved before entering the worker.
- Child/subagent duplicate reviews are skipped; the full main agent still runs and can review its collected execution evidence.
- No edits to Hermes core, default model routing, tool approvals or the gateway heartbeat.

## Verified execution

Both complete suites: **232 passed in 3.61 seconds**. Synthetic regression decisions test deadline, saturation, quota cooldown, late-verdict noninterference, shadow/off gate nonmutation, unchanged full-verifier dispatch, child duplication, asynchronous shadow return, bounded queue and unload cleanup. Existing refusal, memory-ID, handoff, tool dispatch and profile-isolation coverage remains.

An isolated real OpenRouter Jev smoke test reviewed `Hi` / `Hello.` with no claimed tool or memory execution:

- Shadow delivery hook returned in **0.0009065 seconds**.
- Auxiliary API review completed in **0.532323 seconds**.
- Disposition: uncertain; applied: false.
- Reported API cost: $0.00004368.

This is a nonblocking API integration check, not proof of model accuracy. Active outcome transformations remain in shadow/not enabled. The earlier 14-case evaluation and its abstentions are documented separately.

## Limits

An active verifier intentionally differs from vanilla Hermes: it may request a bounded correction or apply existing duplicate-delivery protection. It cannot be both fully active and behaviorally identical. Off/shadow gate tests establish observer noninterference at the tested integration boundaries, not a guarantee for every future Hermes release.

A hard review deadline bounds the caller's wait, not an in-flight HTTP request's lifetime. Transport timeouts remain and two worker slots bound abandoned requests; late results are ignored for delivery. Original tool execution, main-model generation, subagent jobs and a permitted corrected generation have their own durations. The gateway counter remains per-run and is not a total-job stopwatch.
