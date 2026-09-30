# Hermes Outcome Plugins

Two custom Hermes Agent plugins, developed together:

- **response-critic** — pre-delivery verification and bounded corrections, with Kimi and OpenRouter fallback.
- **scenario-router** — Jev-based review of completed agent outcomes, not pre-run task routing.

## Design

Every normal request goes through the full agent. Jev reviews the resulting draft and supplied evidence. Shadow mode records its decisions without changing replies or actions. Active outcome handling is enabled only after scenario validation.

For a saved brain dump, the intended response is a short acknowledgment after verified storage, not an analysis of the note. Technical recovery remains separate from safety-refusal handling.

See [the execution and validation contract](docs/design.md).

## Development status

This public repository is initialized with its design and safety contract. The updated plugin sources and tests are being prepared and will be added after validation. Their absence in this initial revision is intentional; this is not a completed release.

No API keys, private configuration, conversation records or memory data belong in this repository.

## License

MIT.
