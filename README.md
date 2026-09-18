# traffic-monitor

用 Telegram 看主机流量和基本状态。Python 3.9+ 标准库 + systemd，不需要 pip、额外 RPM 或 Docker。

一个 Telegram bot token 同一时间只能有一个进程做 `getUpdates`。因此默认是一台 **hub** 收机器人消息，其他机器跑 **agent**，主动向 hub 上报。

机器相关的值不要写进 git：

| 位置 | 用途 |
|---|---|
| `/etc/traffic-monitor.env`（权限 `0600`） | 每台服务器上的 token、节点名、额度、网卡 |
| `deploy.local`（由 `deploy.local.example` 复制，已 gitignore） | 你本机 SSH 别名、可选的 hub 地址。尽量写 SSH alias，不要写公网 IP |

## 流量怎么计

- 读内核 `/proc/net/dev` 指定网卡（默认 `eth0`）的收/发字节，入站和出站都记。
- 差值累加到按天账本：`/var/lib/traffic-monitor/traffic.json`。监控进程短时间挂了但机器没重启，内核计数还在，下次采样会补上。
- 账单周期按 **UTC**，重置日由 `BILLING_RESET_DAY` 决定。额度按 **十进制**（`2T` = 2×10¹² 字节），与多数云厂商「套餐含入+出」的口径一致。
- 第一次安装时会留下开机快照 `bootstrap.json`，用来补上「装监控之前、自本次开机以来」的计数。

这是预警账，不是云账单对账单。重启前最后一次采样到关机之间会丢掉一小段；hypervisor 计费和本机网卡也会有正常偏差。超额计费若只算出站，请另外看出站数字。

## 角色

**Hub**（第一台，也监控自己）

- `traffic-hub`：库存、心跳、本机采样（约每 20 秒）
- `traffic-bot`：Telegram 长轮询，只响应配置里的 `TELEGRAM_CHAT_ID`
- `traffic-monitor.timer`：每日摘要（默认 UTC 16:00）

**Agent**（更多机器）

- `traffic-agent`：本机记账，出站连 hub
- 不必对公网再开业务端口

不要在两台机器上同时跑 bot。

Hub 的 `8788` **只在有 agent 要加入时**才需要从那些机器访问到。只有一台机器时，bot 走本机回环，不必对公网放行该端口。若要放行，尽量限制来源 IP，鉴权靠 `FLEET_TOKEN`。

## 安装

目标机需要：root 或免密 sudo、systemd、`python3`。

在你用来部署的电脑上：

```bash
cp deploy.local.example deploy.local
# 编辑 SSH_TARGET 等；token 也可以只写在目标机的 /etc/traffic-monitor.env
```

第一台（hub）：

```bash
./deploy-remote.sh --name my-node --cap 2T --reset 27
```

再加机器（agent）。`HUB_URL` 写 agent **实际能访问到的** hub 地址，不要把该地址提交进仓库：

```bash
./deploy-remote.sh --role agent \
  --hub http://HUB_HOST:8788 \
  --name hk --cap 2T --reset 1 \
  user@hk-host
```

`FLEET_TOKEN` 可写在 `deploy.local`，或设置 `HUB_HOST` 让脚本从已有 hub 的 `/etc/traffic-monitor.env` 读取。

额度：`500G`、`1T`、`2T`、`unlimited`。网卡不是 `eth0` 时加 `--iface`。

## Telegram

先在客户端向 bot 发一条消息，再把 `TELEGRAM_CHAT_ID` 配上。

| 命令 | 作用 |
|---|---|
| `/all` | 全部汇总 |
| `/nodes` | 覆盖列表 |
| `/go 名字` | 一台详情 |
| `/traffic` `/today` `/cpu` `/mem` `/disk` `/net` `/xray` `/uptime` | 可加名字或 `all` |
| `/bw 名字 [秒]` | 被动采样网卡 1–15 秒，**不主动打流** |
| `/add 名字 cap=2T reset=27` | 纳入覆盖 |
| `/cap 名字 500G` | 改额度 |
| `/off` `/on` | 停用 / 重新启用（仍留在名单里） |
| `/kick 名字` | 踢出，需再 `/add` 才会回来 |

## `/etc/traffic-monitor.env`

对照 `traffic-monitor.env.example`。不要把填好的文件提交到 git。

| 变量 | 含义 |
|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Hub 必填 |
| `FLEET_TOKEN` | Hub 与 agent 共享的 bearer |
| `ROLE` | `hub` 或 `agent` |
| `NODE_NAME` | 节点名，`[a-z][a-z0-9-]{0,31}` |
| `TRAFFIC_IFACE` | 记账网卡 |
| `HOST_LABEL` | Telegram 里显示的名字；空则用 `NODE_NAME`，再退回主机名 |
| `BILLING_RESET_DAY` | UTC 月重置日 |
| `MONTHLY_CAP_BYTES` | 额度字节数；`0` 表示不限额 |
| `DAILY_REPORT_HOUR_UTC` | 日报小时 |
| `HUB_BIND` / `HUB_PORT` | Hub 监听 |
| `HUB_URL` | Bot 连本机 hub，一般 `http://127.0.0.1:8788` |
| `FLEET_HUB_URL` | Agent 连 hub |
| `FLEET_PUBLIC_URL` | 可选，给提示用的对外地址；不需要就留空 |

## systemd 内存上限

| unit | MemoryMax | 说明 |
|---|---|---|
| `traffic-hub` | 80M | 仅 hub |
| `traffic-bot` | 56M | 仅 hub |
| `traffic-agent` | 56M | 仅 agent |
| `traffic-monitor.timer` | 48M oneshot | 日报 |

状态目录：`/var/lib/traffic-monitor`。代码安装到 `/opt/traffic-monitor`。
