#!/usr/bin/env bash
#
# Stage Audio Works Raspberry Pi Video Player - central manager
# Copyright (C) 2026 Stage Audio Works
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# =============================================================================
# Install or update the SAW central manager
# =============================================================================
# On a Raspberry Pi (Raspberry Pi OS) or a Linux machine or VM (Debian,
# Ubuntu; Fedora/RHEL-family on a best-effort basis). Needs systemd and
# Python 3.9+. Safe to run again: it is also the update.
#
#   sudo bash install.sh                     install / update, then (re)start
#   sudo bash install.sh --password          choose a new password (asks; needs a terminal)
#   sudo bash install.sh --new-password      make a random password and print it
#   sudo bash install.sh --password-file F   the password from file F (removed after reading)
#   sudo bash install.sh --no-password       first install only: no password
#   sudo bash install.sh --port 8080         listen on another port (default 5080)
#   sudo bash install.sh --name "Museum"     the name the players show
#   sudo bash install.sh --uninstall         remove the service and code (keeps data)
#   sudo bash install.sh --uninstall --purge ...and the library, registry, settings
#
# The password for the web pages: on a first install at a terminal (the
# Proxmox Shell, SSH, `curl ... | sudo bash` typed by hand) it asks you to choose
# one; press Enter for a random one, shown once. Without a terminal (automation,
# the Update button) it never waits: a first install gets a random one, printed.
# The question goes to the terminal itself, never to stdin, which under
# `curl ... | bash` is the script. Stored hashed, in /var/lib/saw-manager/auth.json;
# change it on the Software page, or with --password. The manager can change
# every screen and install software on the players - so it has a password
# unless you ask for none.
# =============================================================================
# Intentionally NOT using set -e - `(( ))` that evaluates to 0 would end the
# script silently (the player project's constraint 6). Errors are handled.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST=/opt/saw-manager
DATA=/var/lib/saw-manager
CONF_DIR=/etc/saw-manager
ENV_FILE=${CONF_DIR}/manager.env
UNIT=/etc/systemd/system/saw-manager.service
UPD_UNIT=/etc/systemd/system/saw-manager-update.service
UPD_PATH=/etc/systemd/system/saw-manager-update.path
UPD_LIB=/usr/local/lib/saw-manager
UPD_OUT=/var/lib/saw-manager-update
UPD_SOURCE=${CONF_DIR}/update-source
SVC_USER=saw-manager

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'
info()  { echo -e "${GREEN}[saw-manager]${NC} $*"; }
warn()  { echo -e "${YELLOW}[saw-manager]${NC} $*"; }
fail()  { echo -e "${RED}[saw-manager] ERROR:${NC} $*" >&2; exit 1; }

OPT_PORT=""; OPT_NAME=""; OPT_NEWPW=false; OPT_NOPW=false; OPT_UNINSTALL=false; OPT_PURGE=false
OPT_PW=false; OPT_PWFILE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --port)          OPT_PORT="${2:-}"; shift 2 ;;
        --name)          OPT_NAME="${2:-}"; shift 2 ;;
        --password)      OPT_PW=true; shift ;;
        --password-file) OPT_PWFILE="${2:-}"; shift 2 ;;
        --new-password)  OPT_NEWPW=true; shift ;;
        --no-password)   OPT_NOPW=true; shift ;;
        --uninstall)     OPT_UNINSTALL=true; shift ;;
        --purge)         OPT_PURGE=true; shift ;;
        -h|--help)       sed -n '19,46p' "$0"; exit 0 ;;
        *)              fail "unknown option: $1 (see --help)" ;;
    esac
done

[[ $EUID -eq 0 ]] || fail "run as root: sudo bash install.sh"

random_pw() { head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 16; }

# ask_password: at the terminal itself (/dev/tty), twice. Sets NEWPW to the
# chosen password, or to "" when Enter was pressed (the caller makes a random
# one). Returns 1, asking nothing, when there is no terminal - a piped or
# systemd run never waits.
ask_password() {
    local a b
    NEWPW=""
    { exec 3<>/dev/tty; } 2>/dev/null || return 1
    while true; do
        printf 'Choose a password for the manager'"'"'s web pages (8+ characters; Enter for a random one): ' >&3
        IFS= read -r -s a <&3; printf '\n' >&3
        [[ -n "${a}" ]] || break
        if (( ${#a} < 8 )); then printf 'At least 8 characters, please.\n' >&3; continue; fi
        printf 'The same again: ' >&3
        IFS= read -r -s b <&3; printf '\n' >&3
        if [[ "${a}" == "${b}" ]]; then NEWPW="${a}"; break; fi
        printf 'Those were different - once more.\n' >&3
    done
    exec 3>&-
    return 0
}

if [[ -n "${OPT_PORT}" ]] && ! [[ "${OPT_PORT}" =~ ^[0-9]{2,5}$ && "${OPT_PORT}" -ge 1024 && "${OPT_PORT}" -le 65535 ]]; then
    fail "--port takes a number from 1024 to 65535"
fi
if [[ -n "${OPT_NAME}" ]] && ! [[ "${OPT_NAME}" =~ ^[A-Za-z0-9\ ._-]{1,64}$ ]]; then
    fail "--name: up to 64 letters, digits, spaces, dots, dashes or underscores"
fi

# ── Uninstall ─────────────────────────────────────────────────────────────────
if [[ "${OPT_UNINSTALL}" == true ]]; then
    systemctl disable --now saw-manager-update.path saw-manager 2>/dev/null
    rm -f "${UNIT}" "${UPD_UNIT}" "${UPD_PATH}" "${UPD_SOURCE}"
    systemctl daemon-reload 2>/dev/null
    rm -rf "${DEST}" "${UPD_LIB}"
    if [[ "${OPT_PURGE}" == true ]]; then
        rm -rf "${DATA}" "${CONF_DIR}" "${UPD_OUT}"
        userdel "${SVC_USER}" 2>/dev/null
        info "removed, with the library, the registry and the settings."
    else
        info "removed. Kept: the library and registry (${DATA}) and settings (${CONF_DIR})."
        info "Add --purge to remove those too."
    fi
    exit 0
fi

# ── What this machine is ──────────────────────────────────────────────────────
for f in saw_manager.py saw-manager.service templates/layout.html templates/_logo.html templates/index.html \
         templates/unit.html templates/content.html templates/software.html \
         static/manager.css static/manager.js self-update.sh saw-manager-update.service          saw-manager-update.path; do
    [[ -f "${HERE}/${f}" ]] || fail "missing ${HERE}/${f} - run this from a full checkout"
done

if [[ ! -d /run/systemd/system ]]; then
    fail "this machine does not run systemd (a container, or WSL without systemd?).
  The manager runs as a systemd service. On WSL: add [boot] systemd=true to /etc/wsl.conf."
fi

OS_NAME="unknown"
[[ -r /etc/os-release ]] && OS_NAME="$(. /etc/os-release; echo "${PRETTY_NAME:-${NAME:-unknown}}")"
MODEL=""
[[ -r /proc/device-tree/model ]] && MODEL="$(tr -d '\0' < /proc/device-tree/model)"
VIRT="$(systemd-detect-virt 2>/dev/null || true)"
[[ "${VIRT}" == none ]] && VIRT=""
info "this machine: ${OS_NAME}${MODEL:+ - ${MODEL}} ($(uname -m)${VIRT:+, ${VIRT}})"

pkg_install() {
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1 || warn "apt-get update failed - trying anyway"
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q "$@" >/dev/null
    else
        return 1
    fi
}

command -v python3 >/dev/null 2>&1 || { info "installing python3"; pkg_install python3 || fail "could not install python3"; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || fail "needs Python 3.9 or newer (this one: $(python3 --version 2>&1))"
if ! python3 -c 'import flask' 2>/dev/null; then
    info "installing Flask (python3-flask)"
    pkg_install python3-flask \
        || fail "could not install python3-flask with apt or dnf. Install Flask for the system python3
  (your distribution's python3-flask package), then run this again."
    python3 -c 'import flask' 2>/dev/null || fail "python3-flask installed, but python3 cannot import flask"
fi
# Flask 2.0+ (send_file's max_age and Range answers). Debian 11 ships 1.1.
python3 -c 'import importlib.metadata as m, sys; sys.exit(0 if int(m.version("flask").split(".")[0]) >= 2 else 1)' 2>/dev/null \
    || fail "needs Flask 2.0 or newer (this machine has $(python3 -c 'import importlib.metadata as m;print(m.version("flask"))' 2>/dev/null || echo '?')).
  Raspberry Pi OS Bookworm, Debian 12 and Ubuntu 22.04 or newer have it."
info "Python $(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:3])))'), Flask $(python3 -c 'import importlib.metadata as m;print(m.version("flask"))' 2>/dev/null || echo '?')"

# ── The service user ──────────────────────────────────────────────────────────
if ! id "${SVC_USER}" >/dev/null 2>&1; then
    NOLOGIN="$(command -v nologin || echo /usr/sbin/nologin)"
    useradd --system --home-dir "${DATA}" --no-create-home --shell "${NOLOGIN}" "${SVC_USER}" \
        || fail "could not create the system user ${SVC_USER}"
    info "created system user ${SVC_USER}"
fi

# ── Code: built beside the old copy, then swapped - never half-installed ─────
tmp="$(mktemp -d /opt/.saw-manager.XXXXXX)" || fail "mktemp failed"
mkdir -p "${tmp}/templates" "${tmp}/static"
if ! { cp "${HERE}/saw_manager.py" "${tmp}/" \
       && cp "${HERE}"/templates/*.html "${tmp}/templates/" \
       && cp "${HERE}"/static/*.css "${HERE}"/static/*.js "${tmp}/static/"; }; then
    rm -rf "${tmp}"
    fail "copy failed"
fi
chmod 755 "${tmp}"
chmod -R a+rX "${tmp}"
python3 -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" "${tmp}/saw_manager.py" \
    || { rm -rf "${tmp}"; fail "saw_manager.py does not parse - not installed"; }
rm -rf "${DEST}.old"
[[ -d "${DEST}" ]] && mv "${DEST}" "${DEST}.old"
mv "${tmp}" "${DEST}" || fail "could not put ${DEST} in place"
rm -rf "${DEST}.old"
VERSION="$(sed -nE 's/^MANAGER_VERSION *= *([0-9]+).*/\1/p' "${DEST}/saw_manager.py")"
info "code in ${DEST} (manager v${VERSION:-?})"

# ── Settings: made once, kept across updates; changed only by the flags ──────
mkdir -p "${CONF_DIR}"
FIRST=false
[[ -f "${ENV_FILE}" ]] || FIRST=true
setting() { sed -nE "s/^$1=(.*)$/\1/p" "${ENV_FILE}" 2>/dev/null | tail -1; }
PORT="${OPT_PORT:-$(setting SAW_MANAGER_PORT)}"; PORT="${PORT:-5080}"
NAME="${OPT_NAME:-$(setting SAW_MANAGER_NAME)}"
# The password: hashed in ${DATA}/auth.json (see the header). v1/v2 kept it in
# plain text in manager.env; it is moved into auth.json here, unchanged.
AUTH="${DATA}/auth.json"
LEGACY_PW="$(setting SAW_MANAGER_PASSWORD)"
NEWPW=""; SHOW_PW=false; CHOSEN=false; MIGRATING=false
if [[ -n "${OPT_PWFILE}" ]]; then
    [[ -f "${OPT_PWFILE}" ]] || fail "--password-file: ${OPT_PWFILE} not found"
    NEWPW="$(head -n 1 "${OPT_PWFILE}")"; rm -f "${OPT_PWFILE}"; CHOSEN=true
elif [[ "${OPT_PW}" == true ]]; then
    ask_password || fail "--password needs a terminal to type it in. Without one: --new-password
  (a random one, printed), or --password-file FILE (removed after reading)."
    if [[ -n "${NEWPW}" ]]; then CHOSEN=true; else NEWPW="$(random_pw)"; SHOW_PW=true; fi
elif [[ "${OPT_NEWPW}" == true ]]; then
    NEWPW="$(random_pw)"; SHOW_PW=true
elif [[ -s "${AUTH}" ]]; then
    :                                   # set already: kept
elif [[ -n "${LEGACY_PW}" ]]; then
    NEWPW="${LEGACY_PW}"; MIGRATING=true
elif [[ "${FIRST}" == true && "${OPT_NOPW}" == false ]]; then
    if ask_password && [[ -n "${NEWPW}" ]]; then CHOSEN=true; else NEWPW="$(random_pw)"; SHOW_PW=true; fi
fi
KEEP_LEGACY=""
if [[ -n "${NEWPW}" ]]; then
    install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 750 "${DATA}" || fail "could not create ${DATA}"
    if printf '%s\n' "${NEWPW}" | SAW_MANAGER_DATA="${DATA}" python3 "${DEST}/saw_manager.py" --set-password \
       && chown "${SVC_USER}:${SVC_USER}" "${AUTH}" && chmod 600 "${AUTH}"; then
        [[ "${MIGRATING}" == true ]] && info "password moved from ${ENV_FILE} into ${AUTH} (hashed, unchanged)"
    elif [[ "${MIGRATING}" == true ]]; then
        KEEP_LEGACY="${LEGACY_PW}"      # e.g. shorter than 8: keep it working as it was
        warn "the password in ${ENV_FILE} could not be moved (too short?) - it still works;"
        warn "change it on the Software page or with: sudo bash ${HERE}/install.sh --password"
    else
        fail "the password was not set (at least 8 characters, no control characters)"
    fi
fi
SHOWN_PW=""; [[ "${SHOW_PW}" == true ]] && SHOWN_PW="${NEWPW}"   # printed once, at the end
unset NEWPW LEGACY_PW
envtmp="$(mktemp "${CONF_DIR}/.manager.env.XXXXXX")" || fail "mktemp failed"
{
    echo "# SAW manager settings - written by install.sh, kept across updates."
    echo "# Change with: sudo bash install.sh --port N | --name \"...\""
    echo "# The password is not here: it is hashed in ${AUTH}."
    echo "# Change it on the Software page, or: sudo bash install.sh --password"
    echo "SAW_MANAGER_PORT=${PORT}"
    echo "SAW_MANAGER_NAME=${NAME}"
    [[ -z "${KEEP_LEGACY}" ]] || echo "SAW_MANAGER_PASSWORD=${KEEP_LEGACY}"
} > "${envtmp}"
chown "root:${SVC_USER}" "${envtmp}" && chmod 640 "${envtmp}" && mv "${envtmp}" "${ENV_FILE}" \
    || { rm -f "${envtmp}"; fail "could not write ${ENV_FILE}"; }

# ── The service ───────────────────────────────────────────────────────────────
if ! cmp -s "${HERE}/saw-manager.service" "${UNIT}"; then
    cp "${HERE}/saw-manager.service" "${UNIT}" || fail "could not write ${UNIT}"
    info "service file installed"
fi

# ── Updating from the web page ───────────────────────────────────────────────
# A root job the page can only ask for (self-update.sh): the manager writes a
# one-word request, saw-manager-update.path starts the job, which installs
# from the checkout named in update-source. The script is replaced atomically:
# this installer may itself be running under that job, from that script.
mkdir -p "${UPD_LIB}" && chmod 755 "${UPD_LIB}"
updtmp="$(mktemp "${UPD_LIB}/.self-update.XXXXXX")" || fail "mktemp failed"
if ! { cp "${HERE}/self-update.sh" "${updtmp}" && chmod 755 "${updtmp}" \
        && mv -f "${updtmp}" "${UPD_LIB}/self-update.sh"; }; then
    rm -f "${updtmp}"
    fail "could not install ${UPD_LIB}/self-update.sh"
fi
for u in saw-manager-update.service saw-manager-update.path; do
    if ! cmp -s "${HERE}/${u}" "/etc/systemd/system/${u}"; then
        cp "${HERE}/${u}" "/etc/systemd/system/${u}" || fail "could not write /etc/systemd/system/${u}"
    fi
done
# Only a checkout root owns and nobody else can write may be installed from by
# a root job: anything else would let its owner run code as root.
SRC_OWNER="$(stat -c %U "${HERE}" 2>/dev/null)"; SRC_MODE="$(stat -c %a "${HERE}" 2>/dev/null)"
if git -C "${HERE}" -c "safe.directory=${HERE}" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
   && [[ "${SRC_OWNER}" == root ]] && (( (8#${SRC_MODE:-777} & 8#022) == 0 )); then
    BR="$(git -C "${HERE}" -c "safe.directory=${HERE}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
    [[ -n "${BR}" && "${BR}" != HEAD ]] || BR=main
    { echo "checkout=${HERE}"; echo "branch=${BR}"; } > "${UPD_SOURCE}.tmp" \
        && chmod 644 "${UPD_SOURCE}.tmp" && mv -f "${UPD_SOURCE}.tmp" "${UPD_SOURCE}"
    SELF_UPDATE=true
else
    rm -f "${UPD_SOURCE}"
    SELF_UPDATE=false
fi

systemctl daemon-reload
systemctl enable saw-manager >/dev/null 2>&1
# reset-failed + restart: a watcher left failed (by a start limit on an older
# install) is watching nothing until it is restarted. Restarting it does not
# stop an update job in progress - this installer may be running under one.
systemctl reset-failed saw-manager-update.path saw-manager-update.service >/dev/null 2>&1
systemctl enable saw-manager-update.path >/dev/null 2>&1
systemctl restart saw-manager-update.path >/dev/null 2>&1 || warn "could not start saw-manager-update.path - the update button will not work"
systemctl restart saw-manager

up=false
for _ in $(seq 1 20); do
    sleep 0.5
    if systemctl is-active --quiet saw-manager \
       && python3 -c "import socket,sys; s=socket.create_connection(('127.0.0.1',int(sys.argv[1])),1)" "${PORT}" 2>/dev/null; then
        up=true; break
    fi
done
if [[ "${up}" != true ]]; then
    # In a container without nesting, systemd cannot build the service's
    # sandbox (ProtectSystem, PrivateTmp) and the start fails with 226/NAMESPACE.
    if [[ "$(systemctl show -p ExecMainStatus --value saw-manager 2>/dev/null)" == 226 ]] \
       || journalctl -u saw-manager -n 30 --no-pager 2>/dev/null | grep -q "226/NAMESPACE"; then
        VIRT="$(systemd-detect-virt -c 2>/dev/null || true)"
        fail "the service's sandbox could not be set up (226/NAMESPACE)${VIRT:+ - this is a ${VIRT} container}.
  On Proxmox: the container's Options -> Features -> tick Nesting (or on the host:
  pct set <id> --features nesting=1), restart the container, then run this again."
    fi
    fail "the manager did not start on port ${PORT}:
$(journalctl -u saw-manager -n 15 --no-pager 2>/dev/null)"
fi

# ── Done: what to do next ─────────────────────────────────────────────────────
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
IP="${IP:-this-machine}"
JOIN="${IP}"; [[ "${PORT}" == "5080" ]] || JOIN="${IP}:${PORT}"
echo ""
info "${BOLD}running${NC} - dashboard: http://${IP}:${PORT}/"
if [[ "${SHOW_PW}" == true ]]; then
    echo ""
    echo -e "    ${BOLD}Password for the web pages: ${SHOWN_PW}${NC}   (any user name)"
    echo "    Shown this once - it is stored hashed. Change it on the Software page."
    echo ""
elif [[ "${CHOSEN}" == true ]]; then
    info "password: the one you chose (any user name). Change it on the Software page."
elif [[ ! -s "${AUTH}" && -z "${KEEP_LEGACY}" ]]; then
    warn "no password: anyone on the network can control the players from here."
    warn "Set one on the Software page, or with: sudo bash ${HERE}/install.sh --password"
fi
[[ -s "${AUTH}" ]] && info "forgot the password? sudo bash ${HERE}/install.sh --password"
info "on each player (v23 or later): web UI -> Central Manager -> enter ${JOIN} -> Join"
if [[ "${SELF_UPDATE}" == true ]]; then
    info "updates: Software page -> This manager -> Check for updates (or this install line again)"
else
    info "updates: run the install line again (installed from a copy, so the Update button is off)"
fi

# Firewalls: say what to open, never change one unasked.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "^Status: active"; then
    ufw status 2>/dev/null | grep -qE "^${PORT}(/tcp)? .*ALLOW" \
        || warn "ufw is active and does not allow port ${PORT}: sudo ufw allow ${PORT}/tcp"
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --list-ports 2>/dev/null | grep -qw "${PORT}/tcp" \
        || warn "firewalld is running and does not allow port ${PORT}: sudo firewall-cmd --permanent --add-port=${PORT}/tcp && sudo firewall-cmd --reload"
fi
exit 0
