# Hermes Plugins

A shared repository for custom [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins. Additional independent plugins can be added under `plugins/`.

## Plugins

- [response-critic](plugins/response-critic/) **1.5.0** — bounded pre-delivery verification, optional cooperative outcome review, internal-notification guards and verified-note acknowledgments. Standalone judge chain: Kimi → OpenRouter Muse Spark Contributor at maximum reasoning.
- [scenario-router](plugins/scenario-router/) **0.3.0** — outcome-only Jev reviewer, public cooperative review tool, shadow observation and labeled replay/live evaluation. Despite its historical name, it does not route incoming tasks or switch models.

## Execution contract

**The full agent always runs first.** No incoming classifier call, deterministic pre-run scenario router or fast-path skipped agent turn. Jev reviews the completed draft and supplied redacted execution evidence.

Shadow mode records its proposed handling without changing replies or actions. Active cooperative handling can accept, request bounded correction/recovery, or shorten a confirmed saved-note reply to `Added.`. The agent itself must store and verify the note; these plugins do not write memory or launch/cancel background jobs. Model/provider switching and session-sticky fallback are not implemented.

Post-run review cannot authorize or undo an already-executed external action. Existing tool approvals remain in place. Safety refusals must not become alternate-model bypasses.

See [design and validation](docs/design.md) and [current validation results](docs/validation.md).

## Installation

Copy either independent plugin directory into the **active profile's** `$HERMES_HOME/plugins/` (default `~/.hermes/plugins/`). Back up any existing version before replacing it. The repo root is a collection, not a single plugin manifest.

Runtime dependency: `httpx`. Keys stay outside git in the Hermes environment: `OPENROUTER_API_KEY` for Jev/OpenRouter and `KIMI_API_KEY` or `KIMI_CODING_API_KEY` for Kimi.

```bash
hermes plugins enable response-critic
hermes plugins enable scenario-router
hermes config set plugins.entries.scenario-router.settings.mode shadow
hermes config set plugins.entries.response-critic.settings.outcome_review_enabled true
hermes config set plugins.entries.response-critic.settings.outcome_review_mode shadow
hermes config set plugins.entries.response-critic.settings.max_iterations 1
hermes config set plugins.entries.response-critic.settings.review_budget_seconds 30
```

Use supported plugin reload/session startup. Hooks can reload immediately; new tool visibility may be deferred to a new session. Do not describe a config write alone as successful runtime activation.

This enables asynchronous Jev **observation**, while the existing generative verifier continues its bounded behavior. Automatic shadow observation no longer dispatches a second inline cooperative call. See [latency and compatibility review](docs/latency-review.md) for budgets, noninterference tests and limits. Do not enable active outcome handling simply because unit tests pass.

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
