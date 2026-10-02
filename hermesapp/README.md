# Hermes

Native iPhone chat client for [Hermes Agent](https://github.com/NousResearch/hermes-agent). This repository maintains the iOS app and an optional APNs reply-notification service; no Hermes plugin or native source patch is required.

The iOS app is named **Hermes** (`com.artbred.hermesapp`), distinct from the preserved legacy **Hermes Voice** (`com.artbred.hermesvoice`). Legacy chats and credentials remain separate; the Xcode target and scheme are still `HermesVoice`.

- Create chats, switch between previous conversations, and continue their native Hermes sessions. Different chats run independently; an approval or file import in one chat does not disable another.
- A minimal, gradient-backed chat screen with a rounded composer. Short conversations start beneath the header with whitespace below; the gradient follows the visible chat viewport and continues its end color behind the rounded system keyboard, keeping exposed keyboard corners free of black wedges. New Chat appears only at top right. The left menu contains chat search, recents and refresh, with Settings at the bottom. There is no floating Latest/jump-to-bottom button; scrolling manually back to the end resumes following.
- Add files with the composer's **+** button. Preview or remove them before sending; Hermes receives actual uploaded files and can inspect them with its native tools. Attachment drafts survive relaunch; text drafts are retained when switching chats within the app.
- Every submitted text message starts a full Hermes agent run, with the server's configured model, tools, approvals, and memory integration.
- Voice messages use Hermes STT, then Jev automatically routes the transcript: requests/questions become full agent turns with spoken replies; high-confidence thoughts/diary entries are saved to the Hindsight `voice` bank without an agent run, assistant reply, or TTS. Messages with attached files always go to the agent rather than silent brain-dump storage.
- Your recorded voice messages show a static **Audio message** indicator with selectable transcription underneath, without original-recording playback, seek, or share controls. The recording is retained for transcription/retry; AI reply speech and **Listen** remain available.
- Text-message replies stay silent. Tap **Listen** on any reply to generate and play its audio; generated audio is cached on the phone.
- Agent replies render as inline mobile HTML interfaces: cards, tables, task lists, collapsible details, and local interactive controls. Source links are underlined and open in an in-app browser. Older Markdown replies remain readable; speech, copying, and search use human text rather than markup.
- **New Voice Chat** in Shortcuts/Siri/Action Button opens the app, creates a new chat, and records. Invoke it again while recording to send that take without creating another chat. The in-app microphone records into the current chat.
- Recording has one **Send** button; tapping elsewhere inside the app immediately discards the recording and consumes that tap.
- Conversation titles are generated asynchronously by the auxiliary model, not copied from the opening sentence. Message headers/timestamps are omitted; an assistant-side activity indicator shows the current processing step.

```mermaid
flowchart LR
    A[iPhone chat] -->|text| B[Native Hermes /v1/runs]
    A -->|recording| C[Native Hermes /api/audio/transcribe]
    C --> J[Jev intent classification]
    J -->|request or uncertain| B
    J -->|confident brain dump| M[Hindsight voice bank: no reply]
    B --> D[Full agent run: tools, memory, response]
    D --> A
    A -->|voice turn or Listen| E[Native Hermes /api/audio/speak]
    E -->|audio| A
```

## Setup

Build/install instructions and app behavior: [`ios/README.md`](ios/README.md).
Native HTTP contract and deployment boundaries: [`docs/API.md`](docs/API.md).

Open Settings from the left menu; it uses native push/back navigation, not a bottom sheet. Enter **one server URL and one API token**. Both are stored in Keychain. **Test connection** checks chat, voice, files, routing, titles, and memory storage without invoking a model or writing data.

The deployment uses `https://hermes.sashakuzina.com` and `API_SERVER_KEY` from `~/.hermes/.env` on `kuzin`. Every app request uses that Bearer token. Dashboard and provider credentials remain on the server behind authenticated, narrowly scoped proxy routes.

### Shared desktop, phone, and terminal sessions

`kuzin`, default profile, owns the canonical `/root/.hermes/state.db`. The desktop's existing SSH tunnel (`http://127.0.0.1:19119` → `kuzin:127.0.0.1:9119`) and the phone's public HTTPS URL reach that same store. They intentionally use different credentials: the desktop's dashboard token stays on the Mac; the phone keeps its scoped native API token in Keychain. Do not point the desktop at the phone-only public endpoint or expose desktop administration routes under the mobile token.

The phone imports `api_server`, `desktop`, and `cli` sessions and periodically refreshes history while active, as well as supporting manual Refresh. It continues the native session ID, follows compression aliases, and includes pre-compression display history. Existing local audio, attachments, drafts, failed attempts, and brain dumps are retained. Empty, message-less chats stay out of Recents/search. A topic title is generated from the first submitted user text or transcription in parallel with the agent, shown locally immediately, and published once native session identity arrives; failed publications retry after reopening. Shared session titles and subsequent desktop renames come from the server.

Desktop groups phone-origin sessions under its **API** sidebar section, which may be collapsed; desktop/CLI-origin sessions remain under **Sessions**. This grouping is provenance, not separate storage.

For occasional terminal use, install the explicit launcher without replacing local `hermes`:

```sh
chmod +x hermesapp/scripts/hermes-shared
mkdir -p ~/.local/bin
ln -s "$PWD/hermesapp/scripts/hermes-shared" ~/.local/bin/hermes-shared
hermes-shared
hermes-shared --resume <native-session-id>
hermes-shared sessions list
```

The launcher requires the existing SSH alias `kuzin`. It runs native Hermes with `HERMES_HOME=/root/.hermes` and `--profile default`, quotes arguments for the remote shell, and allocates a terminal only for interactive stdin. Tools and working directories are on the VPS, not the Mac. Ordinary Mac-local `hermes` remains available but uses a separate store; it is not replicated. Do not override the shared launcher's profile if you want the same sessions as desktop and phone.

On this Mac, interactive zsh maps `hermes` to the shared launcher with `alias hermes='hermes-shared'` in `~/.zshrc`. New shells load it automatically; run `source ~/.zshrc` in an existing shell. The unaliased Mac-local executable remains available explicitly as `~/.local/bin/hermes`.

Only server-owned conversation history and titles are shared. Recordings, downloaded speech, unsent drafts, attachments' local previews, and silent diary-only chat entries remain device-local; their saved memory still lives in Hindsight. Removing a chat on the phone hides its whole native compression lineage on that phone; it does not delete the server conversation or other clients' copies.


## Hermes configuration

Enable Hermes's native `api_server` platform and set `API_SERVER_KEY`, `API_SERVER_HOST`, and `API_SERVER_PORT` in the Hermes environment. Keep TLS/authentication in front of it. Enable the native dashboard backend for its speech routes, but **do not expose its administrative API** to the phone's public hostname.

The phone uses the configured `stt` and `tts` providers. For the installed local-command STT path, explicitly set:

```yaml
stt:
  enabled: true
  provider: local_command
  local:
    language: auto
```

An empty language setting on this native path can fall back to English. Current deployment uses Fish through the existing Hermes STT/TTS command-provider configuration.

## Delivery and persistence

Chats, messages, recordings, and synthesized replies live in Application Support under `Chats/`. A message and its request identity are saved before network submission. Native run IDs are persisted; relaunching the app resumes status retrieval rather than creating a second run. Server conversation context stays in Hermes.

The native voice flow is multiple requests, not an atomic audio-and-run endpoint. iOS grants only limited background execution; if it suspends the app before transcription/run submission finishes, reopen the app to resume. An accepted run continues on Hermes without the phone. Failed messages show an error and a Retry control. Retrying a terminal run is an explicit new attempt and can repeat actions already performed by the agent. Native idempotency is retention-bounded (24 hours on the configured server), not a permanent exactly-once guarantee.

While processing, Hermes shows its activity alongside the streamed reply. The composer's square Stop control requests interruption and shows **Stopping…** until the server confirms the result; it also handles a stop requested during submission. Stop intent survives relaunch. Completed actions are not rolled back.

Pending approvals show **Review request** instead of automatically blocking navigation. Close or swipe away the review to keep using another chat; that does not approve, deny, or stop the pending request. The chat list shows which conversations are in progress or need approval. One outstanding turn per local chat is intentional; native requests targeting the same conversation also serialize. The VPS admission limit is 10 concurrent API runs, so server capacity or provider limits can still delay/reject additional work.

Reply notifications use server-side APNs delivery, not phone polling. With notification permission, a push-enabled signing profile, and the notification service configured, a completed reply alerts the phone while Hermes is not open. Foreground replies are acknowledged and do not show banners; tapping an alert opens its owning chat. Alerts contain no reply text. The current production deployment is **not enabled** until Apple signing and APNs credentials are supplied; see [`docs/API.md`](docs/API.md#reply-notifications).

## Migration from the diary app

The former `voice-notes` plugin, `/v1/notes` protocol, and dedicated diary tooling have been removed from this repository. Ordinary messages use native Hermes conversations. Automatically detected brain dumps retain the original transcript in the existing Hindsight `voice` bank through its native API, without a custom plugin.

Existing on-device `Outbox/` files and server diary/Hindsight data are not deleted or automatically replayed as agent instructions. Native credentials use separate Keychain entries; set up the new connection once after upgrading. Deleting a chat in the app removes its local copy/audio and hides it from subsequent history imports; it does not delete the Hermes session.

## Development

```sh
cd ios
xcodegen generate
xcodebuild -scheme HermesVoice -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test
```

The Swift Testing suite covers native protocol boundaries, speech-mode behavior, durable accepted-run recovery, and storage failure handling. See the iOS guide for device installation and verification details.

## License

MIT
