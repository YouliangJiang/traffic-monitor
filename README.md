# traffic-monitor

English: [README.en.md](README.en.md)

每台机器采集资源和月流量，上报到一台汇总节点（hub）；hub 每天推送一次汇总到 Telegram，出现异常时即时推送，也可以随时在 Telegram 里发 `/status` 或在 hub 上用命令查看当前状态。Python 3.9+ 标准库 + systemd，不需要 pip 或 Docker。

## 工作方式

- **agent**（每台被监控的机器）：每 20 秒采集 CPU、内存、磁盘、网卡速率和本机流量，通过 HTTPS 上报给 hub。
- **hub**（汇总节点）：同样采集本机数据；接收各 agent 的上报；定时发每日汇总，出现异常时推送告警，并回答 `/status` 查询。

异常推送（每种异常只在触发时推送一次，恢复时再推送一次）：

| 异常 | 默认阈值 | env 变量 |
|---|---|---|
| 节点离线 | 超过 300 秒没有上报 | `OFFLINE_ALERT_SEC` |
| 磁盘使用率 | ≥ 90%，降到 85% 以下算恢复 | `DISK_ALERT_PCT` |
| 内存使用率 | ≥ 90%，降到 85% 以下算恢复 | `MEM_ALERT_PCT` |
| 月流量 | 达到上限的 80%、90% 各推送一次（仅设置了上限的节点） | 安装时 `--cap` |
| 流量统计异常 | 本机账本读写失败 | — |

每日汇总默认在 hub 本机时间 09:00 推送（`DAILY_REPORT_TIME`），内容为每个节点的在线状态、CPU/内存/磁盘、昨日与今日流量、本账期用量和上限。

## 随时查看当前状态

内容和每日汇总相同，另外多一行网卡实时速率。数据是各节点最近一次上报的值，最多滞后约 20 秒。

**在 Telegram 里**：在配置的那个会话（`TELEGRAM_CHAT_ID`）里发 `/status`。

- 只有这个会话里的消息会被处理，其他会话发来的一律忽略、不回复；只有 `/status` 和 `/help` 两个只读命令。
- hub 启动时会通过 `setMyCommands` 设置输入框旁的命令菜单（Menu），只在这个会话里显示 `/status` 和 `/help`，并清除这个 bot 之前被其他程序注册的全局命令。菜单若仍显示旧命令，重开会话即可刷新。
- hub 停机期间发的命令不会在恢复后补答（超过 2 分钟的消息直接丢弃）。
- 为此 hub 会长轮询 Telegram 的 `getUpdates`。同一个 bot token 只能有一个程序轮询：这个 bot 如果还被别的服务使用或设置了 webhook，命令不会生效（日志里会有 `telegram commands: ...`），推送不受影响。
- 不需要这个功能时，在 hub 的 `/etc/traffic-monitor.env` 里设 `TELEGRAM_COMMANDS=0` 并重启 `traffic-hub`，hub 就只推送、不接收。

**在 hub 上**：

```bash
sudo ./install.sh status          # 在终端打印
sudo ./install.sh status --send   # 把同样的内容立刻推一条到 Telegram
```

```
📡 当前状态 10-02 09:34
在线 2/2

🟢 hk
CPU 3% · 内存 41% · 磁盘 22% · 运行 12天3小时
实时 ↓ 1.2 Mbps · ↑ 8.4 Mbps
昨日 18.20 GB · 今日 6.41 GB
本月 312.50 GB / 2.00 TB（15.6%）
账期 10-01 00:00 → 11-01 00:00

🔴 sg 离线 12分
...
```

## 安装

目标机需要 root、systemd、Python 3.9+（系统自带的 `python3` 版本不够时见下文「老系统上的 Python」）；hub 还需要 `openssl`，并且能访问 `api.telegram.org`。把仓库拷到每台机器上（`git clone` 或 `scp -r`），在仓库目录执行。

配置由 `install.sh` 校验后写入 `/etc/traffic-monitor.env`，不需要手工编辑这个文件；`traffic-monitor.env.example` 只是它的字段说明，不用 `cp`。

**准备 Telegram 参数**（只有 hub 需要）：

- bot token：在 Telegram 里找 @BotFather，`/newbot` 创建机器人后得到。
- chat id：先给机器人发一条消息（发到群里则先把机器人拉进群），再访问 `https://api.telegram.org/bot<token>/getUpdates`，取返回里的 `chat.id`；群的 id 是负数。

**`deploy.local`（可选）**：hub 和 agent 都可以把安装参数写进仓库目录下的 `deploy.local`（已被 git 忽略），就不用在命令行里带，token 也不会出现在 shell 历史里：

```bash
cp deploy.local.example deploy.local && chmod 600 deploy.local
vi deploy.local
```

| 键 | 对应参数 | 说明 |
|---|---|---|
| `NODE_NAME` | `--name` | hub 和 agent 都可用；每台机器必须不同 |
| `MONTHLY_CAP` | `--cap` | 写法同参数：`2T`、`500G`、`unlimited` |
| `BILLING_RESET` | `--reset` | 写法同参数：`27`、`27T08:00` |
| `TELEGRAM_BOT_TOKEN` | `--tg-token` | 仅 hub 读取 |
| `TELEGRAM_CHAT_ID` | `--tg-chat` | 仅 hub 读取 |
| `HUB_URL` | `--hub` | 仅 agent 读取 |
| `AGENT_TOKEN` | `--token` | 仅 agent 读取；hub 上的 token 由安装脚本生成 |
| `HUB_FINGERPRINT` | `--fingerprint` | 仅 agent 读取 |

优先级：命令行参数 > `deploy.local` > `/etc/traffic-monitor.env` 里已有的值。文件里其他的键会被忽略。前三项描述的是本机：把这份文件拷到别的机器时要逐项核对，尤其是 `NODE_NAME`，两台 agent 用了同一个名字，hub 会把它们当成同一个节点。

账期重置时刻和每日汇总时间都按各机器自己的时区计算，安装前用 `timedatectl` 确认时区。

**1. 汇总节点（hub）**

```bash
sudo ./install.sh hub
# 没有 deploy.local 时：
sudo ./install.sh hub --name hk --tg-token 123456:ABC... --tg-chat 987654321 --cap 2T --reset 1
```

hub 自己也是一个被监控的节点，`--cap`、`--reset` 填的是这台机器自己的流量套餐。

安装时会发送一条 Telegram 测试消息，并打印 agent 的安装命令（其中包含共享 token 和 hub 证书指纹）。在 hub 上放行入站 **TCP 8788**（系统防火墙和云厂商安全组都要放行），尽量只允许 agent 的来源 IP 访问。agent 机器不需要开放任何入站端口。

之后想再看一次 token 和指纹，在 hub 上不带参数重新执行 `sudo ./install.sh hub` 即可，token 和证书不会变（会再发一条测试消息并重启服务）。

**2. 其他机器（agent）**：hub 安装完会同时打印一段可以直接粘进 `deploy.local` 的内容。在 agent 上建好 `deploy.local`，填上本机的 `NODE_NAME`、`MONTHLY_CAP`、`BILLING_RESET`，以及 hub 给出的 `HUB_URL`、`AGENT_TOKEN`、`HUB_FINGERPRINT`（这三项每台 agent 都一样），然后：

```bash
sudo ./install.sh agent
```

不用 `deploy.local` 时，复制 hub 打印出的命令，改掉 `--name`、`--hub` 地址和流量参数：

```bash
sudo ./install.sh agent --name sg --hub https://HUB_IP:8788 \
  --token <hub 打印的 token> --fingerprint <hub 打印的指纹> \
  --cap 500G --reset 27T08:00
```

安装时会先上报一次，失败会给出提示。一台机器只能是一种角色：在同一台机器上安装另一种角色，会停掉并移除原来的那个服务。

**3. 检查**

```bash
systemctl status traffic-hub        # agent 上是 traffic-agent
journalctl -u traffic-hub -f        # 日志；上报或推送失败会在这里打印
curl -k https://127.0.0.1:8788/healthz   # 仅 hub，正常返回 {"ok": true}
```

agent 装好后不会有 Telegram 消息，它会出现在下一次每日汇总里；hub 当天安装时如果已经过了汇总时间，第一条汇总在第二天发出。

**升级 / 修改配置**：把新代码拷到机器上，重新执行 `install.sh hub|agent`，只带要改的参数，其余参数沿用 `/etc/traffic-monitor.env` 里的值。

**修改告警阈值等**：`OFFLINE_ALERT_SEC`、`DISK_ALERT_PCT`、`MEM_ALERT_PCT`、`HUB_BIND`、`TELEGRAM_COMMANDS` 没有对应的安装参数，直接编辑 hub 上的 `/etc/traffic-monitor.env`，然后 `sudo systemctl restart traffic-hub`。重新执行 `install.sh` 时这几项会保留；手工加进去的其他变量会被丢掉。

**下线某台机器**：先在那台机器上执行 `sudo ./install.sh uninstall`，再到 hub 上执行 `sudo ./install.sh forget sg`，否则 hub 会一直报它离线。

参数说明：

| 参数 | 说明 |
|---|---|
| `--name` | 节点名，`[a-z][a-z0-9-]{0,31}`；不填则读 `deploy.local` 的 `NODE_NAME` |
| `--cap` | 月流量上限，十进制单位（`2T` = 2×10¹² 字节）；`unlimited` 表示不限量；不填则读 `deploy.local` 的 `MONTHLY_CAP`，都没有则不限量 |
| `--reset` | 账期重置时刻，本机时区：`27` 表示每月 27 日 00:00:00，`27T08:00` 表示 27 日 08:00；短月份自动取月末那天；不填则读 `deploy.local` 的 `BILLING_RESET`，都没有则为 1 日 00:00:00 |
| `--iface` | 统计哪块网卡，默认取默认路由所在的网卡 |
| `--hub` / `--token` / `--fingerprint` | 仅 agent 可用：hub 地址 / 共享 token / hub 证书指纹；不填则读 `deploy.local` |
| `--port` / `--daily` / `--lang` | 仅 hub 可用：监听端口 / 每日汇总时间 / 消息语言 `zh` 或 `en` |
| `--tg-token` / `--tg-chat` | 仅 hub 可用：Telegram bot token / chat id；不填则读 `deploy.local` |

## 老系统上的 Python

需要 Python 3.9 或更新版本，且带 `ssl` 和 `sqlite3` 模块。`install.sh` 会按顺序查找 `python3`、`python3.13` … `python3.9`，以及 `/usr/local/bin` 下的同名文件，用第一个符合要求的，并把它的路径写进 systemd unit。所以只要并排装一个新版本即可，**不需要替换系统自带的 `python3`**。找不到时会列出检查过的解释器和原因。

也可以明确指定：`sudo PYTHON=/usr/local/bin/python3.9 ./install.sh agent`。解释器要装在服务用户 `trafficmon` 能访问的位置（`/usr/local`、`/opt`），不要放在 `/root` 或 `/home` 下。

**CentOS / RHEL 8 系**（自带 3.6）：

```bash
dnf install -y python39
```

**CentOS 7**（自带 3.6，软件源里没有 3.9）：从源码编译，装到 `/usr/local`，不影响系统的 `python3` 和 `yum`。

```bash
yum install -y gcc make openssl-devel bzip2-devel libffi-devel zlib-devel sqlite-devel xz-devel
cd /usr/local/src
curl -fLO https://mirrors.huaweicloud.com/python/3.9.25/Python-3.9.25.tgz   # 或 https://www.python.org/ftp/python/3.9.25/Python-3.9.25.tgz
tar xf Python-3.9.25.tgz && cd Python-3.9.25
./configure --prefix=/usr/local
make -j"$(nproc)"
make altinstall          # altinstall 只装 python3.9，不会覆盖 python3
/usr/local/bin/python3.9 -c 'import ssl, sqlite3; print(ssl.OPENSSL_VERSION)'
```

最后一条能正常打印就说明可用，之后照常执行 `sudo ./install.sh agent`。编译完成后 `/usr/local/src/Python-3.9.25*` 可以删掉。

## 月流量怎么统计

- 每 20 秒读取 `/proc/net/dev` 中指定网卡的 rx/tx 累计字节数，把增量按时间区间写入 `/var/lib/traffic-monitor/traffic.sqlite3`。入站和出站都计入。
- 服务停止但机器没有重启时，内核计数器仍在累加，下次采样会把这段补上；机器重启后从开机时刻重新计数。
- 跨过账期重置时刻或零点的区间，按时间比例拆分到两边，总字节数守恒。
- agent 在本机计算账期用量，hub 宕机期间照常记账。

这是预警用的账本，不能替代云厂商账单：关机或重启前最后一次采样之后的流量（最多约 20 秒）会丢失；hub 在虚机网卡上计量，厂商在宿主机或交换机上计量，口径本来就有差异；账期中途才安装时，安装之前的用量统计不到。

## 安全

- agent 与 hub 之间用 HTTPS 通信。hub 使用自签证书，agent 用 SHA-256 指纹固定校验，指纹不对就不会发出 token。
- 所有 agent 共用一个 `AGENT_TOKEN`，只能用来上报。查询全部节点状态的接口除了 token 还要求请求来自 hub 本机，agent 读不到其他节点的数据。
- Telegram 命令只读，且只响应 `TELEGRAM_CHAT_ID` 这一个会话；群里的任何成员都可以发 `/status`。
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

### 旧版遗留文件

仓库里还留着上一版（Telegram 机器人 + 超量断网）的文件，当前版本不安装、不使用，也已经无法运行：`bot.py`、`cut.py`、`cutctl.py`、`protocol.py`、`deploy.py`、`deploy-remote.sh`、`enroll-agent.py`、`install-host.sh`、`tests/native_nft.py`，以及 `systemd/` 下除 `traffic-hub.service`、`traffic-agent.service` 之外的 unit。安装和升级只用 `install.sh`。
