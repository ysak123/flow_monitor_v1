# SSH Flow & Log Monitor (desktop) — design

## Intent
`assets/apex_monitor.py` runs inside the Linux enclave and serves a dashboard there.
The goal is the same monitoring from the Windows desktop in Google Chrome, for easier
day-to-day viewing: connect over SSH, watch APR/LEC flow progress, and live-tail any
log file by path.

## Goals
- Open in Chrome on the desktop; connect over SSH with the existing key.
- Keep the APR stage dashboard and the LEC dashboard (same scan logic as `apex_monitor.py`).
- New: a "Log file" viewer that live-tails any path typed in (follow mode, filter, error/warning highlight).
- Alerts: Chrome desktop notification + alarm sound on stage fail, run complete, or a log silent > N minutes.
- Start by double-clicking `start_monitor.bat`.

## Non-goals
- Password / 2FA login (key only).
- Editing files or running/killing jobs on Linux. Read-only.
- Installing anything on the desktop (no pip, no Node) or leaving anything running on Linux.

## Anti-goals
- Must not hammer the NFS / login host: one SSH connection, not one per refresh.
- Must not silently show stale data when the connection drops — disconnection is loud and obvious.

## Constraints (facts found)
- Desktop: Windows OpenSSH `ssh.exe` 9.5, Python 3.14, key `C:/Users/yeeliang.sak/.ssh/id_rsa`. No paramiko, no Node.
- Linux side: Python 3.6+ (as `apex_monitor.py` already assumes), stdlib only.
- Defaults from the author's personal config: host `<linux-host>`, user `<user>`,
  sound `C:\Windows\Media\Alarm01.wav`, silent threshold 50 min.
- Corporate network: no reliance on external CDNs/web fonts.

## Approaches considered
1. **Desktop bridge (chosen).** Small stdlib Python helper on Windows holds one SSH session and
   serves the page on `http://127.0.0.1:8899`. Nothing persistent on Linux; page owns the connect flow.
2. SSH port-forward to `apex_monitor.py` on Linux. Least code, but same old page, needs the remote
   server running, dies with the tunnel.
3. paramiko-based helper. Needs pip install; no advantage over `ssh.exe`.

## Design
### Pieces
- `monitor_bridge.py` (Windows, stdlib). HTTP server bound to 127.0.0.1 only. Starts one
  `ssh.exe -T -o BatchMode=yes -o ServerAliveInterval=30 -i <key> user@host python3 -u -c <bootstrap>`.
  The bootstrap reads one base64 line (the agent source) from stdin and `exec`s it, so the agent is
  shipped fresh on every connect and nothing is written to Linux disk.
  Requests/responses are JSON lines over the session's stdin/stdout, serialized by a lock, with a timeout.
  If the process dies, status goes to "disconnected" and the UI offers Reconnect (auto-retry with backoff).
- `remote_agent.py` (runs on Linux via the bridge). Contains the scan functions from `apex_monitor.py`
  (APR `scan_workarea`, LEC `scan_lec`, stage log tail) plus `tail_file(path, offset)` for the generic viewer
  (returns new bytes since offset, handles truncation/rotation, caps at last N lines on first read).
- `monitor.html` (served by the bridge). Single file, no external assets.
- `start_monitor.bat`: starts the bridge and opens Chrome at the URL.

### API (bridge)
`GET /` page · `POST /api/connect {host,user,key}` · `GET /api/conn` state ·
`POST /api/status` (APR) · `POST /api/lec_status` · `GET /api/log` (stage log) · `GET /api/lec_log` ·
`POST /api/tail {path, offset}` · `GET /api/sound` (streams the configured .wav).

### Behaviour
- Settings (host/user/key, watched paths, per-path silent threshold, refresh interval, alert toggles)
  saved in browser localStorage; first-run defaults from `notify_sys_config.json`.
- Alerts fire on *transitions* only (pass→fail, running→complete, fresh→silent), not every refresh.
  Sound repeats until acknowledged, capped at `play_duration_minutes`.

## Visual direction (frontend-design plan)
Subject: physical-design flow monitoring for a PnR engineer; long-running glanceable dashboard.
Direction: **"layout viewer"** — the flow drawn like a routed metal wire through vias, colours borrowed
from layout layer palettes, not a generic SaaS card grid.

Palette
- Oxide `#13232A` — page base
- Die `#1B323B` — panels
- Grid `#2C4A55` — rules, idle tracks
- Copper `#E0A45A` — running / current stage, primary action
- Diffusion `#7FCB8F` — pass
- Poly `#E36B6B` — fail; Text `#E4EEF0`, muted `#8FA9B0`

Type: **Bahnschrift** (ships with Windows; DIN engineering face, condensed width for stage names) for UI;
**Cascadia Mono → Consolas** for log text. Sentence case, no all-caps eyebrows.

Layout
```
+--------------------+-------------------------------------------------------+
| ● connected  host  |  km_noc_vnoc_l_01          RUNNING   7/12  58%        |
|--------------------|  ===o====o====o====o====o====o====[*]----o----o----o  |
| Runs               |  init fp  logic dft  cmp  cmpF cts  CLOCK route ...   |
|  ▸ vnoc_l_01  ███▒ |-------------------------------------------------------|
|  ▸ vnoc_l_03  ████ |  Log  apr_snps_clock_opt.log   [follow] [filter___]   |
|  ▸ vnoc_l_09  ██✕  |  ...live tail, errors in poly, warnings in copper...   |
|  + add path        |                                                       |
| LEC | Log files    |                                                       |
+--------------------+-------------------------------------------------------+
```
Left-aligned throughout. Left rail = connection + watched runs (mini progress tracks, like a layer list);
main = selected run's routed-wire pipeline, log tail below. Clicking a via opens that stage's log in the tail pane.

Principles: one memorable element (the routed-wire pipeline with the copper "signal" at the running stage);
everything else quiet. Only motion: the running via pulses. Disconnected state dims the whole main area
with a clear "Reconnect" — never stale-looking-live. Keyboard focus visible, reduced-motion respected.

## Left to the builder
Exact spacing/sizes, polling intervals (default 10 s status, 3 s tail), backoff timings, error copy.
