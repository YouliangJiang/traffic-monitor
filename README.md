# traffic-monitor

English: [README.en.md](README.en.md)

用 Telegram 看主机流量和基本状态。Python 3.9+ 标准库 + systemd，不需要 pip、额外 RPM 或 Docker。

一个 Telegram bot token 同一时间只能有一个进程做 `getUpdates`。因此默认是一台 **hub** 收机器人消息，其他机器跑 **agent**，主动向 hub 上报。

机器相关的值不要写进 git：

| 位置 | 用途 |
|---|---|
| `/etc/traffic-monitor.env`（权限 `0600`） | 每台服务器上的 token、节点名、额度、网卡 |
| `deploy.local`（由 `deploy.local.example` 复制，已 gitignore） | 你本机 SSH 别名、可选的 hub 地址。尽量写 SSH alias，不要写公网 IP |
| `/var/lib/traffic-monitor/inventory.json` | 节点额度、可选服务探活。只存在 hub 本机，不要提交 |

点 bot 消息下面的按钮即可，不必手打节点名。
点消息里的「中文」/「English」可切换 bot 语言；选择会写到 `/var/lib/traffic-monitor/ui.json`，优先于环境变量 `UI_LANG`。


## 流量怎么计

- 读内核 `/proc/net/dev` 指定网卡（默认 `eth0`）的收/发字节，入站和出站都记。
- 差值按时间记入 `/var/lib/traffic-monitor/traffic.json`。只填重置日时，时刻按 `00:00:00`。监控进程短时间挂了但机器没重启，内核计数还在，下次采样会补上。
- 账单周期用这台机器的系统时区（`timedatectl`）。重置时刻由 `BILLING_RESET_DAY` + 可选的 `BILLING_RESET_TIME`（时:分:秒，没写的分和秒为 0，只写日期则 `00:00:00`）决定。额度按 **十进制**（`2T` = 2×10¹² 字节），与多数云厂商「套餐含入+出」的口径一致。
- 第一次安装时会留下开机快照 `bootstrap.json`，用来补上「装监控之前、自本次开机以来」的计数。

这是预警账，不是云账单对账单。重启前最后一次采样到关机之间会丢掉一小段；hypervisor 计费和本机网卡也会有正常偏差。超额计费若只算出站，请另外看出站数字。


## 套餐将尽时自动断流

**前置条件：** 该节点同时配置了月额度（`MONTHLY_CAP_BYTES` 非 0）和重置时刻。无限流量不会切。不需要再配端口或业务名单。

- 用量到套餐的 **80%**：Telegram 告警。
- 用量到 **90%**：本机 nftables 切断公网业务（留 10% 缓冲），直到重置时刻再自动恢复。`/add` 如果没写重置时刻，只记账，不断流。
- 用量按一条时间线入账。重置可以不是 0 点。重置日当天、时刻之前的流量算上一周期。
- 保留 SSH、本监控（agent↔hub、hub 的 Telegram）、DNS/NTP/DHCP、链路本地 `169.254.0.0/16`（云厂商元数据/监控组件）、私网 RFC1918、`tailscale0`，以及 `tailscaled` 自己的隧道外层流量。其它公网出入站默认丢掉。
- 断流由独立的 root 助手 `traffic-cut.service` 执行，监控进程本身没有改防火墙的权限。另有每分钟一次的 `traffic-cut.timer`：监控进程停了，过了重置时刻也会把规则撤掉；断流期间 Telegram 地址变了会重套规则。
- 这不是云账单对账：入站 DDoS 仍按厂商口径计；本机只保证不再把大包打回公网。

重置可写到秒，例如 `/reset node 27T08:00:00` 或安装时 `--reset 27T08:00:00`。只写日期则当天 0 点；只写小时则分钟和秒为 0。时间按该机器的系统时区。

## 角色

**Hub**（第一台，也监控自己）

- `traffic-hub`：库存、心跳、本机采样（约每 20 秒）
- `traffic-bot`：Telegram 长轮询，只响应配置里的 `TELEGRAM_CHAT_ID`
- `traffic-monitor.timer`：每日摘要（默认本机 16:00）

**Agent**（更多机器）

- `traffic-agent`：本机记账，出站连 hub，并执行测速 / 网卡采样任务
- 不必对公网再开业务端口

不要在两台机器上同时跑 bot。

Hub 对 agent 只提供 **HTTPS**（TLS 1.2+，自签证书）。防火墙放行 **入站 TCP 8788**，不是 UDP，也不是 7 层。证书和私钥在 hub 的 `/var/lib/traffic-monitor/hub.{crt,key}`；部署 agent 时由脚本从 hub 拷走 `hub.crt` 做校验。Bearer token 仍要，但不再在明文 HTTP 里传。

Hub 的 `8788` **只在有 agent 要加入时**才需要对那些机器开放。只有一台机器时，bot 走本机 `https://127.0.0.1:8788`，不必对公网放行。若要放行，尽量限制来源 IP。

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
  --hub https://HUB_HOST:8788 \
  --name hk --cap 2T --reset 1 \
  user@hk-host
```

`FLEET_TOKEN` 可写在 `deploy.local`，或设置 `HUB_HOST` 让脚本从已有 hub 的 `/etc/traffic-monitor.env` 读取。

额度：`500G`、`1T`、`2T`、`unlimited`。网卡不是 `eth0` 时加 `--iface`。

## Telegram

先在客户端向 bot 发一条消息，再把 `TELEGRAM_CHAT_ID` 配上。之后以消息下面的按钮为主。

| 命令 / 按钮 | 作用 |
|---|---|
| `/all` 或「机群总览」 | 全部汇总 |
| `/nodes` | 覆盖列表 |
| `/go 名字` 或点机器名 | 一台详情 |
| `/traffic` `/today` `/cpu` `/mem` `/disk` `/uptime` | 可加名字或 `all` |
| `/svc 名字 xray 443,2053` | 可选：在该机探测这些 TCP 端口；不配则详情里不显示 |
| `/svc 名字 xray off` | 去掉该服务 |
| 「网速」或 `/net 名字` | 读网卡当前吞吐约 3 秒，**不打流** |
| 「测速」或 `/bw 名字 [秒]` | 对 Cloudflare 下载/上传，测公网带宽 |
| 「延迟」或 `/rtt 名字` | 这台机器 ping 欧洲、美国、中国、东南亚的固定地址 |
| `/add 名字 cap=2T reset=27` | 纳入覆盖；`reset=27T08:00:00` 可到秒 |
| `/cap 名字 500G` | 改额度；有额度+重置则 90% 断流 |
| `/reset 名字 27` | 改本机时区的重置时刻 |
| `/off` `/on` | 停用 / 重新启用（仍留在名单里） |
| `/kick 名字` | 踢出，需再 `/add` 才会回来 |
| 「中文」/「English」或 `/lang zh` `/lang en` | 切换 bot 语言 |

「网速」看的是网卡正在走的流量；「测速」才会主动打流。两者不是一回事。
「延迟」是这台机器到欧洲、美国、中国、东南亚的 ping。地址是固定的，不会就近落到本地。

## 可选服务探活

这是扩展项，**不配就不显示按钮**。Xray、Nginx 或别的进程都可以，只是「在那台机器上探测一组 TCP 端口是否在听」。

用 Telegram 配（写进 hub 的 `inventory.json`，不进 git）：

```
/svc NODE xray 443,2053
/svc NODE nginx 80,443 proc=nginx
/svc NODE xray off
/svc NODE off
```

- `NODE` 是节点名；服务名自定，小写字母、数字、短横线。
- 端口任意，逗号分隔。探测的是目标机本机 `127.0.0.1` / `::1`，所以只绑在 localhost 的 inbound 也能盯。
- `proc=` 可选，用来读进程 RSS；省略则默认等于服务名。
- 每台最多 4 个服务。再加一种服务再发一条 `/svc` 即可。

`/xray` 仍可用：若该机配过名为 `xray` 的服务就看它，否则看第一个已配服务。


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
| `BILLING_RESET_DAY` | 本机时区的月重置日（1–31） |
| `BILLING_RESET_TIME` | 当天本机时:分:秒，缺省为 0 |
| `MONTHLY_CAP_BYTES` | 额度字节数；`0` 表示不限额，也不会断流 |
| `DAILY_REPORT_HOUR_UTC` | 日报小时 |
| `HUB_BIND` / `HUB_PORT` | Hub 监听 |
| `HUB_URL` | Bot 连本机 hub，一般 `https://127.0.0.1:8788` |
| `FLEET_HUB_URL` | Agent 连 hub（`https://...`） |
| `FLEET_PUBLIC_URL` | 可选，给提示用的对外地址；不需要就留空 |
| `HUB_CA` | 校验 hub 的证书，默认 `/var/lib/traffic-monitor/hub.crt` |
| `UI_LANG` | bot 默认语言，`zh` 或 `en`。Telegram 按钮可覆盖，写入 `ui.json` |

## systemd 内存上限

| unit | MemoryMax | 说明 |
|---|---|---|
| `traffic-hub` | 96M | 仅 hub |
| `traffic-bot` | 56M | 仅 hub |
| `traffic-agent` | 96M | 仅 agent |
| `traffic-monitor.timer` | 48M oneshot | 日报 |
| `traffic-cut` | 32M oneshot | 套餐断流（root，nft） |

状态目录：`/var/lib/traffic-monitor`。代码安装到 `/opt/traffic-monitor`。
