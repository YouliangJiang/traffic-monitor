#!/usr/bin/env bash
# Install or upgrade traffic-monitor on this host. Run from the repository directory.
set -Eeuo pipefail

usage() {
    cat <<'EOF'
usage:
  sudo ./install.sh hub   --name NAME --tg-token TOKEN --tg-chat CHAT_ID [options]
  sudo ./install.sh agent --name NAME --hub https://HUB_HOST:8788 --token TOKEN --fingerprint HEX [options]
  sudo ./install.sh forget NAME     # hub: drop a decommissioned node
  sudo ./install.sh uninstall       # remove units and code; keeps /etc/traffic-monitor.env and state

options (re-running without them keeps the current values):
  --cap 2T|500G|unlimited   monthly traffic cap, decimal units (2T = 2*10^12 bytes)
  --reset 27|27T08:00       billing reset day[Ttime], host local time
  --iface IFACE             accounting NIC (default: default-route interface)
hub only:
  --port 8788  --daily 09:00  --lang zh|en
EOF
}

APP=/opt/traffic-monitor
STATE=/var/lib/traffic-monitor
ENV_FILE=/etc/traffic-monitor.env
UNITS=/etc/systemd/system
SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
FILES="agent.py counters.py formatters.py hostinfo.py hub.py i18n.py report.py snapshot.py tlsutil.py util.py"

[[ $# -gt 0 ]] || { usage; exit 2; }
ROLE=$1; shift
case "$ROLE" in
    -h|--help) usage; exit 0 ;;
    hub|agent|forget|uninstall) ;;
    *) usage; exit 2 ;;
esac
[[ ${EUID} -eq 0 ]] || { echo "run as root" >&2; exit 1; }
[[ -d /run/systemd/system ]] || { echo "systemd is required" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

# Run a command as the service user with the service environment loaded.
as_service() {
    ( set -a; . "$ENV_FILE"; set +a; export TRAFFIC_MONITOR_STATE_DIR=$STATE; cd "$APP"; runuser -u trafficmon -- "$@" )
}

# Earlier releases shipped a Telegram bot, a daily timer and a root nftables cutoff helper.
remove_legacy() {
    local unit
    for unit in traffic-bot.service traffic-monitor.timer traffic-monitor.service traffic-cut.path \
        traffic-cut.timer traffic-cut.service traffic-cut-reconcile.service; do
        if [[ -e "$UNITS/$unit" ]]; then
            systemctl disable --now "$unit" >/dev/null 2>&1 || true
            rm -f "$UNITS/$unit"
        fi
    done
    if command -v nft >/dev/null && nft list table inet trafficmon-cut >/dev/null 2>&1; then
        nft delete table inet trafficmon-cut
        echo "removed legacy cutoff firewall table inet trafficmon-cut"
    fi
    [[ -L "$APP" ]] && rm -f "$APP"
    return 0
}

if [[ "$ROLE" == uninstall ]]; then
    systemctl disable --now traffic-hub.service traffic-agent.service >/dev/null 2>&1 || true
    rm -f "$UNITS/traffic-hub.service" "$UNITS/traffic-agent.service"
    remove_legacy
    rm -rf "$APP"
    systemctl daemon-reload
    echo "uninstalled; $ENV_FILE and $STATE were kept"
    exit 0
fi

if [[ "$ROLE" == forget ]]; then
    [[ $# -eq 1 ]] || { usage; exit 2; }
    systemctl stop traffic-hub.service
    as_service python3 "$APP/hub.py" --forget "$1"
    systemctl start traffic-hub.service
    exit 0
fi

export INSTALL_ROLE=$ROLE
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) export INSTALL_NAME=${2:?}; shift 2 ;;
        --cap) export INSTALL_CAP=${2:?}; shift 2 ;;
        --reset) export INSTALL_RESET=${2:?}; shift 2 ;;
        --iface) export INSTALL_IFACE=${2:?}; shift 2 ;;
        --tg-token) export INSTALL_TG_TOKEN=${2:?}; shift 2 ;;
        --tg-chat) export INSTALL_TG_CHAT=${2:?}; shift 2 ;;
        --port) export INSTALL_PORT=${2:?}; shift 2 ;;
        --daily) export INSTALL_DAILY=${2:?}; shift 2 ;;
        --lang) export INSTALL_LANG=${2:?}; shift 2 ;;
        --hub) export INSTALL_HUB=${2:?}; shift 2 ;;
        --token) export INSTALL_TOKEN=${2:?}; shift 2 ;;
        --fingerprint) export INSTALL_FINGERPRINT=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done
if [[ "$ROLE" == hub ]]; then
    command -v openssl >/dev/null || { echo "openssl is required on the hub" >&2; exit 1; }
fi

# Validate the options and write the environment file before touching anything else.
python3 - "$SRC" "$ENV_FILE" <<'PY'
import os, secrets, sys, tempfile
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import util

env_path = Path(sys.argv[2])
current = {}
if env_path.is_file():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
flag = lambda name: os.environ.get("INSTALL_" + name, "")
role = flag("ROLE")


def default_iface():
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) > 1 and fields[1] == "00000000":
            return fields[0]
    return "eth0"


def pick(key, name, default=""):
    value = flag(name) or current.get(key) or default
    if not value:
        raise SystemExit(f"missing --{name.lower().replace('_', '-')}")
    return value


config = {"ROLE": role}
config["NODE_NAME"] = util.normalize_node_name(pick("NODE_NAME", "NAME"))
if not util.valid_node_name(config["NODE_NAME"]):
    raise SystemExit("--name must match [a-z][a-z0-9-]{0,31}")
config["TRAFFIC_IFACE"] = pick("TRAFFIC_IFACE", "IFACE", default_iface())
if not Path("/sys/class/net", config["TRAFFIC_IFACE"]).exists():
    raise SystemExit(f"interface {config['TRAFFIC_IFACE']} not found")
if flag("CAP"):
    config["MONTHLY_CAP_BYTES"] = str(util.parse_cap(flag("CAP")) or 0)
else:
    config["MONTHLY_CAP_BYTES"] = current.get("MONTHLY_CAP_BYTES", "0")
if flag("RESET"):
    day, clock = util.parse_reset(flag("RESET"))
    config["BILLING_RESET_DAY"], config["BILLING_RESET_TIME"] = str(day), clock
else:
    config["BILLING_RESET_DAY"] = current.get("BILLING_RESET_DAY", "1")
    config["BILLING_RESET_TIME"] = current.get("BILLING_RESET_TIME", "00:00:00")
if role == "hub":
    config["AGENT_TOKEN"] = current.get("AGENT_TOKEN") or secrets.token_urlsafe(32)
    config["TELEGRAM_BOT_TOKEN"] = pick("TELEGRAM_BOT_TOKEN", "TG_TOKEN")
    config["TELEGRAM_CHAT_ID"] = pick("TELEGRAM_CHAT_ID", "TG_CHAT")
    config["HUB_BIND"] = current.get("HUB_BIND", "0.0.0.0")
    config["HUB_PORT"] = str(int(pick("HUB_PORT", "PORT", "8788")))
    config["DAILY_REPORT_TIME"] = util.parse_reset_time(pick("DAILY_REPORT_TIME", "DAILY", "09:00"))[:5]
    for key, default in (("OFFLINE_ALERT_SEC", "300"), ("DISK_ALERT_PCT", "90"), ("MEM_ALERT_PCT", "90")):
        config[key] = str(int(current.get(key, default)))
    config["UI_LANG"] = pick("UI_LANG", "LANG", "zh")
    if config["UI_LANG"] not in ("zh", "en"):
        raise SystemExit("--lang must be zh or en")
else:
    config["HUB_URL"] = pick("HUB_URL", "HUB").rstrip("/")
    if not config["HUB_URL"].startswith("https://"):
        raise SystemExit("--hub must start with https://")
    config["AGENT_TOKEN"] = pick("AGENT_TOKEN", "TOKEN")
    config["HUB_FINGERPRINT"] = pick("HUB_FINGERPRINT", "FINGERPRINT").replace(":", "").lower()
    if len(config["HUB_FINGERPRINT"]) != 64:
        raise SystemExit("--fingerprint must be the 64-hex SHA-256 printed by the hub install")

fd, tmp = tempfile.mkstemp(prefix=".traffic-monitor-env-", dir=env_path.parent)
with os.fdopen(fd, "w") as out:
    out.write("".join(f"{key}={value}\n" for key, value in config.items()))
os.replace(tmp, env_path)
env_path.chmod(0o600)
PY

remove_legacy
id trafficmon >/dev/null 2>&1 || useradd -r -M -d "$STATE" -s "$(command -v nologin || echo /sbin/nologin)" trafficmon
install -d -m 0755 "$APP" "$APP/locales"
for file in $FILES; do install -m 0644 "$SRC/$file" "$APP/$file"; done
install -m 0644 "$SRC"/locales/*.json "$APP/locales/"
install -d -o trafficmon -g trafficmon -m 0750 "$STATE"
python3 -m py_compile "$APP"/*.py

OTHER=$([[ "$ROLE" == hub ]] && echo agent || echo hub)
systemctl disable --now "traffic-$OTHER.service" >/dev/null 2>&1 || true
rm -f "$UNITS/traffic-$OTHER.service"
install -m 0644 "$SRC/systemd/traffic-$ROLE.service" "$UNITS/"
systemctl daemon-reload

if [[ "$ROLE" == hub ]]; then
    FINGERPRINT=$(as_service python3 "$APP/tlsutil.py")
    as_service python3 "$APP/hub.py" --test \
        || echo "WARNING: Telegram test message failed; check --tg-token/--tg-chat and outbound access to api.telegram.org" >&2
else
    as_service python3 "$APP/agent.py" --check \
        || echo "WARNING: could not report to the hub; check --hub, --token, --fingerprint and that the hub port is reachable" >&2
fi
systemctl enable traffic-$ROLE.service >/dev/null
systemctl restart traffic-$ROLE.service
sleep 2
systemctl --no-pager status traffic-$ROLE.service | head -n 5

if [[ "$ROLE" == hub ]]; then
    set -a; . "$ENV_FILE"; set +a
    cat <<EOF

Hub installed. Open inbound TCP ${HUB_PORT} on this host, then on each other machine run:

  sudo ./install.sh agent --name NAME --hub https://THIS_HOST_ADDRESS:${HUB_PORT} \\
    --token ${AGENT_TOKEN} \\
    --fingerprint ${FINGERPRINT} \\
    --cap 2T --reset 1

EOF
fi
