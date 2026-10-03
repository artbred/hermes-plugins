# fish-speech

Fish Audio speech for Hermes: TTS with per-reply language voice selection,
plus Fish ASR transcription. Registers providers named `fish`:

- **TTS** (`FishSpeechTTS`): Fish Audio `s2.1-pro` synthesis. Before rendering,
  a bounded Jev classifier detects the predominant prose language; strong
  Russian replies use the Russian reference voice, everything else uses the
  configured general voice. Long texts are chunked and re-assembled with
  ffmpeg (never byte-concatenated).
- **Transcription** (`FishTranscription`): Fish Audio ASR (`/v1/asr`).

Also ships `speech_server.py`, the narrow authenticated mobile speech
endpoint (`POST /api/audio/speak`, `POST /api/audio/voice`, default
`127.0.0.1:9124`), and `fish_stt_cli.py`, the original ASR CLI.

## Install

```bash
hermes plugins install <git-url>#fish-speech --enable
```

(or from this monorepo checkout: `hermes plugins install ./plugins/fish-speech`)

## Configure

The plugin reads credentials from the environment — no key files needed:

```bash
FISH_API_KEY=...            # required
OPENROUTER_API_KEY=...      # optional; without it, voice selection abstains to the general voice
```

In `config.yaml`, point Hermes at the plugin providers. This REPLACES the
old `tts.providers.fish` command entry — remove that block, command entries
win over plugins:

```yaml
tts:
  provider: fish
  fish:
    voice: 933563129e564b19a115bedd57b7406a
stt:
  provider: fish
```

## Tests

```bash
python -m pytest tests/ -q
```

Pure-function tests only (no network, no credentials).
