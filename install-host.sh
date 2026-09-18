#!/usr/bin/env bash
set -Eeuo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "run this installer as root" >&2
    exit 1
fi

if [[ ! -d /run/systemd/system ]]; then
    echo "this installer requires systemd" >&2
    exit 2
fi

ROLE=hub
HUB_URL_FLAG=
NODE_NAME_FLAG=
CAP_FLAG=
RESET_FLAG=
IFACE_FLAG=eth0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role) ROLE=${2:?}; shift 2 ;;
        --hub) HUB_URL_FLAG=${2:?}; shift 2 ;;
        --name) NODE_NAME_FLAG=${2:?}; shift 2 ;;
        --cap) CAP_FLAG=${2:?}; shift 2 ;;
        --reset) RESET_FLAG=${2:?}; shift 2 ;;
        --iface) IFACE_FLAG=${2:?}; shift 2 ;;
        -h|--help)
            cat <<'EOF'
usage: install-host.sh --role hub|agent [options]

  --role hub|agent
  --hub URL          agent: fleet hub, e.g. http://HUB_HOST:8788
  --name NAME        node name, e.g. sg
  --cap 2T|500G|unlimited
  --reset 27
  --iface eth0
EOF
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

if [[ "$ROLE" != hub && "$ROLE" != agent ]]; then
    echo "role must be hub or agent" >&2
    exit 2
fi
if [[ "$ROLE" == agent && -z "$HUB_URL_FLAG" && -z "${FLEET_HUB_URL:-}" ]]; then
    echo "agent role needs --hub URL" >&2
    exit 2
fi

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
for required in report.py bot.py hub.py agent.py hostinfo.py util.py snapshot.py formatters.py counters.py \
    systemd/traffic-hub.service systemd/traffic-bot.service systemd/traffic-agent.service \
    systemd/traffic-monitor.service systemd/traffic-monitor.timer; do
    if [[ ! -f "$bundle_dir/$required" ]]; then
        echo "bundle is missing $required" >&2
        exit 4
    fi
done

if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required (stdlib only; no pip, no extra RPMs)" >&2
    exit 6
fi

if ! id trafficmon >/dev/null 2>&1; then
    nologin=/usr/sbin/nologin
    [[ -x "$nologin" ]] || nologin=/sbin/nologin
    useradd -r -M -d /var/lib/traffic-monitor -s "$nologin" trafficmon
fi

export SYSTEMD_PAGER=
export INSTALL_ROLE=$ROLE
export INSTALL_HUB_URL=${HUB_URL_FLAG:-${FLEET_HUB_URL:-}}
export INSTALL_NODE_NAME=$NODE_NAME_FLAG
export INSTALL_CAP=$CAP_FLAG
export INSTALL_RESET=$RESET_FLAG
export INSTALL_IFACE=$IFACE_FLAG

python3 - "$bundle_dir" <<'PY'
import datetime as dt
import json
import os
import pathlib
import secrets
import sys

sys.path.insert(0, sys.argv[1])
import util

bundle = pathlib.Path(sys.argv[1])
role = os.environ["INSTALL_ROLE"]
iface = os.environ.get("INSTALL_IFACE") or "eth0"

opt = pathlib.Path("/opt/traffic-monitor")
opt.mkdir(parents=True, exist_ok=True)
for src in bundle.glob("*.py"):
    dest = opt / src.name
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o644)
opt.chmod(0o755)

unit_dir = pathlib.Path("/etc/systemd/system")
for src in (bundle / "systemd").iterdir():
    if not src.is_file():
        continue
    dest = unit_dir / src.name
    dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    dest.chmod(0o644)

env_path = pathlib.Path("/etc/traffic-monitor.env")
current = {}
if env_path.is_file():
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key] = value

def keep(key, fallback=""):
    return current.get(key, fallback)

node_name = os.environ.get("INSTALL_NODE_NAME") or keep("NODE_NAME") or "node"
node_name = util.normalize_node_name(node_name)
if not util.valid_node_name(node_name):
    node_name = "node"

cap_spec = os.environ.get("INSTALL_CAP") or ""
if cap_spec:
    cap = util.parse_cap(cap_spec)
    cap_bytes = "0" if cap is None else str(cap)
else:
    cap_bytes = keep("MONTHLY_CAP_BYTES", "2000000000000")

reset_day = os.environ.get("INSTALL_RESET") or keep("BILLING_RESET_DAY", "1")
hub_url = os.environ.get("INSTALL_HUB_URL") or keep("FLEET_HUB_URL") or keep("HUB_URL", "http://127.0.0.1:8788")
fleet_token = keep("FLEET_TOKEN")
if role == "hub" and not fleet_token:
    fleet_token = secrets.token_hex(24)

public_url = keep("FLEET_PUBLIC_URL")

merged = {
    "ROLE": role,
    "NODE_NAME": node_name,
    "TRAFFIC_IFACE": iface,
    "BILLING_RESET_DAY": str(reset_day),
    "MONTHLY_CAP_BYTES": cap_bytes,
    "DAILY_REPORT_HOUR_UTC": keep("DAILY_REPORT_HOUR_UTC", "16"),
    "HOST_LABEL": keep("HOST_LABEL"),
    "HUB_BIND": keep("HUB_BIND", "0.0.0.0"),
    "HUB_PORT": keep("HUB_PORT", "8788"),
    "HUB_URL": "http://127.0.0.1:8788" if role == "hub" else hub_url,
    "FLEET_HUB_URL": hub_url if role == "agent" else keep("FLEET_HUB_URL", "http://127.0.0.1:8788"),
    "FLEET_PUBLIC_URL": public_url,
    "FLEET_TOKEN": fleet_token,
    "TELEGRAM_BOT_TOKEN": keep("TELEGRAM_BOT_TOKEN"),
    "TELEGRAM_CHAT_ID": keep("TELEGRAM_CHAT_ID"),
}
if role == "hub" and not merged["TELEGRAM_BOT_TOKEN"]:
    raise SystemExit("hub role needs TELEGRAM_BOT_TOKEN in /etc/traffic-monitor.env")
if role == "agent" and not merged["FLEET_TOKEN"]:
    raise SystemExit("agent role needs FLEET_TOKEN")

lines = [f"{key}={merged[key]}" for key in sorted(merged) if merged[key] != ""]
env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
env_path.chmod(0o600)

state_dir = pathlib.Path("/var/lib/traffic-monitor")
state_dir.mkdir(parents=True, exist_ok=True)
bootstrap = state_dir / "bootstrap.json"
if not bootstrap.exists():
    rx = tx = None
    with open("/proc/net/dev", encoding="utf-8") as fh:
        for line in fh:
            label, _, rest = line.partition(":")
            if label.strip() == iface:
                parts = rest.split()
                rx, tx = int(parts[0]), int(parts[8])
                break
    if rx is None:
        raise SystemExit(f"interface {iface} not found")
    btime = 0
    with open("/proc/stat", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    payload = {
        "iface": iface,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "boot_id": boot_id,
        "boot_time": dt.datetime.fromtimestamp(btime, dt.timezone.utc).isoformat(),
        "rx_bytes": rx,
        "tx_bytes": tx,
    }
    bootstrap.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

print(f"installed role={role} node={node_name}")
PY

chown -R trafficmon:trafficmon /var/lib/traffic-monitor
chmod 0755 /var/lib/traffic-monitor
chmod 0644 /var/lib/traffic-monitor/bootstrap.json 2>/dev/null || true
chmod 0600 /etc/traffic-monitor.env
chown root:root /etc/traffic-monitor.env
python3 -m py_compile /opt/traffic-monitor/*.py

if command -v restorecon >/dev/null 2>&1; then
    restorecon -Rv /opt/traffic-monitor /var/lib/traffic-monitor \
        /etc/systemd/system/traffic-*.service \
        /etc/systemd/system/traffic-*.timer >/dev/null || true
fi

systemctl daemon-reload
for leftover in vnstat.service vnstatd.service; do
    if systemctl list-unit-files --no-pager --no-legend "$leftover" 2>/dev/null | grep -q .; then
        systemctl disable --now "$leftover" >/dev/null 2>&1 || true
    fi
done
rm -f /etc/systemd/system/vnstat.service.d/memory.conf \
      /etc/systemd/system/vnstatd.service.d/memory.conf

if [[ "$ROLE" == hub ]]; then
    systemctl disable --now traffic-agent.service >/dev/null 2>&1 || true
    systemctl enable --now traffic-hub.service traffic-bot.service traffic-monitor.timer
    systemctl restart traffic-hub.service
    sleep 1
    systemctl restart traffic-bot.service
else
    systemctl disable --now traffic-hub.service traffic-bot.service traffic-monitor.timer >/dev/null 2>&1 || true
    systemctl enable --now traffic-agent.service
    systemctl restart traffic-agent.service
fi

echo "role=$ROLE"
if [[ "$ROLE" == hub ]]; then
    systemctl --no-pager --full status traffic-hub.service traffic-bot.service
    echo "Open inbound TCP 8788 on this host so other agents can join."
else
    systemctl --no-pager --full status traffic-agent.service
fi
