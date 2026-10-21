# Hermes host ingress (prepared, not activated)

Minimal adapter for Dokploy/Traefik versions that cannot persist `ExtraHosts`.
Reuses the installed official **nginx 1.29.5** image; no custom proxy application,
image build, paid dependency, credentials, or Docker socket mount.

```
Traefik -> hermes-host-ingress:<port> (Docker DNS, managed isolated network)
        -> /sockets/<name>.sock (read-only bind of persistent directory)
        -> systemd-socket-proxyd -> localhost4:<native-port>
```

| Container port | Socket instance | Native target |
|---|---|---|
| 8642 | api | localhost4:8642 |
| 9120 | speech | localhost4:9119 |
| 9122 | uploads | localhost4:9121 |
| 9123 | push | localhost4:9123 |

There are **no published host ports**, numeric-IP upstream targets, extra hosts,
public listeners, or new authentication policy. Existing Traefik routing,
limits, and upstream authentication remain responsible for access control.
Use Dokploy's native **isolatedDeployment=true** mode. Source Compose deliberately
leaves networks unset: the installed native renderer adds the application's
`appName` network exactly once, and Dokploy creates it and reconnects Traefik.
Only the adapter and Traefik should join that network. Before first deployment,
register a bridge network named exactly `appName` through Dokploy's network API
with `internal=true` and `attachable=true`; then verify the native renderer and
runtime membership. The isolated-app flag owns attachment/reconnection, while
the registered network owns its network policy. An internal bridge does not
replace Traefik's existing external default gateway. Do not rely on the native
helper's non-internal auto-creation default, or deploy after deleting this
registered network without reprovisioning its policy. `expose` is metadata,
not a firewall:
do not attach this adapter to the global shared ingress network. Keep all
upstream authentication; native loopback applications may expose a bootstrap UI.

Artbred uses `compose.broker.yaml` and `nginx-omp.conf` for the same transport
with Docker DNS `omp-host-ingress`, port 8765, and the `broker.sock` instance.
Its optional `systemd/omp-broker-loopback.conf` drop-in changes only the broker
bind to the verified IPv4 loopback name; it does not change vault credentials.
The broker profile also reaches the existing Infisical backend by service DNS
on its declared legacy network and relays port 8080. This lets Traefik retain
only controller-owned isolated attachments rather than a manually attached
`vaultwarden_private` network. The profile uses Docker's embedded DNS resolver
only as resolver infrastructure, supports backend IP changes and WebSocket
upgrades, and does not publish either port. Apply the versioned
`traefik/artbred-system-services.yaml` only after both private probes pass;
its live copy is `/etc/dokploy/traefik/dynamic/legacy-gateway.yml`.

The broker router is tailnet-only through the `tailnet-only` `ipAllowList`
(tailnet ranges plus host loopback/Docker bridges); public requests get 403.
Tailnet clients reach Traefik's `0.0.0.0:443` publish directly through
`tailscale0`, so never add `tailscale serve --tcp 443`: it takes
`100.95.234.17:443` first and Traefik then fails to bind 443 after a Docker
restart.

Dokploy 0.29 lacks the network-registry API: bootstrap the same labelled,
internal/attachable Docker bridge once from the recorded app name before
native deployment, and verify its policy. Dokploy 0.30 can register it through
`network.create`. Do not replace the internal bridge with a default bridge.
For nested Git Compose paths, save an explicit native Compose command with
`--env-file` pointing to the generated `.env` beside the Compose file; this
avoids version-dependent project-directory environment lookup.

## Readiness-gated local completion

`cutover_local.py --check` is read-only and returns 75 while the authenticated
API is not exclusively on loopback. The one-time systemd path/service watches
the existing default-profile PID metadata and waits for a supported owner
restart; it never signals or restarts the messaging service itself and uses no
LLM polling. Each readiness attempt is bounded. After readiness it installs the
staged push source/config with a hash guard, verifies named HTTPS API/audio/file
and validation-only run paths, rejects missing keys, retires obsolete TCP
relays/firewall grants, writes a private completion receipt and disables its
watcher. An unexpected push source edit fails closed instead of overwriting it.

Install the script and its staged files below `/opt/hermes-host-ingress`, keep
`push-stage.json` operator-generated and private, and arm the path only after
all rollback copies and new native socket paths are verified. Set the gateway's
host to `localhost` in its environment and the provided user-unit drop-in;
apply that listener change only with the supported owner command. Do not call
this migration complete until `/var/lib/hermes-host-ingress/cutover.json` exists
and live acceptance has been checked. Record failures from the one-shot unit's
journal; no notification, paid inference or phone call is used as a test.

## Scope and hard gates

- **API cutover is blocked until a user-supported `/restart`.** The running
  native API binds a Docker-side address, not loopback. This adapter intentionally
  will NOT reach that old address. Do not change the API bind in the running
  gateway, signal/restart it, add a temporary numeric-IP alias, or claim the
  API is migrated because this source passes tests.
- Push must separately be rebound to loopback and restarted in an approved
  change. Speech/dashboard and uploads/attachments are IPv4-loopback services.
- Do not modify LiveKit, SIP, voice-agent services, auth, or credentials.
- Existing TCP speech/uploads socket units are not modified by these artifacts.
  Leave them available for the parent's explicit rollback/cutover decision.
- Deployment and router mutations are owned by the operator/parent, not tests.

## Installation artifacts (review before use)

All source paths below are relative to this directory. Nothing here installs
itself. Use **one** managed Compose deployment (e.g. a Dokploy Compose project)
with `nginx.conf` present alongside `compose.yaml` in its working directory.
Do not paste the Compose YAML into a manager that cannot materialize that file.
Set `HOST_INGRESS_CONFIG_DIR` to the absolute managed checkout directory
containing these configuration files, normally
`/etc/dokploy/compose/<appName>/code/deploy/host-ingress`. This avoids relying on
version-specific Compose `--project-directory` behavior for bind mount sources.
The adapter must run on the host that owns the sockets. This is Docker Compose,
not a multi-node `docker stack deploy` recipe. Do not recreate or modify `dokploy-network`; the native isolated-deployment
network is separate. Keep `isolatedDeploymentsVolume=false`, and inspect the
rendered Compose before deploy to confirm the explicit stable DNS alias survives
Dokploy's service-name collision prevention and no shared network is attached.

| Source | Install destination | Owner/mode |
|---|---|---|
| `systemd/hermes-host-ingress@.socket` | `/etc/systemd/system/hermes-host-ingress@.socket` | root:root 0644 |
| `systemd/hermes-host-ingress@.service` | `/etc/systemd/system/hermes-host-ingress@.service` | root:root 0644 |
| `backends/{api,speech,uploads,push}.conf` | `/etc/hermes-host-ingress/<instance>.conf` | root:root 0644 |
| `tmpfiles/hermes-host-ingress.conf` | `/etc/tmpfiles.d/hermes-host-ingress.conf` | root:root 0644 |
| `compose.yaml`, `nginx.conf` | managed Compose checkout/working directory | root-owned, not group/world writable |
| `hosts/localhost4.patch` | reviewed patch for `/etc/hosts` AND `/etc/cloud/templates/hosts.debian.tmpl` | preserve existing root ownership/modes |

Create `/etc/hermes-host-ingress` root:root 0755 before copying backend files.
After installing the reviewed tmpfiles fragment, create its directories with
`systemd-tmpfiles --create /etc/tmpfiles.d/hermes-host-ingress.conf`.
Require `getent group nogroup` to report GID **65534**, matching the Compose
user `65534:65534`. If it differs, stop and review the group mapping; do not
make sockets world-writable.

Persistent host permissions are intentional:

- `/var/lib/hermes-host-ingress`: root:root **0700**. This ancestor prevents
  unrelated host processes in shared `nogroup` from traversing to the sockets.
- `.../sockets`: root:nogroup **0750**, mounted directly at container `/sockets`.
- Each systemd-owned socket: root:nogroup **0660**, not writable by everyone.
- Parent directory persists across boots. Do not substitute `/run`, delete or
  recreate the directory while mounted, or bind-mount individual socket files.
  Missing/recreated socket files produce recoverable **502** responses rather
  than stopping container creation. A missing parent directory is an install
  error (`create_host_path: false`), not something Docker should silently fix.

### Durable localhost4 alias

`systemd-socket-proxyd` resolves a hostname once and uses the first result;
plain `localhost` can select IPv6 before these IPv4-only services. The patch
adds a separate **IPv4-only localhost4 alias**, not a Docker/Tailscale address.
Back up and review BOTH target files. For a staged filesystem root `$STAGE`:

```
patch --dry-run --batch --forward --fuzz=0 -l -p1 -d "$STAGE" -i "$PWD/hosts/localhost4.patch"
patch           --batch --forward --fuzz=0 -l -p1 -d "$STAGE" -i "$PWD/hosts/localhost4.patch"
```

`--fuzz=0` prevents ignoring the context lines; `-l` accommodates tabs
in `/etc/hosts`. The cloud-init template preserves the
alias when `manage_etc_hosts` regenerates the hosts file. Never run cloud-init
or replace the whole hosts file for this change. A pre-existing alias or a
patch conflict needs manual review. Before activation, verify `localhost4`
resolves only to IPv4 loopback (including `socket.getaddrinfo`), each native
listener is loopback-only, and the root/socket permissions match above.

### Later activation/rollback (not performed here)

After the hard gates and backups, verify the installed units with
`systemd-analyze verify`, reload systemd, then enable/start only the reviewed
instances `hermes-host-ingress@<api|speech|uploads|push>.socket`. The matching
service is socket activated; do not enable it separately. A new service
invocation reads its backend config. Deploy Compose through the chosen manager,
then change existing Traefik service URLs to `http://hermes-host-ingress:<port>`
without changing middleware/auth/body limits. Verify authorized/unauthorized
requests, SSE, and uploads through the real router before retiring old bridges.
If a gate is unmet, leave that route uncut; do not hide the old address in DNS.

Rollback is explicit: restore backed-up router configuration, stop/remove only
the adapter deployment, and disable/stop only its template instances if desired.
Keep native backend state and the existing bridges unchanged unless separately
approved. Do not remove a live socket directory. No gateway restart is part of
rollback.

## Verification

From this directory (standard-library tests; `patch` and Docker Compose needed):

```
python3 -m unittest discover -s tests -v
HERMES_INGRESS_INTEGRATION=1 python3 -m unittest discover -s tests -v
systemd-analyze verify systemd/hermes-host-ingress@.socket systemd/hermes-host-ingress@.service
```

The opt-in suite needs root, `nsenter`, `unshare`/`mount`, Docker, and the existing
`systemd-socket-activate`/`systemd-socket-proxyd` binaries. It refuses implicit
image pulls (`--pull never`); ensure the exact digest in Compose is cached.
Fixtures use a random private Docker network, no host-published ports, ephemeral
host **loopback-only** HTTP servers, and real proxyd connections through four
Unix sockets. Each proxyd gets an isolated mount-namespace hosts file; global
hosts/cloud-init and live units are untouched. Temporary paths use `$TMPDIR`
(or the Hermes scratch directory), and fixture processes/containers/networks
are removed on completion. No production DNS/network attachment is tested.

Tests cover all four paths, original Host/auth/forwarded headers and raw URI,
upstream 401, response Location preservation, early SSE delivery despite an
upstream buffering override, early Content-Length AND chunked upload forwarding
above 1 MiB, missing/recreated sockets, group permissions, actual dropped
capabilities/nonroot/read-only execution, Compose security constraints, no
numeric upstreams or published ports, Docker DNS, and both alias-patch targets.

**Remaining verification boundary:** `systemd-analyze verify` validates the
unit syntax; disposable proxyd fixtures do not execute the actual systemd
manager's DynamicUser/IP sandbox or boot ordering. Validate installed units,
real backend bindings/auth, manager file placement/networking, reboot recovery,
and real Traefik routing in the approved activation window. No live cutover is
claimed by this test suite.

## Provenance / reuse

- Existing systemd speech/attachment bridges already use socket-proxyd; these
  templates reuse that implementation instead of writing another TCP server.
- Artbred's installed `nginx:alpine` reported RepoDigest
  `nginx@sha256:1d13701a5f9f3fb01aaa88cef2344d65b6b5bf6b7d9fa4cf0dca557a8d7702ba`.
  Tests use that immutable digest (nginx 1.29.5). Kuzin had no nginx image cached;
  it was cached solely for the disposable tests, not deployed as a service.
- Official documentation, discovered with the keyless Firecrawl developer index:
  [Unix socket proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass),
  [buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering),
  [request streaming](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_request_buffering),
  [headers](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header),
  [body size](https://nginx.org/en/docs/http/ngx_http_core_module.html#client_max_body_size).
  Tested nginx configuration intentionally bypasses the image entrypoint and
  default server; all writable pid/temp paths live in the restricted tmpfs.
