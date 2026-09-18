#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
    cat >&2 <<'EOF'
usage:
  ./deploy-remote.sh [ssh-target]
  ./deploy-remote.sh --role agent --hub https://HUB:8788 --name hk --cap 2T [ssh-target]

Machine-specific values belong in deploy.local (gitignored) or /etc/traffic-monitor.env
on the target. Copy deploy.local.example to deploy.local.
EOF
}

load_kv_file() {
    local file=$1
    [[ -f $file ]] || return 0
    while IFS= read -r line || [[ -n $line ]]; do
        line=${line%$'\r'}
        [[ -z $line || $line == \#* ]] && continue
        [[ $line == *=* ]] || continue
        local key=${line%%=*}
        local val=${line#*=}
        key=${key//[[:space:]]/}
        case $key in
            SSH_TARGET|HUB_HOST|HUB_URL|FLEET_TOKEN|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|NODE_NAME)
                if [[ -z ${!key:-} ]]; then
                    printf -v "$key" '%s' "$val"
                fi
                ;;
        esac
    done < "$file"
}

ROLE=hub
HUB_URL=
NODE_NAME=
CAP_SPEC=
RESET_DAY=
IFACE=eth0
TARGET=
SSH_TARGET=
HUB_HOST=

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
load_kv_file "$bundle_dir/deploy.local"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --role) ROLE=${2:?}; shift 2 ;;
        --hub) HUB_URL=${2:?}; shift 2 ;;
        --name) NODE_NAME=${2:?}; shift 2 ;;
        --cap) CAP_SPEC=${2:?}; shift 2 ;;
        --reset) RESET_DAY=${2:?}; shift 2 ;;
        --iface) IFACE=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        -*) usage; exit 2 ;;
        *) TARGET=$1; shift ;;
    esac
done

target=${TARGET:-${SSH_TARGET:-}}
token=${TELEGRAM_BOT_TOKEN:-}
chat_id=${TELEGRAM_CHAT_ID:-}
fleet_token=${FLEET_TOKEN:-}
hub_host=${HUB_HOST:-}

if [[ -z "$target" ]]; then
    echo "pass an ssh target, or set SSH_TARGET in deploy.local" >&2
    exit 2
fi
if [[ "$ROLE" == hub && ( -z "$token" || -z "$chat_id" ) ]]; then
    echo "no local Telegram credentials; keeping existing /etc/traffic-monitor.env on target" >&2
fi
if [[ "$ROLE" == agent && -z "$HUB_URL" ]]; then
    echo "agent deploy needs --hub or HUB_URL in deploy.local" >&2
    exit 3
fi
if [[ "$ROLE" == agent && -z "$fleet_token" ]]; then
    if [[ -z "$hub_host" ]]; then
        echo "agent deploy needs FLEET_TOKEN or HUB_HOST in deploy.local" >&2
        exit 3
    fi
    echo "reading FLEET_TOKEN from $hub_host..." >&2
    fleet_token=$(ssh -o BatchMode=yes "$hub_host" \
        "sudo -n grep '^FLEET_TOKEN=' /etc/traffic-monitor.env | cut -d= -f2-" || true)
fi
if [[ "$ROLE" == agent && -z "$fleet_token" ]]; then
    echo "could not get FLEET_TOKEN" >&2
    exit 3
fi

remote_stage=
cleanup_remote_stage() {
    if [[ -n "$remote_stage" && "$remote_stage" =~ ^/tmp/traffic-monitor\.[A-Za-z0-9]+$ ]]; then
        ssh -o BatchMode=yes "$target" \
            "find '$remote_stage' -xdev -depth \\( -type f -o -type l \\) -exec unlink -- {} \\; 2>/dev/null || true; find '$remote_stage' -xdev -depth -type d -exec rmdir -- {} \\; 2>/dev/null || true" \
            >/dev/null 2>&1 || true
    fi
}
trap cleanup_remote_stage EXIT

privilege_mode=$(ssh -o BatchMode=yes "$target" \
    'if [[ $(id -u) -eq 0 ]]; then echo root; elif sudo -n true 2>/dev/null; then echo sudo; else echo unavailable; fi')
if [[ "$privilege_mode" == unavailable ]]; then
    echo "the host needs root login or passwordless sudo" >&2
    exit 4
fi

remote_stage=$(ssh -o BatchMode=yes "$target" 'umask 077; mktemp -d /tmp/traffic-monitor.XXXXXX')
if [[ ! "$remote_stage" =~ ^/tmp/traffic-monitor\.[A-Za-z0-9]+$ ]]; then
    echo "unexpected remote staging path: $remote_stage" >&2
    exit 5
fi

COPYFILE_DISABLE=1 tar --no-xattrs -C "$bundle_dir" \
    --exclude='.git' --exclude='__pycache__' --exclude='deploy.local' \
    --exclude='hub.crt' --exclude='hub.key' \
    -cf - . | ssh -o BatchMode=yes "$target" "tar -xf - -C '$remote_stage'"

if [[ "$ROLE" == agent ]]; then
    if [[ -z "$hub_host" ]]; then
        echo "agent TLS needs HUB_HOST in deploy.local to copy hub.crt" >&2
        exit 3
    fi
    ssh -o BatchMode=yes "$hub_host" "sudo -n cat /var/lib/traffic-monitor/hub.crt" \
        | ssh -o BatchMode=yes "$target" "cat > '$remote_stage/hub.crt'"
    ssh -o BatchMode=yes "$target" "test -s '$remote_stage/hub.crt'"
fi

py_sudo="python3"
install_cmd="bash '$remote_stage/install-host.sh'"
if [[ "$privilege_mode" != root ]]; then
    py_sudo="sudo -n python3"
    install_cmd="sudo -n bash '$remote_stage/install-host.sh'"
fi

envjson=$(
    TELEGRAM_BOT_TOKEN="$token" \
    TELEGRAM_CHAT_ID="$chat_id" \
    FLEET_TOKEN="$fleet_token" \
    NODE_NAME="$NODE_NAME" \
    FLEET_HUB_URL="$HUB_URL" \
    python3 -c 'import json,os; keys=["TELEGRAM_BOT_TOKEN","TELEGRAM_CHAT_ID","FLEET_TOKEN","NODE_NAME","FLEET_HUB_URL"]; print(json.dumps({k: os.environ.get(k,"") or "" for k in keys}))'
)
ssh -o BatchMode=yes "$target" "$py_sudo -c $(printf %q "import json, pathlib
u = json.loads(r'''${envjson}''')
path = pathlib.Path('/etc/traffic-monitor.env')
cur = {}
if path.is_file():
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip() or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        cur[k] = v
for key, value in u.items():
    if value:
        cur[key] = value
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(''.join(f'{k}={cur[k]}\\n' for k in sorted(cur)), encoding='utf-8')
path.chmod(0o600)
")"

ssh -o BatchMode=yes "$target" 'sudo -n chmod 0600 /etc/traffic-monitor.env; sudo -n chown root:root /etc/traffic-monitor.env' || true

install_args=(--role "$ROLE" --iface "$IFACE")
[[ -n "$HUB_URL" ]] && install_args+=(--hub "$HUB_URL")
[[ -n "$NODE_NAME" ]] && install_args+=(--name "$NODE_NAME")
[[ -n "$CAP_SPEC" ]] && install_args+=(--cap "$CAP_SPEC")
[[ -n "$RESET_DAY" ]] && install_args+=(--reset "$RESET_DAY")

ssh -o BatchMode=yes "$target" "$install_cmd ${install_args[*]}"
printf '\nRemote %s install finished on %s\n' "$ROLE" "$target"
