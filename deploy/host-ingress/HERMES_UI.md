# Authenticated private Hermes UI

`https://ui.sashakuzina.com` is a dedicated native dashboard listener for the existing default Hermes profile, not another agent installation or a new data store.

```
Tailnet client -> Traefik TLS + direct-source tailnet allowlist
 -> hermes-host-ingress:9124 -> /sockets/ui.sock
 -> systemd-socket-proxyd -> localhost4:9124 -> native dashboard auth gate
```

The old dashboard on localhost:9119 and its speech/API ingress remain unchanged. Do not set the default profile's global `dashboard.public_url` for this deployment: that would engage its auth gate and change existing native-client token behavior. The UI user service sets the supported `HERMES_DASHBOARD_PUBLIC_URL` environment override for this listener only. `--isolated` explicitly permits another listener while retaining the same profile database, sessions and Kanban board.

## Deployment ownership

- Deploy the Git-backed host-ingress update through Dokploy, preserving its internal network, pinned image and no host port publication.
- Install `backends/ui.conf` as `/etc/hermes-host-ingress/ui.conf`, and enable the existing `hermes-host-ingress@ui.socket` template instance. Do not rewrite the common socket/service template or existing backend configs.
- Install `systemd/user/hermes-ui.service` into the root user's systemd user directory. It depends on the existing dashboard for update/restart propagation, not on the messaging gateway.
- Keep the new native listener bound to `127.0.0.1:9124`.
- Install `traefik/hermes-ui.yaml` as a new file-provider config. This host's `letsencrypt` resolver already uses Cloudflare DNS-01. No new reverse-proxy daemon or Tailscale Serve/Funnel route is involved.
- DNS: explicit unproxied A record for `ui.sashakuzina.com` to this host's current Tailscale IPv4. Do not create a public-origin A/AAAA or orange-cloud this private address.

## Credentials

The user service reads the active owner's `.env` with mode 0600. The native basic provider uses `HERMES_DASHBOARD_BASIC_AUTH_PASSWORD` and a persistent `HERMES_DASHBOARD_BASIC_AUTH_SECRET`. The username is `artem` in this UI service only. Generate new values privately only if absent; do not rotate an existing value as an incidental deploy action. No password, signed session, private key or deployment token belongs in this repository, command argv, logs or chat.

This single-user provider is intended for trusted/VPN access. Both the native sign-in and the direct-source tailnet allowlist are required; never replace them with injected legacy session tokens or by rewriting the browser's Origin to localhost.

## Verification

Run the contract, cutover and disposable integration suites:

```
HERMES_INGRESS_INTEGRATION=1 python3 -B -m unittest discover -s deploy/host-ingress/tests -v
```

The UI-specific fixture proves HTTP transparency, authentication preservation, uploads, SSE and a WebSocket upgrade through the Unix transport, without enabling upgrades on legacy ports. Live acceptance additionally requires trusted HTTPS, native sign-in, protected Kanban API access, an authenticated WebSocket/ticket handshake, rejection of invalid credentials/cross-origin requests, public-origin denial even with forged forwarding headers, and unchanged legacy dashboard/messaging PIDs. Do not call a successful login-page GET a completed authenticated UI test.
