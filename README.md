# traffic-monitor

Lightweight host traffic monitor with a Telegram bot.

Runtime: **Python 3.9+ stdlib** and **systemd**. No pip, no extra RPMs, no Docker.

Host-specific values (Telegram token, node name, public address, SSH target) live in **local config**, not in git:

- On each server: `/etc/traffic-monitor.env` (mode 0600)
- On the machine you deploy from: `deploy.local` (copy from `deploy.local.example`)

## Current layout (this box)

One process talks to Telegram (`traffic-bot`) and a local hub (`traffic-hub`). Traffic days are stored in `/var/lib/traffic-monitor/traffic.json`.

Next step elsewhere can be **scheme 1**: each extra server only `sendMessage`s its own report, with no hub port and no IP in git. This repo is being kept free of host addresses so that path stays clean.

## Deploy

```bash
cp deploy.local.example deploy.local   # edit SSH_TARGET / tokens as needed
./deploy-remote.sh --name my-node --cap 2T --reset 27
```

Telegram credentials can stay in `/etc/traffic-monitor.env` on the target; you do not have to put them in `deploy.local`.

## Commands

`/all` `/nodes` `/go 名字` `/cpu` `/mem` `/disk` `/today` `/traffic` `/net` `/bw` `/xray` `/uptime`  
`/add` `/cap` `/off` `/on` `/kick`

Caps: `500G` `1T` `2T` `unlimited`. `/bw 名字` samples `/proc/net/dev` and does not generate traffic.

## Units

| unit | memory cap |
|---|---|
| traffic-hub | 80M |
| traffic-bot | 56M |
| traffic-agent | 56M |
| traffic-monitor.timer | 48M oneshot |
