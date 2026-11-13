# Router Status — Redmi RM2100 (Padavan) collector + dashboard

## Files (on the Mac, `~/router-status/`)

- `collector.py` — polls the router via `ssh router` every 10 s, writes `data.json`.
- `server.py` — stdlib-only HTTPS dashboard on port 18933 (see `port.txt` if it moved).
- `org.zin.router-status-collector.plist` / `org.zin.router-status-server.plist` — LaunchAgents (RunAtLoad + KeepAlive).
- `deploy.sh` — port check, cert check, LaunchAgent (re)load, smoke test. Run with `bash ~/router-status/deploy.sh`.
- `data.json` — latest snapshot (written by the collector).
- `port.txt` — the port actually in use.

## Data sources (verified live on the router)

- `ip -s link` — per-interface RX/TX bytes (WAN eth3, LAN br0/eth2, 2.4G ra0, 5G rai0); Mbps from deltas between polls.
- `/proc/net/arp` + `/tmp/dnsmasq.leases` — device list (IP, MAC, hostname, interface), merged by MAC.
- `/proc/loadavg`, `/proc/meminfo`, `/proc/uptime` — system stats. Note: kernel 3.4 has no `MemAvailable`, so memory-used falls back to `MemFree`.
- NOT available by design: WiFi signal strength, WAN IPv4. WAN status = interface up + traffic flowing.

## Auth

Shared with mac-compute: `~/mac-compute/auth_store.py`, credentials in
`~/mac-compute/auth.json` (mode 600). First visit to the dashboard shows
`/setup` (choose username + password, min 8 chars); afterwards `/login`.
The session cookie is HttpOnly, Secure (over HTTPS), SameSite=Lax, 30 days.
This service never creates `auth.json` except through the user's own
`/setup` submission. No password is stored anywhere in this folder.

## TLS

Real cert (when the cert task delivers it):
`~/mac-compute/certs/status.mac.zin7.de.fullchain.crt` + `.key`.
Until then the server falls back to a temporary self-signed cert
(`selfsigned.crt` / `selfsigned.key`, 30 days, minted by `deploy.sh`) or
plain HTTP if even that is absent — testing only; final state must be the
real cert. To switch to the real cert: drop the files in place and
`launchctl kickstart -k gui/$(id -u)/org.zin.router-status-server`
(or re-run `deploy.sh`).

## Endpoints

- `GET /health` — public, `{"status":"ok"}`
- `GET /` — dashboard (auth; redirects to `/setup` or `/login`)
- `GET /api/data` — JSON snapshot (auth)
- `GET/POST /login`, `GET/POST /setup`, `GET /logout`

## Logs

- `collector.log`, `server.log` (LaunchAgent stdout+stderr).
- Check status: `launchctl list | grep router-status`
