# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Constraints

- Python 3.9+ **stdlib only** (no pip, no Docker), run as systemd units on Linux. CI runs 3.9 and 3.12, so no syntax newer than 3.9; every module starts with `from __future__ import annotations`.
- Modules are flat top-level scripts importing each other by bare name (`import util`). No package. Installed to `/opt/traffic-monitor` and run with that as working directory.
- Telegram text lives in `locales/zh.json` and `locales/en.json` (via `i18n.t`); add keys to both. Messages are HTML (`parse_mode=HTML`), so escape any agent-supplied string with `report.h`.
- README.md (Chinese) and README.en.md (English) mirror each other; update both.
- Never commit `/etc/traffic-monitor.env` or `deploy.local` contents (tokens, chat id); the templates are `traffic-monitor.env.example` and `deploy.local.example`.
- Units run sandboxed (`ProtectSystem=strict`, `MemoryMax=96M`, no capabilities, hub `TasksMax=32`): the only writable path is the state directory, and nothing may need root.

## Legacy files (dead code)

The live code is exactly `FILES` in `install.sh` plus `locales/`, `systemd/traffic-hub.service`, `systemd/traffic-agent.service` and `tests/test_regressions.py`, `tests/benchmark_ledger.py`. Still tracked from the previous bot + cutoff design but not installed, not tested and broken against the current helpers (they call removed functions such as `util.http_json`, `report.telegram_call`, `formatters.fleet_overview`): `bot.py`, `cut.py`, `cutctl.py`, `protocol.py`, `deploy.py`, `deploy-remote.sh`, `enroll-agent.py`, `install-host.sh`, `tests/native_nft.py` and the other `systemd/` units. Do not read them as a description of current behaviour, extend them, or keep helpers alive for them.

## Commands

```bash
python3 -m unittest discover -s tests -v                                   # full suite (CI)
python3 -m unittest tests.test_regressions.HubTests.test_quota_marks_fire_once_per_period -v   # single test
bash -n install.sh                                                          # CI shell check
python3 tests/benchmark_ledger.py .                                         # ledger memory/time, synthetic data
```

`hostinfo.py` reads `/proc`, so live sampling only works on Linux; for an end-to-end run use a Linux container (e.g. `python:3.9-slim` plus `openssl`) and start `hub.serve()` with `report.send_telegram` patched.

The unit suite itself runs on macOS too: it never touches `/proc` (`counters._iface_bytes`, `counters._boot_id` and `hostinfo.collect` are patched) and `TlsTests` is skipped when `openssl` is absent. The traceback printed by `test_accounting_failure_still_reports_resources` is expected output.

Tests isolate state via `TRAFFIC_MONITOR_STATE_DIR` pointed at a temp dir, patch `util.local_tz` to UTC and `report.send_telegram` to capture messages (`start_workers` is never called, so nothing polls Telegram) (see `tests/test_regressions.py:Base`). Resolve state paths through `util.state_dir()`, never hardcode `/var/lib/...`.

## Architecture

Fleet monitor: every host samples itself; agents POST to one hub; the hub sends Telegram messages (daily summary, alerts) and answers the read-only `/status` command. The hub never connects to agents.

- `snapshot.build(config)` is the single sample used by both roles: `hostinfo.collect()` (CPU/mem/disk/NIC rate from `/proc`) + `counters.usage()` (billing period, today, yesterday). Accounting failures never raise here: they yield `traffic_ok=False` + `traffic_error` so resources still get reported and the hub alerts on the broken ledger. `snapshot.validate()` is the hub's input boundary for agent data.
- The snapshot schema lives in three places that must change together: the producers (`hostinfo.collect`, `counters.usage`, `snapshot.build`), the key lists checked by `snapshot.validate` (`INT_KEYS`, `NUM_KEYS`, string keys), and the `snap()` fixture in the tests. Hosts are upgraded one at a time by re-running `install.sh`, and `validate` rejects a snapshot missing any listed key with a 400, so a new field must be optional on the hub until every agent sends it.
- `agent.py`: loop every 20s → `tlsutil.post_json(HUB_URL/v1/report)`. `--check` does one round (used by `install.sh`).
- `hub.py`: `Hub` holds `runtime` (latest snapshot + last_seen per node, memory only), `nodes.json` (known nodes, persisted so a node that never returns still alerts offline) and `alerts.json` (per-node alert flags, quota marks fired per period, last daily date). Two threads: `collect` (samples the hub itself) and `notify` (`check_alerts` + `maybe_daily`). HTTP: `POST /v1/report` (Bearer `AGENT_TOKEN`), `GET /healthz`, `GET /v1/status` (token **and** a peer address equal to the local socket address, i.e. this machine only, so the shared agent token cannot read the fleet). `--test` sends a Telegram test message; `--forget NAME` edits state while the hub is stopped; `--status [--send]` (`install.sh status`) asks the running hub over `/v1/status`, because `runtime` exists only in that process.
- Telegram commands (`Hub._command_loop` → `poll_commands` → `handle_update`): a third thread long-polls `report.get_updates` unless `TELEGRAM_COMMANDS=0`. Only messages from `TELEGRAM_CHAT_ID` newer than `COMMAND_MAX_AGE` are handled; commands are read-only (`/status`, `/help`) and must stay that way without a stronger sender check. The offset is memory only and advances before the reply is sent. Errors back off (5s→300s) with one log line, not a traceback. `formatters.status` is the daily layout plus the NIC rate; `formatters.plain` strips the HTML for the terminal.
- Alert state machine (`Hub._events`): each condition produces a message on transition only (offline/online, disk and mem with a 5-point hysteresis, traffic accounting error/recovered, quota 80%/90% once per `period_start`). All events for a node go out as one message; state is committed only after `send_telegram` succeeds, so failures retry on the next pass. Two different staleness thresholds apply: a node is shown "online" for `STALE_AFTER` (90s) after its last report, while the offline alert fires after `OFFLINE_ALERT_SEC` (300s); in between, and whenever a node is not online, its disk/mem/traffic/quota conditions are not evaluated. A node that has not reported since the hub started is aged from the hub start time.
- Daily summary (`Hub.maybe_daily`): once per hub-local date at `DAILY_REPORT_TIME`. With no `daily` key in `alerts.json` (fresh install) it only arms the next day, and it waits `3 * SAMPLE_SEC` after a hub start so agents are not all listed as missing.
- Auth/TLS: one shared `AGENT_TOKEN`. The hub cert is self-signed (`tlsutil.ensure_hub_cert`, openssl CLI); agents pin its SHA-256 (`HUB_FINGERPRINT`) and verify it before sending the token.
- Traffic accounting (`counters.py`): `/proc/net/dev` deltas stored as `(start_us, end_us, rx, tx)` intervals in `traffic.sqlite3`, keyed by `boot_id` (reboot counts from boot time; counter decrease without reboot records 0). `_sum` splits edge-crossing intervals proportionally with integer math so adjacent periods conserve bytes. Billing periods use the host timezone with a second-precision reset (`billing_period`); caps are decimal bytes. Billing config is per-host env (`MONTHLY_CAP_BYTES`, `BILLING_RESET_DAY`, `BILLING_RESET_TIME`), not managed by the hub.
- Interpreter: `install.sh` `find_python` picks the first Python 3.9+ with `ssl` and `sqlite3` (`PYTHON=` override, then `python3`, `python3.13`…`python3.9`, then `/usr/local/bin/*` because sudo's PATH omits it), uses it for every step and rewrites the unit's `ExecStart=/usr/bin/python3` with that path. The fleet includes CentOS 7/8 hosts whose `python3` is 3.6, so never call bare `python3` in `install.sh` and keep that `ExecStart` prefix in the unit files.
- `install.sh hub|agent|status|forget|uninstall`: writes `/etc/traffic-monitor.env` (re-runs keep existing values for omitted flags), copies the file list in `FILES` to `/opt/traffic-monitor`, installs one unit (`systemd/traffic-hub.service` or `traffic-agent.service`), runs as user `trafficmon`. It also removes units and the `inet trafficmon-cut` nft table left by the previous (bot + cutoff) design. When adding a module, add it to `FILES`.
- The env file is regenerated from a fixed key list by the Python heredoc inside `install.sh` (which imports `util` from the repo for parsing/validation), so any key not written there is dropped on the next re-run. A new setting needs: that heredoc, `traffic-monitor.env.example`, the `util.env*` read, and both READMEs. `OFFLINE_ALERT_SEC`, `DISK_ALERT_PCT`, `MEM_ALERT_PCT`, `TELEGRAM_COMMANDS` and `HUB_BIND` have no flag; they are edited in the env file and preserved.
- `deploy.local` (gitignored, next to `install.sh`) supplies the keys in the heredoc's `LOCAL_KEYS` (both roles: `NODE_NAME`, `MONTHLY_CAP`, `BILLING_RESET`; hub: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; agent: `HUB_URL`, `AGENT_TOKEN`, `HUB_FINGERPRINT`) when the matching flag is omitted. The hub never takes `AGENT_TOKEN` from it (generated once, then kept from the env file). `MONTHLY_CAP`/`BILLING_RESET` use the `--cap`/`--reset` syntax, not the env file's `MONTHLY_CAP_BYTES`/`BILLING_RESET_DAY`/`BILLING_RESET_TIME`. Precedence in `pick`/`given`: flag > `deploy.local` > current env file > default. Other keys in it are ignored.
