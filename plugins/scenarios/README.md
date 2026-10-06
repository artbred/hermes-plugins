# Scenarios 1.0.0

A small advisory routing plugin for Hermes. It registers only `pre_llm_call`, returning trusted user-message context when a registry scenario matches. It does not execute tools, start children, restrict tools, maintain task state, judge completion, retry work, or rewrite final answers.

## Initial scenario

`scenarios.yaml` enables only OMP routing. OpenRouter Jev must confidently answer **yes** to both questions:

- Is this a direct request to perform coding, rather than research, discussion, quoted instructions, design talk, or ordinary operations?
- Would doing **and verifying** the requested work take an experienced engineer **more than five minutes**?

Uncertainty and internal/background notices do not qualify. Matching guidance tells the parent to use the existing **`ompx`** command in the background, never bare `omp`, and independently verify the result. This is advice, not an enforcement mechanism.

## Classification and registry

Eligible human turns make at most one request, carrying all registry questions together, to `https://openrouter.ai/api/v1/systemone` with model `jev-latest`. There is no fallback model or completion reviewer. Missing credentials, invalid configuration, abstention, malformed answers, transport errors, and timeouts produce no injected context.

The dependency-free YAML subset supports nested mappings, JSON-style double-quoted strings, plain scalars, comments, and `>-` folded strings. It rejects duplicate keys and unsupported YAML syntax. Quote choice names such as `"yes"` and `"no"` to avoid YAML boolean interpretation.

The only root keys are `version`, `untrusted_data_clause`, and `scenarios`. Each scenario declares:

- `threshold`: minimum answer confidence, in `(0, 1]`.
- `max_probability_must_win`: whether the selected choice must be a probability winner.
- `questions`: uniquely named `choice` questions, each with `instructions` and `criteria`; include an abstaining `no` or `none` choice.
- `on_match`: maps a choice to a block identifier or `null`. Every question in that scenario must confidently select the same mapped choice before its block fires.
- `blocks`: one-line guidance strings.

Add another scenario to this registry without changing Python. All expected answers must be well formed, with finite probabilities in `[0, 1]`, a valid probability sum, and valid choices. Confidence thresholds are scenario-specific; there is no extra global probability cutoff. Unknown keys, including enforcement configuration, are rejected.

## Privacy and limits

Only current human request text can leave for OpenRouter Jev. History is consulted locally only to identify native message metadata; system/developer messages, prior conversation text, tool results, media, and native memory/context appendages are not classifier input. Native `display_kind` metadata takes precedence over text heuristics for synthetic notices. Without metadata, known internal envelopes are excluded conservatively. Multimodal input contributes only supported text parts. Model-authored delegated child turns skip classification; native slash-skill boundaries expose only the human instruction, and auto-loaded skill scaffolds abstain.

Oversized input abstains rather than classifying a clipped prefix. Limits: 4,000 characters / 16,384 UTF-8 bytes before and after redaction; 256 KiB registry and request; 64 KiB response; three-second connection timeout and eight-second network waiting deadline. The plugin rejects redirects and compressed responses, disables environment proxies, and permits only one in-flight transport worker per imported classifier module; while a timed-out worker is still exiting, other turns abstain rather than accumulating workers.

Credentials are resolved each turn via `agent.secret_scope.get_secret("OPENROUTER_API_KEY")`, preserving the active Hermes profile. An available native API returning no key or raising never falls back to process credentials. Environment fallback exists only when the native secret module is absent, for standalone tests. There are no raw credential-file reads or plugin secret settings.

Before transport, native `agent.redact.redact_for_egress` force-redacts secrets even when ordinary display redaction is disabled. If the redactor is unavailable or fails, classification abstains; standalone tests supply a synthetic redactor. No local request/answer/evidence persistence or database exists. Logs contain only safe error categories or matched scenario names, never request text, answers, credentials, or turn/session identifiers. The normal Hermes transcript may retain injected guidance as part of its native context handling.

## Offline verification

Use an isolated environment with `pytest` and a compatible Hermes checkout/runtime dependency set. No classifier tests call a live provider; HTTP responses are synthetic. Native tests exercise the installed context collector and provider wire conversions. A standalone environment can run unit tests; native tests require Hermes and its dependencies.

```bash
HERMES_HOME="$(mktemp -d)" \
HERMES_DISABLE_LAZY_INSTALLS=1 \
PYTHONDONTWRITEBYTECODE=1 \
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
PYTHONPATH=/path/to/hermes-agent:/path/to/managed/runtime/site-packages \
python -m pytest -q -p no:cacheprovider plugins/scenarios/tests

ruff check plugins/scenarios
```

Use synthetic credentials only and remove the temporary profile afterward. Do not install test dependencies into the live managed runtime. This repository change does not install or activate the plugin; deployment and publishing are separate owner-controlled operations.
