#!/usr/bin/env bash
# gopro-nas-sync installer for Raspberry Pi OS (Bookworm or later).
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/gopro-nas-sync/main/install.sh | sudo bash
#   sudo ./install.sh                  # from a local checkout
#   sudo ./install.sh --reconfigure    # run the setup wizard again
#   sudo ./install.sh --uninstall [--purge]
#
# Environment: GOPRO_SYNC_REPO (git URL), GOPRO_SYNC_REF (branch / tag), NONINTERACTIVE=1
set -euo pipefail

REPO_URL="${GOPRO_SYNC_REPO:-https://github.com/OWNER/gopro-nas-sync.git}"
REPO_REF="${GOPRO_SYNC_REF:-main}"
PREFIX=/opt/gopro-sync
CONF_DIR=/etc/gopro-sync
STATE_DIR=/var/lib/gopro-sync
SERVICE_USER=gopro-sync
UNIT=/etc/systemd/system/gopro-sync.service
POLKIT_RULE=/etc/polkit-1/rules.d/50-gopro-sync.rules
SUDOERS=/etc/sudoers.d/gopro-sync
WIFI_IF=wlan0
LAN_IF=eth0

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

ask() { # ask <prompt> <default> -> answer on stdout
    local answer=""
    if [[ "${NONINTERACTIVE:-0}" == 1 || ! -r /dev/tty ]]; then echo "$2"; return; fi
    read -r -p "$1 [$2]: " answer </dev/tty || true
    echo "${answer:-$2}"
}

ask_secret() {
    local answer=""
    if [[ "${NONINTERACTIVE:-0}" == 1 || ! -r /dev/tty ]]; then echo ""; return; fi
    read -r -s -p "$1: " answer </dev/tty || true
    echo >&2
    echo "$answer"
}

MODE=install PURGE=0 RECONFIGURE=0
for arg in "$@"; do
    case "$arg" in
        --uninstall) MODE=uninstall ;;
        --purge) PURGE=1 ;;
        --reconfigure) RECONFIGURE=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) die "Unknown option $arg" ;;
    esac
done

[[ $EUID -eq 0 ]] || die "Please run as root (sudo)."

uninstall() {
    info "Stopping service"
    systemctl disable --now gopro-sync.service 2>/dev/null || true
    if [[ -x $PREFIX/bin/gopro-sync-mount ]]; then
        info "Removing NAS mount unit"
        "$PREFIX/bin/gopro-sync-mount" remove >/dev/null || true
    fi
    nmcli connection delete gopro-sync-camera >/dev/null 2>&1 || true
    rm -f "$UNIT" "$POLKIT_RULE" "$SUDOERS"
    systemctl daemon-reload
    rm -rf "$PREFIX"
    if [[ $PURGE == 1 ]]; then
        info "Removing configuration, state and service user"
        rm -rf "$CONF_DIR" "$STATE_DIR" /etc/gopro-sync-mount
        userdel "$SERVICE_USER" 2>/dev/null || true
    else
        info "Kept $CONF_DIR and $STATE_DIR (use --purge to remove them)"
    fi
    info "Uninstalled. Files on your NAS were not touched."
}

if [[ $MODE == uninstall ]]; then uninstall; exit 0; fi

# ---------------------------------------------------------------------------------------------
info "Checking system"
. /etc/os-release
case "${VERSION_CODENAME:-}" in
    bookworm|trixie) ;;
    *) warn "Tested on Raspberry Pi OS Bookworm / Trixie; detected '${PRETTY_NAME:-unknown}'." ;;
esac
command -v systemctl >/dev/null || die "systemd is required."

info "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
APT_LOCK=(-o DPkg::Lock::Timeout=600)
apt-get "${APT_LOCK[@]}" update -qq
apt-get "${APT_LOCK[@]}" install -y -qq bluez network-manager cifs-utils nfs-common python3 curl git ca-certificates iproute2 rfkill >/dev/null

systemctl is-active --quiet NetworkManager || die "NetworkManager is not running. Enable it with: sudo raspi-config (Advanced > Network Config > NetworkManager)."
systemctl enable --now bluetooth >/dev/null 2>&1 || warn "Could not start the bluetooth service."
rfkill unblock wifi || true
rfkill unblock bluetooth || true

nmcli -t -f DEVICE,TYPE device | grep -q "^${WIFI_IF}:wifi$" || die "Wi-Fi interface ${WIFI_IF} not found."
if ! nmcli -t -f DEVICE,STATE device | grep -q "^${LAN_IF}:connected"; then
    warn "${LAN_IF} is not connected. The NAS must be reachable over Ethernet."
fi
WIFI_CONN=$(nmcli -t -f GENERAL.CONNECTION device show "$WIFI_IF" | cut -d: -f2-)
if [[ -n "$WIFI_CONN" && "$WIFI_CONN" != "--" && "$WIFI_CONN" != "gopro-sync-camera" ]]; then
    warn "${WIFI_IF} is connected to '${WIFI_CONN}'. The Wi-Fi chip is used exclusively for the camera;"
    warn "that connection will drop during every sync. Consider: sudo nmcli connection modify '${WIFI_CONN}' connection.autoconnect no"
fi
if command -v iw >/dev/null && iw reg get 2>/dev/null | grep -q "country 00"; then
    warn "Wi-Fi country is not set; 5 GHz camera access points may not be visible. Set it with: sudo raspi-config nonint do_wifi_country DE"
fi

# ---------------------------------------------------------------------------------------------
info "Creating service user and directories"
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$STATE_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi
for group in bluetooth netdev; do
    getent group "$group" >/dev/null || groupadd --system "$group"
done
usermod -a -G bluetooth,netdev "$SERVICE_USER"
install -d -m 0755 -o root -g root "$PREFIX" "$PREFIX/bin"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_USER" "$CONF_DIR" "$STATE_DIR"

info "Fetching gopro-nas-sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/packaging" ]]; then
    rm -rf "$PREFIX/src"
    mkdir -p "$PREFIX/src"
    tar -C "$SCRIPT_DIR" --exclude=.venv --exclude=.git -cf - . | tar -C "$PREFIX/src" --no-same-owner -xf -
    chown -R root:root "$PREFIX/src"
elif [[ -d "$PREFIX/src/.git" ]]; then
    git -C "$PREFIX/src" fetch --depth 1 origin "$REPO_REF"
    git -C "$PREFIX/src" reset --hard FETCH_HEAD
else
    rm -rf "$PREFIX/src"
    git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$PREFIX/src"
fi
SRC="$PREFIX/src"

info "Installing uv and Python environment"
if [[ ! -x $PREFIX/bin/uv ]]; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$PREFIX/bin" UV_NO_MODIFY_PATH=1 sh >/dev/null
fi
export UV_PYTHON_INSTALL_DIR="$PREFIX/python" UV_CACHE_DIR="$PREFIX/.uv-cache"
"$PREFIX/bin/uv" venv --quiet --allow-existing --python ">=3.11,<3.14" "$PREFIX/venv"
"$PREFIX/bin/uv" pip install --quiet --python "$PREFIX/venv/bin/python" --reinstall-package gopro-nas-sync "$SRC"

info "Installing mount helper, polkit rule, sudoers entry and service"
install -m 0755 -o root -g root "$SRC/packaging/gopro-sync-mount" "$PREFIX/bin/gopro-sync-mount"
install -m 0644 -o root -g root "$SRC/packaging/50-gopro-sync.rules" "$POLKIT_RULE"
install -m 0440 -o root -g root "$SRC/packaging/sudoers-gopro-sync" "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null || { rm -f "$SUDOERS.tmp"; die "sudoers validation failed"; }
mv "$SUDOERS.tmp" "$SUDOERS"
install -m 0644 -o root -g root "$SRC/packaging/gopro-sync.service" "$UNIT"
ln -sf "$PREFIX/venv/bin/gopro-sync" /usr/local/bin/gopro-sync
systemctl daemon-reload
systemctl reload polkit 2>/dev/null || systemctl restart polkit 2>/dev/null || true

# ---------------------------------------------------------------------------------------------
CONFIG="$CONF_DIR/config.toml"
NAS_JSON=""
if [[ ! -f "$CONFIG" || $RECONFIGURE == 1 ]]; then
    info "Setup (press Enter to accept defaults; everything can be changed later in the web UI)"
    PORT=$(ask "Web UI / API port" 8080)
    TOKEN=""
    if [[ "$(ask "Protect the API with a token? (y/n)" n)" =~ ^[Yy] ]]; then
        TOKEN=$("$PREFIX/venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(24))')
    fi
    NAS_TYPE=$(ask "NAS type: smb, nfs, path (existing directory) or skip" skip)
    export NAS_TYPE
    case "$NAS_TYPE" in
        smb|nfs)
            export NAS_SERVER NAS_SHARE NAS_VERSION NAS_USER NAS_PASSWORD
            NAS_SERVER=$(ask "NAS server (hostname or IP)" "")
            if [[ $NAS_TYPE == smb ]]; then
                NAS_SHARE=$(ask "Share name (optionally share/sub/dir)" "")
                NAS_VERSION=$(ask "SMB version (empty = auto)" "")
                NAS_USER=$(ask "SMB username" "")
                NAS_PASSWORD=$(ask_secret "SMB password")
            else
                NAS_SHARE=$(ask "NFS export path" "")
                NAS_VERSION=$(ask "NFS version (empty = auto)" "")
                NAS_USER="" NAS_PASSWORD=""
            fi
            NAS_JSON=$("$PREFIX/venv/bin/python" -c '
import json, os
e = os.environ
print(json.dumps({"type": e["NAS_TYPE"], "server": e["NAS_SERVER"], "share": e["NAS_SHARE"],
                  "version": e["NAS_VERSION"], "username": e["NAS_USER"], "password": e["NAS_PASSWORD"] or None}))')
            ;;
        path)
            export NAS_PATH
            NAS_PATH=$(ask "Existing directory (already mounted)" /mnt/nas)
            ;;
    esac
    export PORT TOKEN CONFIG
    "$PREFIX/venv/bin/python" - <<'PY'
import os
from pathlib import Path
from gopro_sync.config import Config, ConfigStore, NasConfig, NasType
store = ConfigStore(Path(os.environ["CONFIG"]))
cfg = store.config if store.path.exists() else Config()
cfg.server.port = int(os.environ["PORT"])
cfg.server.api_token = os.environ.get("TOKEN", "")
if os.environ.get("NAS_TYPE") == "path":
    cfg.nas = NasConfig(type=NasType.PATH, path=os.environ["NAS_PATH"])
store.save(cfg)
PY
    chown "$SERVICE_USER:$SERVICE_USER" "$CONFIG"
    chmod 0640 "$CONFIG"
fi

info "Starting service"
systemctl enable gopro-sync.service >/dev/null
systemctl restart gopro-sync.service

PORT=$("$PREFIX/venv/bin/python" -c "
from pathlib import Path; from gopro_sync.config import ConfigStore
print(ConfigStore(Path('$CONFIG')).config.server.port)")
TOKEN=$("$PREFIX/venv/bin/python" -c "
from pathlib import Path; from gopro_sync.config import ConfigStore
print(ConfigStore(Path('$CONFIG')).config.server.api_token)")

for _ in $(seq 1 30); do
    curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null && break
    sleep 1
done
curl -fs "http://127.0.0.1:$PORT/api/status" >/dev/null || die "Service did not start. See: journalctl -u gopro-sync -e"

if [[ -n "$NAS_JSON" ]]; then
    info "Configuring and mounting the NAS share"
    AUTH=()
    [[ -n "$TOKEN" ]] && AUTH=(-H "Authorization: Bearer $TOKEN")
    if ! curl -fsS -X PUT "${AUTH[@]}" -H "Content-Type: application/json" -d "$NAS_JSON" \
        "http://127.0.0.1:$PORT/api/nas" >/dev/null; then
        warn "NAS setup failed; open the web UI (NAS tab) to fix the settings."
    fi
fi

IP=$(ip -4 -o addr show "$LAN_IF" 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
echo
info "gopro-nas-sync is running: http://${IP:-<pi-address>}:$PORT/"
[[ -n "$TOKEN" ]] && info "API token: $TOKEN  (also stored in $CONFIG)"
echo "    Pair a camera in the web UI (Cameras tab), then trigger syncs with:"
echo "    curl -X POST ${TOKEN:+-H 'Authorization: Bearer $TOKEN' }http://${IP:-<pi-address>}:$PORT/api/sync"
