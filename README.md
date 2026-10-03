# Hermes Plugins

A monorepo for custom [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins and the Hermes Voice iOS app. Additional independent plugins can be added under `plugins/`.

## Active plugins

- [reminder](plugins/reminder/) **0.1.0** — gatekeeper ledger: verb-derived checklists, per-task tool evidence, open-check injection while runs are fixable. Backed by the Graphiti `infra` system map.
- [fish-speech](plugins/fish-speech/) **1.0.0** — Fish Audio speech: TTS (`s2.1-pro`) with per-reply Jev language voice selection (Russian replies use the Russian reference voice) plus Fish ASR transcription. Registers `tts.provider: fish` and `stt.provider: fish`. Replaces the old `tts.providers.fish` command entry.

## Archived plugins — retired

- [response-critic](plugins/response-critic/) **1.7.0** — Jev-controlled optional pre-delivery verification, high/max reasoning, persistent provider failure circuits, internal-notification guards and verified-note acknowledgments. Judge chain: Kimi → OpenRouter Muse Spark Contributor at the requested effort.
- [scenario-router](plugins/scenario-router/) **0.5.0** — outcome-only Jev reviewer, independent judge-needed/effort decisions, public cooperative review tool, shadow observation and labeled replay/live evaluation. Despite its historical name, it does not route incoming tasks or switch models.
- [reasoning-shadow](plugins/reasoning-shadow/) **0.1.0** — independent passive main-effort predictor, private causal SQLite/FTS5 request pool and offline metadata report/import/annotation CLI. Defaults off; enabling shadow never changes main reasoning. See [shadow engine](docs/shadow-engine.md).

**Retired on 2026-10-02 at the owner's request.** All three plugins are explicitly disabled on this Mac and on `kuzin`. The gateway and remote desktop backend were restarted to unload existing registrations; a live gateway inspection returned no active registrations for these plugins. Sources, installed server copies, and private example/history stores are preserved. OpenRouter credentials and provider configuration are unchanged. Do not enable these archived plugins for normal operation; app-specific Jev classification is independent of them.

## App

[`hermesapp/`](hermesapp/) contains the Hermes Voice iOS client, its optional APNs notification service, and app-specific documentation. It is a regular directory tracked by this repository, not a submodule.

Build/install instructions: [`hermesapp/ios/README.md`](hermesapp/ios/README.md). Run the app's development commands from `hermesapp/`.

## Archived execution contract

**The full agent always runs first.** No incoming classifier call, deterministic pre-run scenario router or fast-path skipped agent turn. Jev reviews the completed draft and supplied redacted execution evidence.

Shadow mode records its proposed handling without changing replies or actions. Active cooperative handling can skip the generative judge only on an explicit high-confidence Jev skip decision, request bounded correction/recovery, or shorten a confirmed saved-note reply to `Added.`. When judging is required, Jev chooses only `high` or `max`; uncertain/unavailable policy requires `max`. Provider capabilities resolve maximum to `max`, otherwise `xhigh`, otherwise `high`. No review request uses medium/low/none. Both plugins persist failure cooldowns across reloads. The agent itself must store and verify the note; these plugins do not write memory or launch/cancel background jobs. Main-model thinking, audio delivery, model/provider switching and session-sticky fallback are not changed.

Post-run review cannot authorize or undo an already-executed external action. Existing tool approvals remain in place. Safety refusals must not become alternate-model bypasses.

See [design and validation](docs/design.md), [current active-review checks](docs/active-review.md), and the separate [future main-reasoning/request-pool design](docs/main-reasoning-pool.md).

## Historical installation — do not activate

Copy either independent plugin directory into the **active profile's** `$HERMES_HOME/plugins/` (default `~/.hermes/plugins/`). Back up any existing version before replacing it. The repo root is a collection, not a single plugin manifest.

The commands below document the retired implementation, not recommended deployment. Keep `reasoning-shadow`, `response-critic`, and `scenario-router` in `plugins.disabled`; copying or retaining their files does not enable them.

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

## Superset workspaces

[Superset lifecycle configuration](.superset/config.json) requires Python 3.10+
with `venv` and `pip`. Setup creates a workspace-local `.venv` and installs
`httpx` from the plugin requirements plus `pytest`, `pyyaml`, and `python-dotenv`.
Use `source .venv/bin/activate` or `.venv/bin/python` for local commands.

There is no dev server or required database/container, so `run` is omitted and
`teardown` is empty. Setup does not copy credentials, enable plugins, or change
the Hermes profile. Full integration tests still require the compatible Hermes
source checkout described below.

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
