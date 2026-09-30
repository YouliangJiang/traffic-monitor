# traffic-monitor

English: [README.en.md](README.en.md)

每台机器采集资源和月流量，上报到一台汇总节点（hub）；hub 每天推送一次汇总到 Telegram，出现异常时即时推送。Python 3.9+ 标准库 + systemd，不需要 pip 或 Docker。

## 工作方式

- **agent**（每台被监控的机器）：每 20 秒采集 CPU、内存、磁盘、网卡速率和本机流量，通过 HTTPS 上报给 hub。
- **hub**（汇总节点）：同样采集本机数据；接收各 agent 的上报；定时发每日汇总，出现异常时推送告警。hub 只调用 Telegram 的 `sendMessage`，不接收消息。

异常推送（每种异常只在触发时推送一次，恢复时再推送一次）：

| 异常 | 默认阈值 | env 变量 |
|---|---|---|
| 节点离线 | 超过 300 秒没有上报 | `OFFLINE_ALERT_SEC` |
| 磁盘使用率 | ≥ 90%，降到 85% 以下算恢复 | `DISK_ALERT_PCT` |
| 内存使用率 | ≥ 90%，降到 85% 以下算恢复 | `MEM_ALERT_PCT` |
| 月流量 | 达到上限的 80%、90% 各推送一次（仅设置了上限的节点） | 安装时 `--cap` |
| 流量统计异常 | 本机账本读写失败 | — |

每日汇总默认在 hub 本机时间 09:00 推送（`DAILY_REPORT_TIME`），内容为每个节点的在线状态、CPU/内存/磁盘、昨日与今日流量、本账期用量和上限。

## 安装

目标机需要 root、systemd、`python3`；hub 还需要 `openssl`。把仓库拷到机器上，在仓库目录执行。

**1. 汇总节点（hub）**

```bash
sudo ./install.sh hub --name hk --tg-token 123456:ABC... --tg-chat 987654321 --cap 2T --reset 1
```

安装时会发送一条 Telegram 测试消息，并打印 agent 的安装命令（其中包含共享 token 和 hub 证书指纹）。在 hub 上放行入站 **TCP 8788**，尽量只允许 agent 的来源 IP 访问。

**2. 其他机器（agent）**：复制 hub 打印出的命令，改掉 `--name`、`--hub` 地址和流量参数：

```bash
sudo ./install.sh agent --name sg --hub https://HUB_IP:8788 \
  --token <hub 打印的 token> --fingerprint <hub 打印的指纹> \
  --cap 500G --reset 27T08:00
```

安装时会先上报一次，失败会给出提示。

**升级 / 修改配置**：重新执行 `install.sh hub|agent`，只带要改的参数，其余参数沿用 `/etc/traffic-monitor.env` 里的值。

**下线某台机器**：先在那台机器上执行 `sudo ./install.sh uninstall`，再到 hub 上执行 `sudo ./install.sh forget sg`，否则 hub 会一直报它离线。

参数说明：

| 参数 | 说明 |
|---|---|
| `--name` | 节点名，`[a-z][a-z0-9-]{0,31}` |
| `--cap` | 月流量上限，十进制单位（`2T` = 2×10¹² 字节）；`unlimited` 或不填表示不限量 |
| `--reset` | 账期重置时刻，本机时区：`27` 表示每月 27 日 00:00:00，`27T08:00` 表示 27 日 08:00；短月份自动取月末那天 |
| `--iface` | 统计哪块网卡，默认取默认路由所在的网卡 |
| `--port` / `--daily` / `--lang` | 仅 hub 可用：监听端口 / 每日汇总时间 / 消息语言 `zh` 或 `en` |

## 月流量怎么统计

- 每 20 秒读取 `/proc/net/dev` 中指定网卡的 rx/tx 累计字节数，把增量按时间区间写入 `/var/lib/traffic-monitor/traffic.sqlite3`。入站和出站都计入。
- 服务停止但机器没有重启时，内核计数器仍在累加，下次采样会把这段补上；机器重启后从开机时刻重新计数。
- 跨过账期重置时刻或零点的区间，按时间比例拆分到两边，总字节数守恒。
- agent 在本机计算账期用量，hub 宕机期间照常记账。

这是预警用的账本，不能替代云厂商账单：关机或重启前最后一次采样之后的流量（最多约 20 秒）会丢失；hub 在虚机网卡上计量，厂商在宿主机或交换机上计量，口径本来就有差异；账期中途才安装时，安装之前的用量统计不到。

## 安全

- agent 与 hub 之间用 HTTPS 通信。hub 使用自签证书，agent 用 SHA-256 指纹固定校验，指纹不对就不会发出 token。
- 所有 agent 共用一个 `AGENT_TOKEN`，只能用来上报。
- 服务以 `trafficmon` 用户运行，不需要 root 权限。

## 文件位置

| 路径 | 用途 |
|---|---|
| `/etc/traffic-monitor.env`（0600） | 配置与 token，不要提交到 git，参考 `traffic-monitor.env.example` |
| `/opt/traffic-monitor` | 程序 |
| `/var/lib/traffic-monitor` | 流量账本；hub 另有 `nodes.json`（已知节点）、`alerts.json`（告警状态）、`hub.crt/key` |

## 开发

```bash
python3 -m unittest discover -s tests -v
bash -n install.sh
python3 tests/benchmark_ledger.py .   # 用合成数据测试账本的内存和耗时
```
