# Hermes Plugins

A shared repository for custom [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins. Additional independent plugins can be added under `plugins/`.

## Plugins

- [response-critic](plugins/response-critic/) **1.7.0** — Jev-controlled optional pre-delivery verification, high/max reasoning, persistent provider failure circuits, internal-notification guards and verified-note acknowledgments. Judge chain: Kimi → OpenRouter Muse Spark Contributor at the requested effort.
- [scenario-router](plugins/scenario-router/) **0.5.0** — outcome-only Jev reviewer, independent judge-needed/effort decisions, public cooperative review tool, shadow observation and labeled replay/live evaluation. Despite its historical name, it does not route incoming tasks or switch models.

## Execution contract

**The full agent always runs first.** No incoming classifier call, deterministic pre-run scenario router or fast-path skipped agent turn. Jev reviews the completed draft and supplied redacted execution evidence.

Shadow mode records its proposed handling without changing replies or actions. Active cooperative handling can skip the generative judge only on an explicit high-confidence Jev skip decision, request bounded correction/recovery, or shorten a confirmed saved-note reply to `Added.`. When judging is required, Jev chooses only `high` or `max`; uncertain/unavailable policy requires `max`. Provider capabilities resolve maximum to `max`, otherwise `xhigh`, otherwise `high`. No review request uses medium/low/none. Both plugins persist failure cooldowns across reloads. The agent itself must store and verify the note; these plugins do not write memory or launch/cancel background jobs. Main-model thinking, audio delivery, model/provider switching and session-sticky fallback are not changed.

Post-run review cannot authorize or undo an already-executed external action. Existing tool approvals remain in place. Safety refusals must not become alternate-model bypasses.

See [design and validation](docs/design.md), [current active-review checks](docs/active-review.md), and the separate [future main-reasoning/request-pool design](docs/main-reasoning-pool.md).

## Installation

Copy either independent plugin directory into the **active profile's** `$HERMES_HOME/plugins/` (default `~/.hermes/plugins/`). Back up any existing version before replacing it. The repo root is a collection, not a single plugin manifest.

Runtime dependency: `httpx`. Keys stay outside git in the Hermes environment: `OPENROUTER_API_KEY` for Jev/OpenRouter and `KIMI_API_KEY` or `KIMI_CODING_API_KEY` for Kimi.

```bash
hermes plugins enable response-critic
hermes plugins enable scenario-router
hermes config set plugins.entries.scenario-router.settings.mode active
hermes config set plugins.entries.response-critic.settings.critic_mode active
hermes config set plugins.entries.response-critic.settings.outcome_review_enabled true
hermes config set plugins.entries.response-critic.settings.outcome_review_mode active
hermes config set plugins.entries.response-critic.settings.min_effort high
hermes config set plugins.entries.response-critic.settings.max_effort max
hermes config set plugins.entries.response-critic.settings.provider_cooldown_seconds 300
hermes config set plugins.entries.response-critic.settings.provider_max_cooldown_seconds 3600
hermes config set plugins.entries.response-critic.settings.provider_failure_threshold 2
hermes config set plugins.entries.response-critic.settings.max_iterations 1
hermes config set plugins.entries.response-critic.settings.review_budget_seconds 300
hermes config set plugins.hook_callback_timeout 330
```

Use supported plugin reload/session startup. Hooks can reload immediately; new tool visibility may be deferred to a new session. Do not describe a config write alone as successful runtime activation.

This explicitly opts into live outcome policy; shipped defaults remain shadow/standalone. For observation only, set both `mode`/`outcome_review_mode` to `shadow`. Active mode performs one inline Jev outcome decision and no duplicate automatic post-run observation. See [active optional-review validation](docs/active-review.md) for exercised behavior and limitations, and [latency and compatibility review](docs/latency-review.md) for budgets. Activating policy does not establish calibrated scenario accuracy.

## Validation

Tests require a compatible Hermes source checkout and an isolated Python environment with `pytest`, `httpx`, `pyyaml` and `python-dotenv`, plus the Hermes runtime dependencies needed by its plugin manager.

```bash
PYTHONPATH=/path/to/hermes-agent /path/to/test-env/bin/python \
  -m pytest plugins/scenario-router/tests plugins/response-critic/tests -o addopts='' -q

# Offline: explicit synthetic decisions exercise dispatch logic, not model accuracy.
python plugins/scenario-router/__init__.py \
  --evaluate plugins/scenario-router/examples/synthetic-outcomes.jsonl

# Live: billed OpenRouter calls against public synthetic example states.
python plugins/scenario-router/__init__.py \
  --evaluate plugins/scenario-router/examples/synthetic-outcomes.jsonl --live
```

The evaluator exits nonzero for mismatches: this is a validation result, not a reason to invent a passing report or lower safety thresholds. Confidence describes probability concentration, not established correctness. Calibrate scenarios on labeled, consented examples; keep real chat/evidence files outside git.

## Privacy and compatibility

Review sends redacted request/draft/evidence to external providers. Redaction is not full anonymization. OpenRouter's Muse Contributor tier permits provider use for model improvement.

The critic's non-file verification compatibility adapter uses a private in-memory Hermes attribute. It does not write core files, but it must be retested across Hermes upgrades. Tool/result format mismatches conservatively defer to full verification.

No API keys, private runtime configuration, authentication files, logs, conversation records, memory data, caches or historical backups are published.

## License

MIT.
