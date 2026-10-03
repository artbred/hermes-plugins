# Hermes Plugins

A monorepo for custom [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugins and the Hermes Voice iOS app. Additional independent plugins can be added under `plugins/`.

## Active plugins

- [proofgate](plugins/proofgate/) **0.4.0** — proof before "done": change requests (remove, install/set up, update, schedule) open a per-turn checklist; tool calls are logged as evidence; checks close automatically after a read-only verification and the model is nudged until then. Local rules only — no model calls, nothing leaves the machine. Formerly `reminder`.
- [fish-speech](plugins/fish-speech/) **1.0.0** — Fish Audio speech: TTS (`s2.1-pro`) with per-reply language voice selection (local Cyrillic check first, Jev tiebreak; Russian replies use the Russian reference voice) plus Fish ASR transcription. Registers `tts.provider: fish` and `stt.provider: fish`.

## Speech provider plugin

[`fish-language-tts`](plugins/fish-language-tts/) **1.0.0** is a real Hermes backend plugin. It registers provider **`fish-language`** through `PluginContext.register_tts_provider()`; it does not register agent hooks or delegate to a command provider. Jev selects Russian versus the configured general voice, and synthesis always uses paid Fish `s2.1-pro`. Credentials are resolved per call from Hermes's profile-scoped secret API.

The plugin contains the canonical `hermes_fish_speech` package. The mobile speech service installs that same package instead of maintaining another implementation.

**Live installation is awaiting owner authorization.** The real installer, native speech tool, scoped credential behavior, and unload were exercised only in temporary profiles; active Mac/kuzin settings and services were not changed.

After explicit authorization, install through Hermes:

```bash
hermes plugins install artbred/hermes-plugins/plugins/fish-language-tts --enable --yes-deps
hermes config set tts.provider fish-language
hermes config set tts.voice 933563129e564b19a115bedd57b7406a
hermes config set tts.model s2.1-pro
hermes config set tts.output_format mp3
```

Use the already configured profile's `FISH_API_KEY`; `OPENROUTER_API_KEY` enables optional Jev routing. Missing Jev uses the general voice, while missing Fish credentials make the provider unavailable. Never place credentials in plugin settings or Git. `ffmpeg` is required for replies spanning multiple chunks. See [`hermesapp/docs/API.md`](hermesapp/docs/API.md#real-provider-plugin-packaging-and-deferred-cutover) for the mobile package and migration order.

## App

[`hermesapp/`](hermesapp/) contains the Hermes Voice iOS client, its optional APNs notification service, and app-specific documentation. It is a regular directory tracked by this repository, not a submodule.

Build/install instructions: [`hermesapp/ios/README.md`](hermesapp/ios/README.md). Run the app's development commands from `hermesapp/`.

## Superset workspaces

[Superset lifecycle configuration](.superset/config.json) requires Python 3.10+
with `venv` and `pip`. Setup creates a workspace-local `.venv` and installs
`httpx`, `pytest`, `pyyaml`, and `python-dotenv`.
Use `source .venv/bin/activate` or `.venv/bin/python` for local commands.

There is no dev server or required database/container, so `run` is omitted and
`teardown` is empty. Setup does not copy credentials, enable plugins, or change
the Hermes profile. Full integration tests still require the compatible Hermes
source checkout described below.

## Validation

Tests require a compatible Hermes source checkout and an isolated Python environment with `pytest`, `httpx`, `pyyaml` and `python-dotenv`, plus the Hermes runtime dependencies needed by its plugin manager.

The current speech plugin/service suite uses real Hermes registration and native speech dispatch in temporary profiles with synthetic HTTP transports. `ffmpeg`/`ffprobe` and a compatible Hermes checkout are required:

```bash
(
  cd hermesapp/speech
  PYTHONPATH=/path/to/hermes-agent uv run --locked \
    --with pyyaml --with ruamel.yaml --with rich --with packaging --with python-dotenv \
    pytest -q . ../../plugins/fish-language-tts/tests
  uv run --locked ruff check . ../../plugins/fish-language-tts
)
```

The `proofgate` and `fish-speech` tests need no Hermes checkout:

```bash
python3 plugins/proofgate/tests/test_proofgate.py
(cd plugins/fish-speech && python -m pytest -q tests)   # needs httpx
```

## Privacy

`proofgate` keeps its ledger local (0600), redacts secrets from recorded tool arguments, and sends nothing off the machine. `fish-speech` sends reply text to Fish Audio and, for undecided language, to OpenRouter (Jev). No API keys, private runtime configuration, authentication files, logs, conversation records, memory data, caches or historical backups are published.

## License

MIT.
