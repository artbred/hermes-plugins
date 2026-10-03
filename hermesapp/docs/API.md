# Native Hermes API used by the iOS app

The app uses native Hermes agent/speech APIs plus three exact authenticated proxy routes to existing OpenRouter and Hindsight APIs. Reply notifications additionally use the companion APNs service in `notifications/`; it does not patch Hermes or replace native run execution. Provider credentials remain on the host.

## Authentication and ingress

- Every app request uses `Authorization: Bearer <API_SERVER_KEY>` at `https://hermes.sashakuzina.com`.
- The native agent validates that key. Transcription and file routes use native forwardAuth before server-side dashboard credential injection. Mobile synthesis is handled by the narrowly scoped Fish speech service, which validates the same native Bearer key directly and keeps Fish credentials server-side. Dashboard tokens alone cannot authenticate on the public hostname.
- The app has no separate voice/file URL or token. Redirects are refused to avoid forwarding credentials to another origin.
- HTTPS is required outside trusted local networks; the iOS app permits local-network HTTP under its ATS configuration.

Only expose the routes needed by the client. Dashboard/provider credentials are server-only: the proxy must not make `/api/config`, `/api/env`, `/api/audio/voice-config`, the dashboard UI, or other administrative routes reachable through the app's hostname. Native desktop authentication remains unchanged.

## Connection test

1. `GET /v1/capabilities`: require the native Hermes capability object, run submission/status/events/stop/approval, sessions, and durable run idempotency.
2. `POST /api/audio/transcribe` with `{}`: require `422` with a native missing-field error for `body.data_url`.
3. `POST /api/audio/speak` with `{}`: require `422` with a native missing-field error for `body.text`.
4. `POST /api/files/upload-stream` with `{}`: require native `422` missing `body.file` and `body.path`.
5. `POST /api/voice/retain` with `{}`: require native `422` missing `body.items`.
6. `POST /api/voice/classify` and `/api/voice/title` with `{}`: validate the native structured missing-input errors (`400`), not arbitrary proxy failures.

Empty objects fail request validation before any speech inference. A GET probe is insufficient: the current dashboard's SPA fallback answers `404`, not `405`, for these POST-only routes.

## Chat model inventory and selection

`GET /api/model/options` uses the same native Bearer authentication as runs and returns the configured picker inventory, not the virtual gateway aliases from `/v1/models`. Example:

```json
{"model":"muse-spark-1.3","provider":"muse-code","providers":[{"slug":"muse-code","authenticated":true,"models":["muse-spark-1.3"],"unavailable_models":[]},{"slug":"openai-codex","authenticated":true,"models":["gpt-6.1-sol"],"unavailable_models":[]}]}
```

The top-level concrete model/provider identifies Hermes's configured default. It is automatically selected for new chats and stays first in the picker. Provider `slug` and exact model ID form the request identity; display labels are presentation-only. Only authenticated, usable inventories are offered: virtual/unconfigured/native-empty/entitlement-pending rows and unavailable/reserved model IDs are excluded. Exact provider identity takes precedence; only a unique server-supplied alias may resolve to another concrete slug.

Other suggestions come from authenticated `GET /api/sessions?source=<source>&limit=200&include_children=false` metadata for `api_server`, `desktop`, `cli`, `telegram`, and `oneshot`, without importing their transcripts. Models are ranked by distinct session/compression-lineage count, then most recent activity and stable identity. Hidden, archived, cron, internal and duplicate rows do not contribute. Session lists omit providers, so only models matching one eligible provider are suggested; the configured default's exact identity resolves its own otherwise-ambiguous ID. At most five choices are shown, default included; fewer real choices stay fewer rather than being filled with featured/random catalog entries. Metadata/authentication/transport errors remain visible with Retry rather than partial or fabricated suggestions.

Manual selections persist on their chat but do not become the next chat's default. Previously saved app-global preferences are no longer read or written. Empty chats adopt the loaded configured default unless manually selected during this app lifetime; nonempty chats and queued/frozen submissions are not rewritten by an inventory refresh.

`models/hermes-models.yml` is the narrowly scoped Traefik artifact: current-host GET only, exact `/api/model/options`, forwarded to the native API listener so native authentication still runs. The owner explicitly authorized this route for the phone installation; it is live as `/etc/dokploy/traefik/dynamic/hermes-models.yml` (mode0600). Authenticated GET returned 200 with 151 eligible native model entries; missing auth returned401, POST returned404, and private `/api/config` stayed404. Native configuration remained byte-identical and no gateway/dashboard restart or plugin activation was performed. Protected rollback marker/source is `/root/.hermes/backups/phone-model-route-20261003T015628Z/`. An unavailable route still shows a model-loading error and Retry rather than a fabricated catalog or global-default fallback.

## Full agent turns

### `POST /v1/runs`

```http
Authorization: Bearer <API_SERVER_KEY>
Content-Type: application/json
X-Hermes-Session-Key: ios-chat:<local-chat-id>
Idempotency-Key: <local-message-id>-<attempt>
```

```json
{"input":"What should we do next?","session_id":"previous-native-session-id","instructions":"Return a self-contained mobile HTML fragment, not Markdown.","provider":"openrouter","model":"google/gemini-3.7-flash"}
```

Omit `session_id` for the first turn. The stable session key associates subsequent requests with the same conversation. Once known, the explicit native session ID is sent; Hermes follows its compression continuation and loads server-owned history. The app does not resend the complete transcript.

`instructions` is the native per-run system-prompt extension, not part of the user's text. The abbreviated example above illustrates the field; new mobile requests send the complete [`MobileResponseFormat.instructions`](../ios/HermesVoice/Rendering/MobileResponseFormat.swift) contract. Other native clients and the server's global prompt are unchanged. The app freezes `instructions` alongside `input` and `session_id` before admission; uncertain requests saved by older builds continue to omit it, preserving their original idempotency fingerprint.

Every new prepared run includes the concrete selected `provider` and `model`. The choice is captured on the queued user message before asynchronous upload/transcription work and persisted again in `RunSubmission`; changing another chat or refreshing the configured default cannot change an in-flight request. The native run handler translates those fields to `requested_provider`/`requested_model` without changing global Hermes configuration. Existing native session/model locks may reject incompatible choices; errors remain visible instead of silently substituting a default. Only previously frozen legacy requests without model fields continue omitting them during lost-admission recovery, preserving their original idempotency fingerprint.

Thinking is also captured on the queued text/voice message and frozen in `RunSubmission`. Explicit levels use the native per-run envelope `{"model_options":{"reasoning":{"enabled":true,"effort":"high"}}}`; permitted picker efforts are `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. Off sends `{"model_options":{"reasoning":{"enabled":false}}}` without an effort. Automatic and legacy nil snapshots omit `model_options` entirely, preserving previous admission fingerprints and resolving Hermes's configured reasoning for the actual model. The native run adapter applies the request override before provider-specific effort mapping; unsupported levels are not promised as distinct wire settings, and some models cannot disable thinking. Changing the current chat setting never rewrites an already queued or frozen submission. An explicit terminal/new-attempt retry captures the current chat setting; uncertain admission recovery keeps the original. The APNs relay forwards this body unchanged and includes it in its existing idempotency hash.

Response: `202 {"run_id":"run_...","status":"started","replayed":false}`. The admission receipt need not contain a session ID; obtain it from status.

Submission identifiers are saved locally before sending. A retry after a lost response uses the same message/attempt key. A retry of a known terminal failed/interrupted run increments the attempt. Reusing a key with a different body produces `409`; do not silently generate a new key in response. Native key retention is advertised by capabilities and is 24 hours on the current deployment.

### Status, events, and controls

| Method | Route | Purpose |
|---|---|---|
| GET | `/v1/runs/{run_id}` | Poll status, final `output`, `session_id`, error, pending approval |
| GET | `/v1/runs/{run_id}/events` | SSE: `message.delta`, `message.interim`, `approval.request`, terminal events |
| POST | `/v1/runs/{run_id}/stop` | Request interruption of the native run |
| POST | `/v1/runs/{run_id}/approval` | Submit `{"choice":"once","request_id":"..."}`; use choices and request ID supplied by Hermes |

The client uses SSE for provisional text and status polling as the authoritative recovery path. Leaving/suspending the app does not stop an accepted run. Polling resumes when the app returns. Approval request IDs are not SSE sequence IDs.

`POST .../stop` may return `{"run_id":"...","status":"stopping"}`; this is an acknowledgement, not terminal confirmation. The client persists stop intent and acknowledgement separately and continues polling until a terminal status. A stop during uncertain admission reuses the frozen submission and idempotency key to recover the same run ID. An acknowledged stop is not reposted on relaunch. Transport/authentication/proxy errors remain pending and visible rather than declaring the run stopped; a completed response racing Stop remains completed.

Only HTTP `404` with native `error.code == "run_not_found"` closes an unavailable saved run locally. The app preserves its run ID, marks the local message failed, and releases polling/stream/stop state for that chat. It does not claim a remote terminal outcome or replay automatically. Explicit Retry creates a new attempt and warns that prior actions may have occurred. A generic `404`, an unrelated error code, or `401` is not evidence that a run disappeared.

SSE `tool.started` and `tool.completed` events carry `tool`; the app shows the current tool name without displaying raw arguments or reasoning. Interrupted/cancelled partial output is retained when returned by status, but does not trigger automatic speech.

Completed output becomes an assistant message. Failed, cancelled, or interrupted runs remain visible with their error. Tools may have performed actions before a run stopped; retrying is not a rollback.

Optional closed-app reply delivery uses the [APNs notification bridge](#reply-notifications). Native execution and status remain authoritative. A permitted iOS request includes its registered device and local chat IDs; the bridge durably associates them with the admission receipt before returning it. The production-hosted bridge is active for development-signed `com.artbred.hermesapp` installations; the current provider key is sandbox-only.

### Mobile response interfaces

Mobile runs request self-contained HTML fragments with semantic headings, lists, cards, tables, source links, and optional inline CSS/JavaScript for local controls. A checklist, filter, tab, or table-view toggle changes only that reply's presentation; it cannot mutate real tasks, submit a chat, approve a tool, or access native APIs. Native Hermes tools and explicit approval controls remain the action boundary.

The app stores the raw answer and renders it inline in a non-persistent `WKWebView`. A DOM sanitizer removes redirects, remote resources, frames, credential inputs, and unsafe destinations. A shell-first CSP denies network connections, frames, workers, media, and form submission; the isolated web store also uses a closed loopback proxy without failover. Page scripts cannot access the renderer's separate-world native bridge. Streaming snapshots are static; local scripts run only after completion. An explicit formatting reload resets local control state after a web-process failure.

Source links are visibly underlined. A real tap on an HTTPS link opens an in-app Safari preview without the app's authorization headers. HTTP/contact links instead show a notice with a copy action; script/file/data/app destinations cannot navigate. Long replies can be expanded, and wide tables/code remain scrollable. Legacy Markdown is converted through a GFM parser into the same surface; fenced HTML examples stay inert code.

Speech, reply copying, chat previews/search, title generation, and assistant context for intent classification use semantic plain text extracted without executing scripts. CSS, scripts, controls, and decorative content are excluded; headings, list items, table cells, entities, and code text remain readable.


### Conversation isolation and capacity

Each local chat has its own `X-Hermes-Session-Key`, worker, stream, stop state, and pending approvals. An explicit `session_id` takes precedence over the header: two different keys pointing at the same native conversation are not independent chats. Hermes intentionally serializes turns within a durable conversation/compression lineage; distinct conversations can run concurrently.

The importer revalidates session ownership after awaiting history and recognizes a fresh session whose ID already matches an accepted local run. The client still obtains authoritative continuation IDs from run status; a replayed or recovered run must not assume `session_id == run_id`. Closing approval review changes UI presentation only; approval submission remains bound to the captured chat/run/request, not whichever chat is selected later.

The shared owner is `kuzin`'s default profile and `/root/.hermes/state.db`; the Mac desktop's loopback URL is an SSH tunnel to that server, not a Mac-local database. Desktop dashboard authentication, native mobile Bearer authentication, and the explicit SSH terminal launcher remain separate secure transports to the same store. No session-sharing header or database replication is required.

The configured VPS `gateway.api_server.max_concurrent_runs` is **10**. This is an admission ceiling shared with other native API turn endpoints, not guaranteed simultaneous inference capacity. New requests at that ceiling receive `429` with `Retry-After: 1`; exact idempotency replays recover before this capacity check. Approval waits and cooperative stopping occupy slots until their workers finish. Native execution also uses a finite thread pool, and provider limits may delay work below the admission ceiling.

Live verification used distinct A/B keys: B completed while A remained running; stopping A cancelled only A; B's continuation retained its context. The app-only isolation fixes require no new VPS service, plugin, or gateway restart.

## Native speech

### `POST /api/audio/transcribe`

```json
{"data_url":"data:audio/mp4;base64,<recording>","mime_type":"audio/mp4"}
```

Response: `{"ok":true,"transcript":"...","provider":"local_command"}`.

The app records AAC, 16 kHz, mono in `.m4a`. Raw recordings must be nonempty and at most 25 MiB. An empty transcript is treated as no detected speech and does not start an empty agent run. Hermes uses its configured STT provider; language/model selection is server-side.

### `POST /api/audio/voice`

```json
{"text":"Это ответ на русском языке.","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro"}
```

Response: `{"provider":"fish","reference_id":"c35aeeb5f9c145199fbffdbc2ef8ed95","model":"s2.1-pro","language":"russian"}`.

This authenticated, classifier-only endpoint makes one bounded Jev `jev-latest` request through OpenRouter's TypeSafe `/api/v1/systemone` API; it does not synthesize audio or change the main answer model. It classifies the predominant human-readable language of the complete final reply, treating its text as data and ignoring markup, code, URLs, and embedded classification instructions. Valid Russian choices require confidence ≥0.90 and probability ≥0.95 and select the public Russian **Рената Литвинова** voice (`c35aeeb5f9c145199fbffdbc2ef8ed95`). English uses the supplied general voice, defaulting to Sarah. Other/mixed/uncertain language and malformed, unavailable, or failed classification use that same general voice. `language` is `english`, `russian`, `other`, or `unknown`; uncertainty uses `other`, and unavailable/invalid judgments use `unknown`.

The classifier has a 15-second total deadline, no retries, no synthesis on this route, and no dependency on the retired review/shadow plugins. Input/voice/paid-model validation and Bearer authentication precede classification. Provider errors and credentials are not returned. Missing optional Jev credentials leave explicit synthesis and readiness available.

### `POST /api/audio/speak`

```json
{"text":"Hermes's reply","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro"}
```

Response: `{"ok":true,"data_url":"data:audio/mpeg;base64,...","mime_type":"audio/mpeg","provider":"fish","reference_id":"933563129e564b19a115bedd57b7406a","model":"s2.1-pro"}`.

Fish **2.1 Pro** is explicitly selected by the upstream `model: s2.1-pro` header; the free model is not used. Sarah (`933563129e564b19a115bedd57b7406a`) is the default. New mobile clients send the chosen reference and paid model on every synthesis request and require matching acknowledgment, so an old text-only endpoint cannot silently ignore voice selection. Older text-only clients receive the same default voice/model. Other explicit models, malformed references, and unknown fields are rejected before contacting Fish. Missing text retains the native `422` readiness shape.

The app sends semantic plain text, not HTML/CSS/JavaScript. Voice-origin turns synthesize automatically; text replies synthesize only on Listen; brain dumps remain silent. Build 30 resolves the effective voice through `/api/audio/voice` only before uncached synthesis, then sends that exact voice to `/api/audio/speak`. Cached assistant audio persists its actual voice/model separately from the general Settings preference; valid Russian caches pause/resume and survive relaunch without another classifier or synthesis request. A changed general preference, edited reply, or legacy cache without the new preference marker invalidates reuse; original user recordings are untouched. A changed preference stops playback and invalidates old in-flight results, including change-away-and-back races. Pending automatic speech follows the latest preference; stale on-demand speech waits for a fresh Listen. Audio metadata is committed before retiring replaced files.

`plugins/fish-language-tts/hermes_fish_speech/` is the single speech/language implementation. The real Hermes provider imports it through its plugin package, and `hermesapp/speech/` is the authenticated mobile adapter with an explicit package dependency. The old command adapter is removed from repository code, not from the still-running host deployment. Long text is split into ordered, at-most-4000-character chunks without truncation; real ffmpeg assembles multiple MP3 chunks. Provider failures remain visible, without retries or a free-model fallback. Input remains bounded to 64000 characters/256 KiB and audio to 32 MiB.

### Real provider-plugin packaging and deferred cutover

Plugin directory: `plugins/fish-language-tts`; manifest name: `fish-language-tts`; native provider name: `fish-language`; standalone distribution: `hermes-fish-speech`. The supported Hermes `TTSProvider` interface executes synthesis in-process through `register_tts_provider`, with no agent hooks, core patches, or command subprocess. Native credentials use `agent.secret_scope.get_secret()` per call; profile isolation, key rotation, and unload follow the Hermes registry lifecycle. The mobile process still uses its existing named credentials and HTTP authentication. Its protocol and iOS callers do not change.

**No active-host installation or service migration is authorized yet.** `native-fish.yml` is the future declarative configuration, not proof of activation. Merge its `plugins.enabled` entry into existing configuration rather than replacing other enabled plugins. Use a supported Hermes release with the TTS-provider registrar. The new provider name deliberately differs from legacy command provider `fish`, because command entries win over same-named plugin registrations.

Repository development resolves the shared dependency from `../../plugins/fish-language-tts` through `uv.lock`. For a separately deployed mobile service, build a wheel and export only its third-party runtime requirements:

```bash
uv build --wheel --out-dir dist plugins/fish-language-tts
uv export --project hermesapp/speech --locked --no-dev --no-emit-project \
  --no-emit-package hermes-fish-speech --output-file dist/speech-requirements.txt
```

After explicit deployment approval: retain a protected rollback; install the real native plugin and select `tts.provider: fish-language` with the existing general voice, paid model, and MP3 output; provision the service's existing three private credentials; install the exported requirements with hash verification and the built wheel into the speech service's virtual environment; then replace `speech_server.py` and restart only the services needing the cutover. Do not deploy the repository-relative `uv` source path into `/opt` and expect it to resolve. Once both native plugin dispatch and authenticated mobile synthesis pass, remove the obsolete command configuration and old duplicated `fish_tts.py`/`language_voice.py` files. Keep the credential files because the mobile service still uses them. Existing ingress and mobile cache contracts are unchanged.

To disable the provider later, first select an intentional alternate TTS configuration, then disable `fish-language-tts`; do not leave an unknown `tts.provider` name configured, because Hermes core can fall back to its default engine when no provider is registered.


## Conversation history

- `GET /api/sessions?source=<api_server|desktop|cli>&limit=200&offset=0`: fetch each interactive source independently, paginate using `has_more`, then deduplicate native IDs. Internal worker and messaging-platform sessions are not imported.
- `GET /api/sessions/{session_id}/messages?limit=500&offset=0&order=oldest&include_compacted=true`: persisted display history across the compression lineage; only nonempty visible user/assistant text is shown.
- `PATCH /api/sessions/{session_id}` with `{"title":"..."}`: publish a phone-generated title through native Bearer authentication. The response must acknowledge the addressed session and a nonempty persisted title. The public proxy admits PATCH only for the exact session-detail path; it does not expose session creation/deletion, message mutations, or dashboard administration.

Title publication first requests `GET /api/sessions/{session_id}/messages?limit=0&order=oldest` to obtain the resolved `session_id` without downloading message rows. It then PATCHes that resolved ID. Titles can be published while the native run is still active.

Foreground refresh updates known conversations as well as importing new ones. Native message IDs are retained separately from local message IDs so server history can merge without replacing recordings, attachment manifests, retry identities, or cached speech for unchanged replies. A changed assistant row invalidates its old speech cache. Refresh defers chats with pending local work and rechecks ownership/transcript state after network awaits.

Assistant `finish_reason` values `verify_hook_continue` and `verification_required` identify provisional verification candidates, not delivered replies. The importer omits their text but retains their IDs separately in `RemoteHistory.supersededMessageIDs`, across every raw-row page. Reconciliation removes only cached assistant records bound to those native IDs (or their legacy imported-ID form); the final run reply and its audio/notification identity remain intact. Ordinary commentary, partial delivered output, and identical answers from distinct turns are not collapsed. Server records are left unchanged.

The list's `_lineage_root_id`, message rows' `session_id`, and history response's resolved `session_id` keep one local chat across compression. History pagination restarts if its tip alias changes between pages. Subsequent runs address the resolved native ID rather than replaying the transcript. Removing a local chat hides its current native ID and known lineage root; server data is not deleted.

Local chat identity is a UUID independent of the server's opaque native ID; the notification protocol addresses that local UUID. Loading older imports migrates non-UUID local IDs and binds their existing remote rows. The persisted `RunSubmission.sessionKey` freezes the header as well as the body, preserving idempotency fingerprints for uncertain requests across that migration. Legacy frozen non-UUID attempts recover under their old header without attaching a new push destination; subsequent turns use the notification-safe local UUID.

## Auxiliary routing, titles, and brain-dump storage

All three routes require the existing agent Bearer token. `/etc/dokploy/traefik/dynamic/hermes-voice.yml` (mode0600) authenticates it with native `/v1/capabilities` using Traefik forwardAuth **before** injecting the upstream credential. Only exact POST paths are exposed. No provider key is sent to the phone.

- `POST /api/voice/classify` proxies OpenRouter `/api/v1/systemone`, model `jev-latest`. The typed Choice question `intent` evaluates the transcript plus up to six preceding conversational messages. Options are `chat`, `brain_dump`, and `unsure`. Only `brain_dump` with confidence≥0.90 and probability≥0.95 is silently stored; every other valid result becomes chat. Invalid probabilities/schema/HTTP failures stop with a visible error. See [OpenRouter's TypeSafe contract](https://openrouter.ai/docs/guides/community/typesafe-sdk) and [confidence semantics](https://docs.typesafe.ai/confidence).
- `POST /api/voice/title` proxies OpenRouter chat completions with `google/gemini-3.7-flash`, temperature0.2, reasoning effort `minimal`, and `max_tokens:256`. It requests only a broad 3–6-word topic name in the user's language from the first meaningful submitted user text/transcription or attachment names, without responding or acting. The request runs independently of agent admission/completion. A truncated/empty title is rejected. The generated title and pending-publication state are saved locally before any session PATCH, including when native identity is still unknown; once known, the title is published without requiring run completion. Reopening retries pending publication without repeating title inference. Confirmed diary-only entries keep local titles.
- `POST /api/voice/retain` proxies only Hindsight `/v1/default/banks/voice/memories`. It sends the original transcript, recording timestamp, tags `brain_dump`/`hermes_voice`, document ID `voice-<messageUUID>`, `async:true`, and `operation_id:<messageUUID>`. The app validates durable acceptance for that same operation before showing Saved. Retried saves keep their classification and operation identity, never become agent turns. Hindsight extraction/indexing completes asynchronously.

The host's Hindsight credential has broader permissions, but this ingress cannot select another bank or endpoint. Missing/invalid app tokens return401 before any provider request; arbitrary paths/methods are not routed. Test connection still checks agent and speech availability without paid inference; model/storage failures are shown on the affected message.


## Native file uploads

`POST /api/files/upload-stream` uses the same configured base URL and Bearer token as chat. The authenticated proxy routes it to a separate scoped instance of the stock Hermes file service and supplies the dashboard credential server-side. Multipart fields:

- `path`: `uploads/ios/<attachment-UUID>[.<safe-extension>]`
- `overwrite`: `true` (safe retries write the same locally persisted bytes to the same UUID path)
- `file`: the binary content; original display names stay in the agent manifest, not in the multipart storage filename.

The native service limit is 100 MiB per file; ingress allows another 64 KiB for multipart framing. The client builds its body off the main actor in 64 KiB chunks and sends it from a temporary file. The server publishes atomically after the complete upload. The acknowledgement must contain `ok:true`, matching absolute `path` and `entry.path`, matching byte count, and matching `root`/`locked_root` with `can_change_path:false`. Wrong or incomplete receipts fail before agent submission.

The app adds an attachment manifest (name, MIME type, absolute server path) to the native run input. Binary data is not inlined into the prompt. Files upload before `/v1/runs`; each receipt is persisted. Once prepared, the run input and session ID are frozen for retries after a lost admission response. Accepted-run recovery skips reupload and resubmission.

### Scoped upload deployment

The existing desktop file browser has unrestricted local path access, so its upload endpoint is **not** exposed. Instead:

- `/etc/systemd/system/hermes-attachments.service` runs stock `hermes serve --host 127.0.0.1 --port 9121 --isolated --skip-build`.
- `HERMES_HOME=/var/lib/hermes-attachment-relay` and the separate gateway lock directory isolate configuration, state, and process ledgers from the normal gateway. No desktop flag is set, so it does not start desktop cron/reapers.
- `HERMES_DASHBOARD_FILES_ROOT=/root/.hermes/attachments` locks all writes inside that directory. Parent traversal and absolute paths outside it are rejected.
- `HERMES_DISABLE_LAZY_INSTALLS=1` prevents runtime provisioning or rewriting the shared launcher. `installs`/`tools` use the existing native stores; only generation lease metadata is writable there. The main home/config/session data is not copied.
- The service filesystem is otherwise read-only and its networking is loopback-only. `hermes-attachments-bridge.socket`/`.service` forward `172.23.0.1:9122` to `127.0.0.1:9121`, restricted to the current Traefik source `172.23.0.6`.
- `/etc/dokploy/traefik/dynamic/hermes-attachments.yml` exposes only the exact authenticated POST upload route, with a 104923136-byte request-body limit. General file browsing, reading, deletion, and dashboard administration are not exposed.

Update the bridge's systemd IP allowlist and UFW rule if Traefik's bridge address changes. This is native service/proxy configuration, not a custom backend or plugin.

## Configured deployment on `kuzin`

Public base: `https://hermes.sashakuzina.com`.

- Native API binds `172.23.0.1:8642`; gateway configuration enables `platforms.api_server`.
- Existing native dashboard remains on loopback `127.0.0.1:9119`.
- `hermes-speech-bridge.socket` / `.service` use standard `systemd-socket-proxyd` to forward `172.23.0.1:9120` to the dashboard. No custom server code is involved.
- Traefik configuration: `/etc/dokploy/traefik/dynamic/hermes-native.yml`. The current public hostname admits authenticated GET session resources and PATCH session details alongside the listed agent routes and exact speech paths. The old hostname remains GET-only for sessions; iOS migrates it to the current hostname. Desktop administrative routes remain reachable only through the private tunnel, not this public ingress.
- Bridge/system firewall rules restrict access to the current Traefik bridge address, `172.23.0.6`. If that container address changes, update both the systemd allowlist and UFW rules before restarting the bridge.
- App secret: `~/.hermes/.env` → `API_SERVER_KEY`. Server-only dashboard secret: `~/.hermes/dashboard-remote.env` → `HERMES_DASHBOARD_SESSION_TOKEN`. Keep internal credential injection after native Bearer authentication; rotating dashboard credentials requires restarting their native services and updating the protected proxy configuration.
- `stt.local.language: auto` avoids the native local-command path's English fallback. Fish command providers remain configured in Hermes, not in this app.
- Paid mobile speech runs as `hermes-speech.service` on `127.0.0.1:9124`, with `DynamicUser`, a read-only system/home sandbox, private temporary files, and three named systemd credentials (`api-key`, `fish-key`, `jev-key`) from root-only `/etc/hermes-speech/`. `hermes-mobile-speech-bridge.socket`/`.service` exposes `172.23.0.1:9124` only to the current Traefik source and loopback; the corresponding UFW rule is source/interface scoped.
- `/etc/dokploy/traefik/dynamic/hermes-speech.yml` (mode0600, priority310) overrides only current-host POST `/api/audio/speak` and `/api/audio/voice`. Transcription, native chat, and administrative route boundaries are unchanged.
- The still-installed native deployment retains Sarah/paid `s2.1-pro` through the previous `tts.providers.fish` command and root-private Fish/Jev key files. That deployed code is intentionally unchanged while the real-plugin installation awaits authorization. Repository code instead uses the new `fish-language` plugin and the shared wheel described above. Existing native caps/timing and protected rollbacks (`/root/.hermes/backups/language-voice-20261002T214256Z/` and `/root/.hermes/backups/fish-speech-20261002T203437Z/`) remain intact. Gateway/dashboard were restarted during the earlier, separately authorized retirement—not during this plugin-only repository migration.

Never place secret values in source control, screenshots, command output, or documentation.

## Reply notifications

All notification routes use the same agent Bearer token. They must route to the companion service, not the dashboard:

| Method | Route | Body / purpose |
|---|---|---|
| POST | `/api/push/devices` | `{"device_id":"<installation-UUID>","token":"<APNs-hex-token>","environment":"development"}`; register or update Apple's token |
| POST | `/api/push/runs/{run_id}/ack` | `{"device_id":"<installation-UUID>"}`; mark a persisted foreground reply delivered |
| POST | `/v1/runs` | Existing native body and identity headers; additionally `X-Hermes-Push-Device` and `X-Hermes-Push-Chat` for reply delivery |

Push registration/acknowledgement must return `200 {"ok":true}`. The session header must equal `ios-chat:<X-Hermes-Push-Chat>`. `development` uses sandbox APNs, `production` uses production APNs; iOS reads the signed provisioning environment rather than assuming Debug/Release signing. App Store installations without an embedded profile use production.

The bridge stores notification intent before admission and the actual native receipt before answering `202`. Lost admission receipts recover with the original frozen body, session key, and idempotency key, never a new attempt. Unknown admission recovery stops after 23 hours, below the configured native 24-hour retention. Durably accepted receipts replay locally; changed input under the same identity returns `409`, including after reply delivery.

After native status confirms a nonempty completed output, the bridge durably schedules an alert with a two-second foreground-acknowledgement grace period. Background persistence is not an acknowledgement. APNs payloads contain only a generic alert, `kind:hermes_reply`, `chat_id`, and `run_id`; response text, reasoning, recordings, and credentials are excluded. Chat IDs group alerts; a stable per-run collapse ID coalesces retried sends. Accepted APNs deliveries and acknowledgements survive service restarts. An ambiguous APNs transport outcome cannot provide a strict exactly-once guarantee.

Pending deliveries survive temporary network/provider errors; invalid device registrations are removed without deleting a concurrently replaced token. Pending accepted runs expire after 30 days; finished delivery records retain seven days, inactive device registrations 90 days. SQLite and its WAL live in a private state directory. Logs exclude authorization headers and device-token URLs. APNs credentials never reach iOS.

### Deployment prerequisites and cutover

1. Enable Push Notifications for the explicit app identifier `com.artbred.hermesapp` and obtain a matching iOS App Development profile. Use automatic signing with an authenticated Xcode account, or manual signing with the downloaded profile and a compatible local Apple Development identity. The cached wildcard profile cannot sign this feature.
2. Obtain an APNs-enabled `.p8` provider key for team `8RK5YR2SLU`; retain its Key ID and verify its topic/environment restrictions. Install the key, root-readable only, as `/etc/hermes-push/AuthKey.p8`. Do not add keys to Git. A sandbox-only key cannot deliver to production-signed or App Store installations.
3. Install `server.py`, `pyproject.toml`, and `uv.lock` from `notifications/` into `/opt/hermes-push`. Use `UV_PYTHON_INSTALL_DIR=/opt/hermes-push/python UV_LINK_MODE=copy /root/.hermes/bin/uv python install --no-bin 3.12`, then the same environment with `uv sync --locked --no-dev --python 3.12 --managed-python --compile-bytecode`. The managed interpreter and copied, precompiled dependencies must be readable/executable by the service's dynamic user; secrets must not be placed under that directory or depend on inaccessible `/root` paths.
4. Create root-only `/etc/hermes-push.env` containing the existing `API_SERVER_KEY`, `APNS_KEY_ID`, `APNS_TEAM_ID=8RK5YR2SLU`, and `APNS_TOPIC=com.artbred.hermesapp`. The service uses systemd credentials for the private key, a dynamic user, and private persistent state. The configured Key ID is `7F5N86YMF4`, restricted by Apple to this topic and the sandbox environment.
5. Install `notifications/hermes-push.service` in `/etc/systemd/system/`, reload systemd, and start the service. Its Docker ordering waits for the bridge address before binding `172.23.0.1`. It must start successfully before ingress cutover; a missing or invalid private key fails startup.
6. Restrict Docker-bridge port `172.23.0.1:9123` to the current Traefik source `172.23.0.6`. Install `notifications/hermes-push.yml` in `/etc/dokploy/traefik/dynamic/` only after the service is healthy. Its priority 300 intercepts exact POST admission/notification routes; existing status/events/stop/approval and sessions keep using native Hermes. Requests without push headers remain native admissions.
7. Install the newly signed iOS build, allow notifications, and confirm Settings says **Reply notifications enabled**. Submit a real request, leave the app before completion, observe the alert on the physical phone, tap it to verify its chat, then repeat with the app active to verify no duplicate banner.

`hermes-push.service` is active and enabled on `kuzin`, with root-only credentials and mode0600 SQLite state. The notification ingress is active only for the three exact POST routes above. A Traefik-namespace probe reached the authenticated listener; an unrelated container timed out. Public authenticated probes preserved native agent, speech, file, and auxiliary validation routes and rejected private configuration access. An actual HTTP/2 sandbox request returned `400 BadDeviceToken` for a deliberately invalid token, not a real device delivery.

Build 15 is development-signed for `8RK5YR2SLU.com.artbred.hermesapp`, passed strict signature verification, and is installed on the connected iPhone 18 Pro. The server access token was imported into the app's Keychain without printing it; temporary transfer copies were removed, and provider signing keys never reached the phone. Physical Settings verified the real connection and **Reply notifications enabled** with no notification action controls. Real foreground requests produced no banner; a real background alert opened its owning reply chat from another selected chat without crashing. Notification response/presentation completion handlers execute on the main queue, including ignored payloads, instead of the async delegate completion that crashed UIKit state restoration. Live ordinary and push-aware native admissions, durable acknowledgment, and narrowly scoped ingress were also exercised.
