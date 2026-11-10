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
# The manager's update, asked for from its own web page
# =============================================================================
# Installed by install.sh as /usr/local/lib/saw-manager/self-update.sh (root,
# 755) and run as root by saw-manager-update.service, which
# saw-manager-update.path starts when the manager writes its request file.
#
# The web page can only ASK: the request holds one word, "check" or
# "update", and nothing else in it is used. What is installed comes from the
# root-owned checkout that bootstrap.sh made (/etc/saw-manager/update-source
# names it, written by install.sh only for a root-owned checkout), from that
# checkout's own origin - the public dist repo. The manager runs as its own
# user and cannot change any of those.
#
#   check   git fetch; report the version published
#   update  git fetch + reset to it, then install.sh (settings kept). If the
#           install fails, the previous commit is put back and installed again.
#
# Status for the page: /var/lib/saw-manager-update/state.json and update.log
# (root-owned, readable). Never writes into the manager's own data directory
# beyond removing the request.
# =============================================================================
# Intentionally NOT using set -e (see install.sh).
set -uo pipefail

REQ="${SAW_UPDATE_REQUEST:-/var/lib/saw-manager/self-update-request}"
OUT="${SAW_UPDATE_OUT:-/var/lib/saw-manager-update}"
SOURCE="${SAW_UPDATE_SOURCE:-/etc/saw-manager/update-source}"
INSTALLED_PY="${SAW_UPDATE_INSTALLED:-/opt/saw-manager/saw_manager.py}"
LOG="${OUT}/update.log"

mkdir -p "${OUT}" && chmod 755 "${OUT}"

# The request: one word. A symlink is not followed (it lives in a directory
# the service user can write).
ACTION=check
if [[ -f "${REQ}" && ! -L "${REQ}" ]]; then
    ACTION="$(head -c 16 "${REQ}" | tr -cd 'a-z')"
fi
rm -f "${REQ}"
case "${ACTION}" in check|update) ;; *) ACTION=check ;; esac

json_str() { printf '%s' "$1" | tr -d '"\\\000-\037' | head -c 300; }
version_of() { sed -nE 's/^MANAGER_VERSION *= *([0-9]+).*/\1/p' "$1" 2>/dev/null | head -1; }

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
INSTALLED="$(version_of "${INSTALLED_PY}")"
AVAILABLE=""
state() {   # state STATE MESSAGE
    local tmp
    tmp="$(mktemp "${OUT}/.state.XXXXXX")" || return
    printf '{"state": "%s", "action": "%s", "message": "%s", "installed": %s, "available": %s, "started": "%s", "updated": "%s"}\n' \
        "$1" "${ACTION}" "$(json_str "$2")" "${INSTALLED:-null}" "${AVAILABLE:-null}" "${STARTED}" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "${tmp}"
    chmod 644 "${tmp}"
    mv -f "${tmp}" "${OUT}/state.json"
}
finish() { state "$1" "$2"; echo "== $2"; exit "$3"; }

: > "${LOG}"; chmod 644 "${LOG}"
exec >> "${LOG}" 2>&1
echo "== ${ACTION} requested from the web page, $(date)"
state running "${ACTION} started"

# ── Where from ────────────────────────────────────────────────────────────────
SRC="$(sed -n 's/^checkout=//p' "${SOURCE}" 2>/dev/null | head -1)"
BRANCH="$(sed -n 's/^branch=//p' "${SOURCE}" 2>/dev/null | head -1)"; BRANCH="${BRANCH:-main}"
[[ -n "${SRC}" && -d "${SRC}/.git" ]] \
    || finish failed "this manager was not installed with the online installer, so it cannot update itself. Run the install line (or the Proxmox line) once; from then on the button works." 1
[[ "$(stat -c %U "${SRC}")" == root ]] || finish failed "${SRC} is not owned by root - refusing to install from it" 1
GIT=(git -c "safe.directory=${SRC}" -C "${SRC}")

echo "== fetching ($("${GIT[@]}" remote get-url origin 2>/dev/null), ${BRANCH})"
"${GIT[@]}" fetch --prune --quiet origin \
    || finish failed "could not reach the update source (git fetch failed) - check this machine's internet access" 1
AVAILABLE="$("${GIT[@]}" show "origin/${BRANCH}:release.json" 2>/dev/null | grep -oE '"version": *[0-9]+' | grep -oE '[0-9]+' | head -1)"
[[ -n "${AVAILABLE}" ]] || finish failed "the update source has no release.json" 1
echo "== installed v${INSTALLED:-?}, published v${AVAILABLE}"

if [[ "${ACTION}" == check ]]; then
    if [[ -n "${INSTALLED}" ]] && (( AVAILABLE > INSTALLED )); then
        finish checked "v${AVAILABLE} is available" 0
    fi
    finish checked "up to date" 0
fi

# ── Update ────────────────────────────────────────────────────────────────────
if [[ -n "${INSTALLED}" ]] && (( AVAILABLE <= INSTALLED )); then
    finish "done" "already up to date (v${INSTALLED})" 0
fi
PREV="$("${GIT[@]}" rev-parse HEAD)"
"${GIT[@]}" reset --quiet --hard "origin/${BRANCH}" || finish failed "git reset failed" 1
echo "== installing v${AVAILABLE} ($("${GIT[@]}" rev-parse --short HEAD), was ${PREV:0:7})"
state running "installing v${AVAILABLE} - the page goes away for a few seconds while the manager restarts"
if bash "${SRC}/install.sh"; then
    INSTALLED="$(version_of "${INSTALLED_PY}")"
    finish "done" "updated to v${INSTALLED}" 0
fi
echo "== the install failed - putting ${PREV:0:7} back"
"${GIT[@]}" reset --quiet --hard "${PREV}"
if bash "${SRC}/install.sh"; then
    INSTALLED="$(version_of "${INSTALLED_PY}")"
    finish failed "v${AVAILABLE} did not install (see the log); v${INSTALLED} was put back" 1
fi
finish failed "v${AVAILABLE} did not install, and putting the previous version back failed too - run the install line by hand" 1
