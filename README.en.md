# traffic-monitor

中文: [README.md](README.md)

Each machine collects resource usage and monthly traffic and reports to one **hub**. The hub pushes a daily summary to Telegram and pushes anomalies as they happen. Python 3.9+ stdlib + systemd. No pip, no Docker.

## How it works

- **agent** (every monitored machine): samples CPU, memory, disk, NIC rate and the local traffic ledger every 20s, and POSTs it to the hub over HTTPS.
- **hub**: samples itself too, receives agent reports, sends the daily summary and anomaly alerts. It only calls Telegram `sendMessage`; there is no bot to talk to.

Anomalies (one message when a condition starts, one when it clears):

| Condition | Default | env |
|---|---|---|
| Node offline | no report for 300 s | `OFFLINE_ALERT_SEC` |
| Disk usage | ≥ 90%, clears below 85% | `DISK_ALERT_PCT` |
| Memory usage | ≥ 90%, clears below 85% | `MEM_ALERT_PCT` |
| Monthly traffic | 80% and 90% of the cap, once each per period (only nodes with a cap) | `--cap` at install |
| Traffic accounting failure | local ledger unreadable/unwritable | — |

The daily summary goes out at 09:00 hub-local time (`DAILY_REPORT_TIME`): per node online state, CPU/memory/disk, yesterday's and today's traffic, and period usage against the cap.

## Install

Targets need root, systemd and `python3`; the hub also needs `openssl`. Copy the repository to the machine and run from its directory.

**1. Hub**

```bash
sudo ./install.sh hub --name hk --tg-token 123456:ABC... --tg-chat 987654321 --cap 2T --reset 1
```

It sends a Telegram test message and prints the agent command with the shared token and the hub certificate fingerprint. Open inbound **TCP 8788** on the hub, restricted to agent source IPs if you can.

**2. Agents**: run the printed command, adjusting name, hub address and traffic options:

```bash
sudo ./install.sh agent --name sg --hub https://HUB_IP:8788 \
  --token <token from hub> --fingerprint <fingerprint from hub> \
  --cap 500G --reset 27T08:00
```

The installer reports once and warns if that fails.

**Upgrade / change settings**: re-run `install.sh hub|agent` with only the options to change; the rest come from `/etc/traffic-monitor.env`.

**Retire a machine**: `sudo ./install.sh uninstall` on it, then `sudo ./install.sh forget sg` on the hub (otherwise it stays "offline").

| Option | Meaning |
|---|---|
| `--name` | node name, `[a-z][a-z0-9-]{0,31}` |
| `--cap` | monthly cap, decimal units (`2T` = 2×10¹² bytes); `unlimited` or omitted means no cap |
| `--reset` | period reset in host local time: `27` = day 27 00:00:00, `27T08:00` = 08:00; short months use their last day |
| `--iface` | accounting NIC, default is the default-route interface |
| `--port` / `--daily` / `--lang` | hub only: listen port / summary time / `zh` or `en` |

## How monthly traffic is counted

- Every 20s the rx/tx byte counters of the NIC in `/proc/net/dev` are read and the delta is stored as a timestamped interval in `/var/lib/traffic-monitor/traffic.sqlite3`. Inbound and outbound both count.
- If the service stops but the host does not reboot, the kernel counters keep running and the next sample fills the gap. After a reboot, counting restarts from boot time.
- An interval crossing a reset instant or midnight is split proportionally; total bytes are conserved.
- Agents compute their own period usage, so accounting continues while the hub is down.

This is an early-warning ledger, not a provider invoice: traffic after the last sample before shutdown (≤ ~20s) is lost, the provider meters at the hypervisor or switch rather than the guest NIC, and usage before installation in a period is not seen.

## Security

- Agent ↔ hub is HTTPS. The hub cert is self-signed; agents pin its SHA-256 fingerprint and never send the token to anything else.
- One shared `AGENT_TOKEN`, which can only submit reports.
- Services run as the unprivileged `trafficmon` user.

## Files

| Path | Purpose |
|---|---|
| `/etc/traffic-monitor.env` (0600) | config and tokens; see `traffic-monitor.env.example`, never commit a real one |
| `/opt/traffic-monitor` | code |
| `/var/lib/traffic-monitor` | traffic ledger; on the hub also `nodes.json` (known nodes), `alerts.json` (alert state), `hub.crt/key` |

## Development

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
python3 tests/benchmark_ledger.py .   # ledger memory/time with synthetic data
```
