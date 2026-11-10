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
# Pi Video Player Manager as a Proxmox VE container (LXC)
# =============================================================================
# Run ON the Proxmox host, as root (the host's Shell in the web UI will do).
# Creates an unprivileged Debian container and installs the manager in it - or,
# if a Pi Video Player Manager container already exists, updates it. The same link both times:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/proxmox-lxc.sh)
#
#   IP=192.0.2.80/24 GW=192.0.2.1 bash <(curl ...)     a fixed address (recommended:
#                                                    the players are told this address)
#   VLAN=20 bash <(curl ...)                         on a VLAN of the bridge
#
# Environment (all optional):
#   CTID            container id                     (next free; an existing SAW
#                                                     manager container is found by its tag)
#   HN              hostname                         (saw-manager)
#   BRIDGE, VLAN    network bridge, VLAN tag         (vmbr0, none)
#   IP, GW          dhcp, or a.b.c.d/nn + gateway    (dhcp)
#   STORAGE         storage for the container's disk (the first that can hold one)
#   TEMPLATE_STORE  storage for the Debian template  (the first that can hold one)
#   DISK            disk size in GB - the clip library lives here   (32)
#   CORES, RAM      CPU cores, memory in MB          (1, 512)
#   DEBIAN_VERSION  12 or 13                         (12)
#   PORT, NAME      the manager's port and name      (5080, the hostname)
#   NO_PASSWORD=1   first install only: no password on the web pages
#   NEW_PASSWORD=1  on an existing container: choose a new password
#
# A new container: it asks you here, on the host, for the password of the web
# pages (Enter for a random one, printed at the end). The password goes into the
# container as a file (pct push, mode 600) that the installer reads and
# deletes - never on a command line.
#
# The library is inside the container, so Proxmox backups (vzdump) include it.
# Exit status: 0 done; 1 refused or failed (the message says why).
# =============================================================================
# Intentionally NOT using set -e: every step is checked (see install.sh).
set -uo pipefail

DIST_RAW="${SAW_DIST_RAW:-https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main}"
TAG=saw-manager

CTID="${CTID:-}"; HN="${HN:-saw-manager}"; BRIDGE="${BRIDGE:-vmbr0}"; VLAN="${VLAN:-}"
IP="${IP:-dhcp}"; GW="${GW:-}"; STORAGE="${STORAGE:-}"; TEMPLATE_STORE="${TEMPLATE_STORE:-}"
DISK="${DISK:-32}"; CORES="${CORES:-1}"; RAM="${RAM:-512}"; DEBIAN_VERSION="${DEBIAN_VERSION:-12}"
PORT="${PORT:-}"; NAME="${NAME:-}"; NO_PASSWORD="${NO_PASSWORD:-}"; NEW_PASSWORD="${NEW_PASSWORD:-}"
NET_WAIT="${SAW_NET_WAIT_SECONDS:-90}"
PW_FILE=""; CT_PW_FILE=/root/.saw-manager-password
trap '[[ -n "${PW_FILE}" ]] && rm -f "${PW_FILE}"' EXIT

die()  { echo "ERROR: $*" >&2; exit 1; }
say()  { echo "==> $*"; }

# Commands run inside the container get a plain, always-present locale: the
# host shell's (en_US.UTF-8, say) is not generated in the Debian template, and
# apt and perl then complain on every line.
ctx() { local ct="$1"; shift; pct exec "${ct}" -- env LANG=C.UTF-8 LC_ALL=C.UTF-8 "$@"; }

# ask_password: here, at the host's terminal, twice -> PW (empty: Enter, a random
# one). Returns 1, asking nothing, when there is no terminal.
ask_password() {
    local a b
    PW=""
    { exec 3<>/dev/tty; } 2>/dev/null || return 1
    while true; do
        printf 'Choose a password for the manager'"'"'s web pages (8+ characters; Enter for a random one): ' >&3
        IFS= read -r -s a <&3; printf '\n' >&3
        [[ -n "${a}" ]] || break
        if (( ${#a} < 8 )); then printf 'At least 8 characters, please.\n' >&3; continue; fi
        printf 'The same again: ' >&3
        IFS= read -r -s b <&3; printf '\n' >&3
        if [[ "${a}" == "${b}" ]]; then PW="${a}"; break; fi
        printf 'Those were different - once more.\n' >&3
    done
    exec 3>&-
    return 0
}

# The first active storage that can hold CONTENT (rootdir, vztmpl).
first_storage() { { pvesm status --content "$1" 2>/dev/null || true; } | awk 'NR>1 && $3=="active"{print $1; exit}'; }

# The id of a container already carrying the manager (by its tag), or nothing.
find_manager_ct() {
    local id _
    while read -r id _; do
        [[ -n "${id}" ]] || continue
        if pct config "${id}" 2>/dev/null | grep -Eq "^tags: (.*[;,])?${TAG}([;,]|\$)"; then
            echo "${id}"; return 0
        fi
    done < <(pct list 2>/dev/null | tail -n +2)
    return 0
}

wait_net() {
    local _ addr gw
    say "Waiting for the container's network (up to ${NET_WAIT} s)"
    for _ in $(seq 1 "${NET_WAIT}"); do
        pct exec "$1" -- getent hosts raw.githubusercontent.com >/dev/null 2>&1 && return 0
        sleep 1
    done
    # Which step fails, so the message says what to fix.
    addr="$(pct exec "$1" -- ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | head -1)"
    gw="$(pct exec "$1" -- ip -4 route show default 2>/dev/null | awk '{print $3}' | head -1)"
    if [[ -z "${addr}" ]]; then
        die "container $1 got no IPv4 address after ${NET_WAIT} s. With DHCP: is there a DHCP server on bridge ${BRIDGE}${VLAN:+, VLAN ${VLAN}}? Or re-run with IP=a.b.c.d/nn GW=a.b.c.d."
    elif [[ -n "${gw}" ]] && ! pct exec "$1" -- ping -c 2 -W 2 "${gw}" >/dev/null 2>&1; then
        die "container $1 has ${addr} but cannot reach its gateway ${gw}: wrong bridge or VLAN (now ${BRIDGE}${VLAN:+, VLAN ${VLAN}}), or a wrong GW.
  Compare with the host: ip -br addr; ip route. Fix with: pct set $1 --net0 name=eth0,bridge=...,ip=...,gw=...[,tag=N]
  then run this again - it carries on in the same container."
    else
        die "container $1 reaches its gateway but cannot look up names (DNS). Set one: pct set $1 --nameserver <dns-ip>, then run this again."
    fi
}

# The newest Debian VERSION template for ARCH, downloaded if missing -> its volume id.
ensure_template() {
    local deb
    pveam update >/dev/null 2>&1 || true
    deb="$(pveam available --section system 2>/dev/null \
           | awk -v v="${DEBIAN_VERSION}" -v a="$1" '$2 ~ ("^debian-" v "-standard_.*_" a "[.]tar") {print $2}' \
           | sort -V | tail -1)"
    [[ -n "${deb}" ]] || die "no Debian ${DEBIAN_VERSION} template for '$1' in 'pveam available' - is the host online?"
    if ! pveam list "${TEMPLATE_STORE}" 2>/dev/null | grep -q "${deb}"; then
        say "Downloading ${deb} to ${TEMPLATE_STORE}" >&2      # stdout is the volume id
        pveam download "${TEMPLATE_STORE}" "${deb}" >&2 || die "could not download ${deb}"
    fi
    echo "${TEMPLATE_STORE}:vztmpl/${deb}"
}

# ── Checks ────────────────────────────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] || die "run as root on the Proxmox host"
command -v pct >/dev/null 2>&1 || die "pct not found: run this on a Proxmox VE host (its Shell), not inside a container"

[[ "${DISK}" =~ ^[0-9]+$ && "${DISK}" -ge 4 ]] || die "DISK is in GB, at least 4 (got '${DISK}')"
[[ "${CORES}" =~ ^[0-9]+$ && "${CORES}" -ge 1 ]] || die "CORES must be a number (got '${CORES}')"
[[ "${RAM}" =~ ^[0-9]+$ && "${RAM}" -ge 256 ]] || die "RAM is in MB, at least 256 (got '${RAM}')"
case "${DEBIAN_VERSION}" in 12|13) ;; *) die "DEBIAN_VERSION must be 12 or 13" ;; esac
[[ -z "${VLAN}" || ( "${VLAN}" =~ ^[0-9]+$ && "${VLAN}" -ge 1 && "${VLAN}" -le 4094 ) ]] || die "VLAN must be 1-4094 (got '${VLAN}')"
[[ "${HN}" =~ ^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$ ]] || die "HN must be a valid hostname (got '${HN}')"
if [[ "${IP}" != dhcp ]]; then
    [[ "${IP}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] || die "IP must be dhcp or a.b.c.d/nn, e.g. 192.0.2.80/24 (got '${IP}')"
    [[ "${GW}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || die "a fixed IP needs a gateway: GW=a.b.c.d"
fi
[[ -z "${PORT}" || ( "${PORT}" =~ ^[0-9]{4,5}$ && "${PORT}" -ge 1024 && "${PORT}" -le 65535 ) ]] || die "PORT must be 1024-65535 (got '${PORT}')"
[[ -z "${NAME}" || "${NAME}" =~ ^[A-Za-z0-9\ ._-]{1,64}$ ]] || die "NAME: up to 64 letters, digits, spaces, dots, dashes or underscores"
INSTALL_ARGS=()
[[ -z "${PORT}" ]] || INSTALL_ARGS+=(--port "${PORT}")
[[ -z "${NAME}" ]] || INSTALL_ARGS+=(--name "${NAME}")
[[ -z "${NO_PASSWORD}" ]] || INSTALL_ARGS+=(--no-password)

# ── Create, or find the one to update ─────────────────────────────────────────
EXISTING="$(find_manager_ct)"
if [[ -n "${CTID}" && -n "${EXISTING}" && "${CTID}" != "${EXISTING}" ]]; then
    die "container ${EXISTING} already runs the Pi Video Player Manager; CTID=${CTID} would make a second one. Unset CTID to update ${EXISTING}."
fi
if [[ -n "${EXISTING}" ]]; then
    CT="${EXISTING}"; MODE=update
    say "Updating the Pi Video Player Manager in container ${CT}"
    if ! pct status "${CT}" 2>/dev/null | grep -q running; then
        pct start "${CT}" || die "could not start container ${CT}"
    fi
fi

# The password - asked before the slow part, so nobody waits at a question.
if [[ -z "${NO_PASSWORD}" ]] && [[ -z "${EXISTING}" || -n "${NEW_PASSWORD}" ]]; then
    if ask_password && [[ -n "${PW}" ]]; then
        PW_FILE="$(mktemp)" && chmod 600 "${PW_FILE}" && printf '%s\n' "${PW}" > "${PW_FILE}" \
            || die "could not write a temp file for the password"
    elif [[ -n "${NEW_PASSWORD}" ]]; then
        INSTALL_ARGS+=(--new-password)    # Enter, or no terminal: a random one, printed
    fi
    unset PW
fi

if [[ -z "${EXISTING}" ]]; then
    MODE=create
    CT="${CTID:-$(pvesh get /cluster/nextid 2>/dev/null)}"
    [[ "${CT}" =~ ^[0-9]+$ ]] || die "could not get a free container id: set CTID=<id>"
    pct status "${CT}" >/dev/null 2>&1 && die "container ${CT} already exists and is not a Pi Video Player Manager: choose another CTID"
    STORAGE="${STORAGE:-$(first_storage rootdir)}"
    TEMPLATE_STORE="${TEMPLATE_STORE:-$(first_storage vztmpl)}"
    [[ -n "${STORAGE}" ]] || die "no active storage that can hold a container's disk: set STORAGE=<id> (see: pvesm status)"
    [[ -n "${TEMPLATE_STORE}" ]] || die "no active storage that can hold templates: set TEMPLATE_STORE=<id>"
    ip link show "${BRIDGE}" >/dev/null 2>&1 || die "bridge '${BRIDGE}' not found: set BRIDGE=<name> (see: ip link)"
    ARCH="$(dpkg --print-architecture 2>/dev/null || true)"
    case "${ARCH}" in amd64|arm64) ;; *) die "cannot tell the host's architecture (dpkg says '${ARCH:-nothing}')" ;; esac
    [[ "${IP}" != dhcp ]] || echo "NOTE: DHCP. The players are given this container's address, so give it a DHCP reservation, or re-run with IP=a.b.c.d/nn GW=a.b.c.d."
    TPL="$(ensure_template "${ARCH}")" || exit 1
    NET="name=eth0,bridge=${BRIDGE},ip=${IP}"
    [[ "${IP}" == dhcp ]] || NET="${NET},gw=${GW}"
    [[ -z "${VLAN}" ]] || NET="${NET},tag=${VLAN}"
    say "Creating container ${CT} (${HN}): Debian ${DEBIAN_VERSION}, ${CORES} core(s), ${RAM} MB, ${DISK} GB on ${STORAGE}"
    # Unprivileged; nesting=1 lets systemd set up the service's sandbox
    # (ProtectSystem, PrivateTmp) inside the container.
    pct create "${CT}" "${TPL}" --hostname "${HN}" --arch "${ARCH}" --ostype debian \
        --cores "${CORES}" --memory "${RAM}" --swap 512 --rootfs "${STORAGE}:${DISK}" \
        --net0 "${NET}" --unprivileged 1 --features nesting=1 --onboot 1 \
        --timezone host --tags "${TAG}" \
        || die "pct create failed (see above)"
    pct start "${CT}" || die "could not start container ${CT}"
fi
wait_net "${CT}"

# ── Install or update inside it: the same bootstrap as on any Linux machine ───
say "Installing curl in the container (if missing)"
if ! ctx "${CT}" sh -c 'command -v curl >/dev/null'; then
    ctx "${CT}" sh -c 'apt-get update -qq >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl ca-certificates >/dev/null' \
        || die "could not install curl in container ${CT}"
fi
if [[ -n "${PW_FILE}" ]]; then
    pct push "${CT}" "${PW_FILE}" "${CT_PW_FILE}" --perms 600 || die "could not hand the password to container ${CT}"
    rm -f "${PW_FILE}"; PW_FILE=""
    INSTALL_ARGS+=(--password-file "${CT_PW_FILE}")
fi
say "Running the manager's bootstrap in container ${CT}"
# The arguments are checked above (numbers, a hostname-like name, fixed paths); quoted for sh.
QARGS=""
for a in ${INSTALL_ARGS[@]+"${INSTALL_ARGS[@]}"}; do QARGS="${QARGS} '${a//\'/}'"; done
ctx "${CT}" SAW_REPO_URL="${SAW_REPO_URL:-}" sh -c \
    "curl -fsSL '${DIST_RAW}/bootstrap.sh' | bash -s --${QARGS}"
RC=$?
pct exec "${CT}" -- rm -f "${CT_PW_FILE}" >/dev/null 2>&1     # read and removed by the installer; in case it failed first
[[ ${RC} -eq 0 ]] || die "the install in container ${CT} failed (see above). Fix it and run this again - it updates the same container."

# ── Done ──────────────────────────────────────────────────────────────────────
CT_IP="$(pct exec "${CT}" -- hostname -I 2>/dev/null | awk '{print $1}')"
CT_PORT="$(pct exec "${CT}" -- sh -c "sed -n 's/^SAW_MANAGER_PORT=//p' /etc/saw-manager/manager.env" 2>/dev/null)"
CT_PORT="${CT_PORT:-5080}"
URL="http://${CT_IP:-<container address>}:${CT_PORT}/"
JOIN="${CT_IP:-<container address>}"; [[ "${CT_PORT}" == 5080 ]] || JOIN="${JOIN}:${CT_PORT}"
pct set "${CT}" --description "Pi Video Player Manager - dashboard ${URL}
Players: web UI -> Central Manager -> ${JOIN}
Password: change it on the Software page. Forgot it? On this host: NEW_PASSWORD=1 and the same link, or: pct enter ${CT}, then bash /opt/saw-manager-src/install.sh --password
Update: Software page -> This manager, or the same proxmox-lxc.sh link on this host." >/dev/null 2>&1 || true
echo ""
say "Pi Video Player Manager ${MODE}d in container ${CT}"
echo "    Dashboard : ${URL}   (any user name)"
echo "    Players   : web UI -> Central Manager -> ${JOIN} -> Join"
echo "    Password  : change it on the Software page; forgot it? NEW_PASSWORD=1 and this same link"
echo "    Update    : Software page -> This manager, or this same link"
exit 0
