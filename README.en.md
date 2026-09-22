# traffic-monitor

中文: [README.md](README.md)

Watch host traffic and basic health over Telegram. Python 3.9+ stdlib + systemd. No pip, extra RPMs, or Docker.

A Telegram bot token can have only one `getUpdates` consumer at a time. The default layout is one **hub** that owns the bot, and **agents** on the other machines that report in.

Keep machine-specific values out of git:

| Location | Purpose |
|---|---|
| `/etc/traffic-monitor.env` (mode `0600`) | Tokens, node name, cap, NIC on each host |
| `deploy.local` (copy from `deploy.local.example`, gitignored) | SSH aliases and optional hub URL on the machine you deploy from. Prefer SSH aliases, not public IPs |
| `/var/lib/traffic-monitor/inventory.json` | Node caps and optional service watches. Lives on the hub only; do not commit |

Tap the buttons under bot messages. You do not need to type node names.

Tap **中文** / **English** on a message to switch UI language. The choice is stored in `/var/lib/traffic-monitor/ui.json` and overrides `UI_LANG`.

## How traffic is counted

- Read rx/tx bytes for a chosen NIC (default `eth0`) from `/proc/net/dev`. Inbound and outbound both count.
- Deltas are stored on one timeline in `/var/lib/traffic-monitor/traffic.json`. A day-only reset means `00:00:00`. If the monitor process dies briefly but the host does not reboot, the kernel counters remain and the next sample fills the gap.
- Billing periods use the host timezone (`timedatectl`). The reset instant is `BILLING_RESET_DAY` plus optional `BILLING_RESET_TIME` (`HH`, `HH:MM`, or `HH:MM:SS`; omitted minutes and seconds are 0, a day alone is `00:00:00`). Caps are **decimal** (`2T` = 2×10¹² bytes), matching most cloud "plan includes in+out" wording.
- First install writes `bootstrap.json` so usage since the current boot, before the monitor was installed, can be included.

This is an early-warning ledger, not a cloud invoice. A short gap is lost between the last sample and shutdown; hypervisor billing and the host NIC also differ in normal ways. If overage is egress-only, look at the outbound figure separately.


## Cutoff when the monthly cap is nearly gone

**Requirement:** the node has both a monthly cap (`MONTHLY_CAP_BYTES` not 0) and a reset instant. Unlimited nodes are never cut. You do not configure ports or service names for this.

- At **80%** of the cap: Telegram alert.
- At **90%**: host nftables drops public traffic (10% buffer) until the reset instant, then restores automatically. `/add` without a reset instant only keeps the ledger; it does not arm cutoff.
- Usage is one timeline, so a reset that is not midnight splits that local day.
- Kept: SSH, this monitor (agent↔hub and hub Telegram), DNS/NTP/DHCP, link-local `169.254.0.0/16` (cloud metadata/vendor agents), RFC1918 private paths, `tailscale0`, and `tailscaled`'s own tunnel underlay. Everything else to/from the public internet is dropped.
- A separate root helper `traffic-cut.service` applies the table; the unprivileged monitor cannot change firewall rules. `traffic-cut.timer` runs once a minute: if the monitor is down it still lifts the rules after the reset instant, and it refreshes Telegram addresses while a cutoff is in place.
- This is not the cloud invoice: inbound floods still follow the provider’s rules; the host only stops sending large replies.

Reset can be second-precise, e.g. `/reset node 27T08:00:00` or `--reset 27T08:00:00`. A day-only value means 00:00:00 that day in the host timezone. An hour alone means minute and second 0.

## Roles

**Hub** (first host; it also monitors itself)

- `traffic-hub`: inventory, heartbeats, local sampling (~every 20s)
- `traffic-bot`: Telegram long poll; only the configured `TELEGRAM_CHAT_ID` is accepted
- `traffic-monitor.timer`: daily summary (default 16:00 host local time)

**Agent** (more hosts)

- `traffic-agent`: local accounting, outbound to the hub, and NIC / public speed-test jobs
- No extra public service port is required

Do not run the bot on two hosts at once.

The hub speaks **HTTPS only** to agents (TLS 1.2+, self-signed). Open **inbound TCP 8788**, not UDP, not L7. Cert and key live on the hub at `/var/lib/traffic-monitor/hub.{crt,key}`; the deploy script copies `hub.crt` to agents for verification. A bearer token is still required.

Hub port `8788` needs to be reachable **only when agents join**. With a single machine the bot uses `https://127.0.0.1:8788` and the port can stay closed. If you open it, restrict source IPs when you can.

## Install

The target host needs root or passwordless sudo, systemd, and `python3`.

On the machine you deploy from:

```bash
cp deploy.local.example deploy.local
# edit SSH_TARGET etc; the token can live only on the target in /etc/traffic-monitor.env
```

First host (hub):

```bash
./deploy-remote.sh --name my-node --cap 2T --reset 27
```

More hosts (agent). `HUB_URL` must be an address the **agent can actually reach**. Do not commit that address:

```bash
./deploy-remote.sh --role agent \
  --hub https://HUB_HOST:8788 \
  --name hk --cap 2T --reset 1 \
  user@hk-host
```

`FLEET_TOKEN` can live in `deploy.local`, or set `HUB_HOST` so the script reads it from the existing hub's `/etc/traffic-monitor.env`.

Caps: `500G`, `1T`, `2T`, `unlimited`. Add `--iface` when the NIC is not `eth0`.

## Telegram

Send the bot a message first, then set `TELEGRAM_CHAT_ID`. After that, prefer the buttons under the message.

| Command / button | What it does |
|---|---|
| `/all` or Overview | Fleet summary |
| `/nodes` | Coverage list |
| `/go name` or tap a machine | One node |
| `/traffic` `/today` `/cpu` `/mem` `/disk` `/uptime` | Optional name or `all` |
| `/svc name xray 443,2053` | Optional: probe those TCP ports on the node; hidden if unset |
| `/svc name xray off` | Remove that service |
| NIC or `/net name` | Sample current NIC occupancy for ~3s, **no generated traffic** |
| Speed test or `/bw name [seconds]` | Download/upload via Cloudflare (public bandwidth) |
| `/add name cap=2T reset=27` | Add to coverage; `reset=27T08:00:00` for seconds |
| `/cap name 500G` | Change cap; cap+reset arms 90% cutoff |
| `/reset name 27` | Change the reset instant in the host timezone |
| `/off` `/on` | Disable / enable (still listed) |
| `/kick name` | Remove; needs `/add` to return |
| 中文 / English or `/lang zh` `/lang en` | Switch bot language |

NIC occupancy is traffic currently on the card. Speed test generates traffic on purpose. They are not the same.

## Optional service watches

This is optional. **No button appears until you configure it.** Xray, Nginx, or anything else is just a name plus TCP ports probed on that host.

Configure from Telegram (stored in the hub `inventory.json`, not git):

```
/svc NODE xray 443,2053
/svc NODE nginx 80,443 proc=nginx
/svc NODE xray off
/svc NODE off
```

- `NODE` is the node name. Service names are yours: lowercase letters, digits, hyphens.
- Ports are arbitrary, comma-separated. Probes use `127.0.0.1` / `::1` on the target, so localhost-only inbounds work.
- `proc=` is optional (RSS). Defaults to the service name.
- At most 4 services per node. Add another with a second `/svc` line.

`/xray` still works: it opens the service named `xray` if present, otherwise the first configured service.


User-facing strings live in `locales/zh.json` and `locales/en.json`. Code only formats those keys.

## `/etc/traffic-monitor.env`

See `traffic-monitor.env.example`. Do not commit a filled-in copy.

| Variable | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Required on the hub |
| `FLEET_TOKEN` | Shared bearer for hub and agents |
| `ROLE` | `hub` or `agent` |
| `NODE_NAME` | `[a-z][a-z0-9-]{0,31}` |
| `TRAFFIC_IFACE` | Accounting NIC |
| `HOST_LABEL` | Name shown in Telegram; empty uses `NODE_NAME`, then hostname |
| `BILLING_RESET_DAY` | Reset day in the host timezone (1–31) |
| `BILLING_RESET_TIME` | Local `HH[:MM[:SS]]` that day; omitted fields are 0 |
| `MONTHLY_CAP_BYTES` | Cap in bytes; `0` means unlimited and no cutoff |
| `DAILY_REPORT_HOUR_UTC` | Daily summary hour |
| `HUB_BIND` / `HUB_PORT` | Hub listen address |
| `HUB_URL` | Bot to local hub, usually `https://127.0.0.1:8788` |
| `FLEET_HUB_URL` | Agent to hub (`https://...`) |
| `FLEET_PUBLIC_URL` | Optional public URL for join hints |
| `HUB_CA` | Hub cert for agents, default `/var/lib/traffic-monitor/hub.crt` |
| `UI_LANG` | Default bot language, `zh` or `en`. Telegram buttons override this in `ui.json` |

## systemd memory caps

| unit | MemoryMax | Notes |
|---|---|---|
| `traffic-hub` | 96M | hub only |
| `traffic-bot` | 56M | hub only |
| `traffic-agent` | 96M | agent only |
| `traffic-monitor.timer` | 48M oneshot | daily report |
| `traffic-cut` | 32M oneshot | Cap cutoff (root, nft) |

State directory: `/var/lib/traffic-monitor`. Code installs to `/opt/traffic-monitor`.
