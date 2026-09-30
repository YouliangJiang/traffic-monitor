# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Constraints

- Python 3.9+ **stdlib only** (no pip, no Docker), run as systemd units on Linux. CI runs 3.9 and 3.12, so no syntax newer than 3.9; every module starts with `from __future__ import annotations`.
- Modules are flat top-level scripts importing each other by bare name (`import util`). No package. Installed to `/opt/traffic-monitor` and run with that as working directory.
- Telegram text lives in `locales/zh.json` and `locales/en.json` (via `i18n.t`); add keys to both. Messages are HTML (`parse_mode=HTML`), so escape any agent-supplied string with `report.h`.
- README.md (Chinese) and README.en.md (English) mirror each other; update both.
- Never commit `/etc/traffic-monitor.env` contents (tokens, chat id); the template is `traffic-monitor.env.example`.

## Commands

```bash
python3 -m unittest discover -s tests -v                                   # full suite (CI)
python3 -m unittest tests.test_regressions.HubTests.test_quota_marks_fire_once_per_period -v   # single test
bash -n install.sh                                                          # CI shell check
python3 tests/benchmark_ledger.py .                                         # ledger memory/time, synthetic data
```

`hostinfo.py` reads `/proc`, so live sampling only works on Linux; for an end-to-end run use a Linux container (e.g. `python:3.9-slim` plus `openssl`) and start `hub.serve()` with `report.send_telegram` patched.

Tests isolate state via `TRAFFIC_MONITOR_STATE_DIR` pointed at a temp dir, patch `util.local_tz` to UTC and `report.send_telegram` to capture messages (see `tests/test_regressions.py:Base`). Resolve state paths through `util.state_dir()`, never hardcode `/var/lib/...`.

## Architecture

Push-only fleet monitor: every host samples itself; agents POST to one hub; the hub sends Telegram messages. There is no Telegram bot (no `getUpdates`).

- `snapshot.build(config)` is the single sample used by both roles: `hostinfo.collect()` (CPU/mem/disk/NIC rate from `/proc`) + `counters.usage()` (billing period, today, yesterday). Accounting failures never raise here: they yield `traffic_ok=False` + `traffic_error` so resources still get reported and the hub alerts on the broken ledger. `snapshot.validate()` is the hub's input boundary for agent data.
- `agent.py`: loop every 20s → `tlsutil.post_json(HUB_URL/v1/report)`. `--check` does one round (used by `install.sh`).
- `hub.py`: `Hub` holds `runtime` (latest snapshot + last_seen per node, memory only), `nodes.json` (known nodes, persisted so a node that never returns still alerts offline) and `alerts.json` (per-node alert flags, quota marks fired per period, last daily date). Two threads: `collect` (samples the hub itself) and `notify` (`check_alerts` + `maybe_daily`). HTTP: `POST /v1/report` (Bearer `AGENT_TOKEN`), `GET /healthz`. `--test` sends a Telegram test message; `--forget NAME` edits state while the hub is stopped.
- Alert state machine (`Hub._events`): each condition produces a message on transition only (offline/online, disk and mem with a 5-point hysteresis, traffic accounting error/recovered, quota 80%/90% once per `period_start`). All events for a node go out as one message; state is committed only after `send_telegram` succeeds, so failures retry on the next pass.
- Auth/TLS: one shared `AGENT_TOKEN`. The hub cert is self-signed (`tlsutil.ensure_hub_cert`, openssl CLI); agents pin its SHA-256 (`HUB_FINGERPRINT`) and verify it before sending the token.
- Traffic accounting (`counters.py`): `/proc/net/dev` deltas stored as `(start_us, end_us, rx, tx)` intervals in `traffic.sqlite3`, keyed by `boot_id` (reboot counts from boot time; counter decrease without reboot records 0). `_sum` splits edge-crossing intervals proportionally with integer math so adjacent periods conserve bytes. Billing periods use the host timezone with a second-precision reset (`billing_period`); caps are decimal bytes. Billing config is per-host env (`MONTHLY_CAP_BYTES`, `BILLING_RESET_DAY`, `BILLING_RESET_TIME`), not managed by the hub.
- `install.sh hub|agent|forget|uninstall`: writes `/etc/traffic-monitor.env` (re-runs keep existing values for omitted flags), copies the file list in `FILES` to `/opt/traffic-monitor`, installs one unit (`systemd/traffic-hub.service` or `traffic-agent.service`), runs as user `trafficmon`. It also removes units and the `inet trafficmon-cut` nft table left by the previous (bot + cutoff) design. When adding a module, add it to `FILES`.
