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
# SAW central manager - one-line install AND update
# =============================================================================
# On a Raspberry Pi or a Linux machine / VM with internet access:
#
#   curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/bootstrap.sh | sudo bash
#
# Run the same command again to update. Options go after `-s --`:
#   ... | sudo bash -s -- --port 8080 --name "Museum"
# (every option of install.sh works here - see its header).
#
# Environment overrides:
#   SAW_REPO_URL   git URL to install from  (default: the public dist repo)
#   SAW_BRANCH     branch                  (default: main)
#   SAW_DIR        where the checkout lives (default: /opt/saw-manager-src)
# =============================================================================
# Intentionally NOT using set -e (see install.sh).
set -uo pipefail

REPO_URL="${SAW_REPO_URL:-https://github.com/stageaudioworks/pi-video-player-server-dist.git}"
BRANCH="${SAW_BRANCH:-main}"
CLONE_DIR="${SAW_DIR:-/opt/saw-manager-src}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[bootstrap]${NC} $*"; }
warn() { echo -e "${YELLOW}[bootstrap]${NC} $*"; }
fail() { echo -e "${RED}[bootstrap] ERROR:${NC} $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || fail "run as root - pipe into 'sudo bash', not 'bash'."

if ! command -v git >/dev/null 2>&1; then
    info "installing git"
    if command -v apt-get >/dev/null 2>&1; then
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null || fail "could not install git"
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y -q git >/dev/null || fail "could not install git"
    else
        fail "git is needed - install it, then run this again"
    fi
fi

GIT=(git -c "safe.directory=${CLONE_DIR}")
if [[ -d "${CLONE_DIR}/.git" ]]; then
    info "updating ${CLONE_DIR}"
    "${GIT[@]}" -C "${CLONE_DIR}" fetch --prune --quiet origin || fail "git fetch failed - check network access"
    # A pure deployment checkout, never edited by hand: reset, not merge.
    "${GIT[@]}" -C "${CLONE_DIR}" reset --quiet --hard "origin/${BRANCH}" || fail "no branch '${BRANCH}'?"
else
    if [[ -e "${CLONE_DIR}" ]]; then
        ASIDE="${CLONE_DIR}.old-$(date +%Y%m%d-%H%M%S)"
        mv "${CLONE_DIR}" "${ASIDE}" || fail "${CLONE_DIR} exists and is not a checkout; could not move it aside"
        warn "${CLONE_DIR} was not a git checkout - moved aside to ${ASIDE} (nothing deleted)"
    fi
    info "cloning ${REPO_URL}"
    "${GIT[@]}" clone --quiet --branch "${BRANCH}" "${REPO_URL}" "${CLONE_DIR}" || fail "git clone failed"
fi
info "at $("${GIT[@]}" -C "${CLONE_DIR}" rev-parse --short HEAD)"

[[ -f "${CLONE_DIR}/install.sh" ]] || fail "install.sh not found in ${CLONE_DIR}"
bash "${CLONE_DIR}/install.sh" "$@"
