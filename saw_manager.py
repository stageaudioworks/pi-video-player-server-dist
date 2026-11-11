#!/usr/bin/env python3
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
"""
Stage Audio Works central manager  (v5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

One page listing every player that has joined, live, in groups; remote
controls for a unit, a group or all of them; a content library and the clip
each player should hold in each slot; and the player version each should run.
Runs on a Raspberry Pi or any Linux machine with Python 3.9+ and Flask
(python3-flask on Debian, Ubuntu and Raspberry Pi OS).

How a player joins: its web UI's Central Manager panel takes this machine's
address (players v23 and later). From then on the player POSTs
/api/v1/heartbeat every INTERVAL_S; nothing is configured here. The player
calls the manager, never the reverse, so this works through NAT, and nothing
here needs to reach a player - except the operator's browser, for "Open web UI".

  * Content is a desired state: the reply to each heartbeat names the clip
    (hash and size) wanted in each managed slot; the unit fetches any that
    differ from /api/v1/content/<sha256> - resuming with Range requests - checks
    it and swaps it in. A unit that is off catches up when it is back.
  * Player versions likewise: the reply names the version wanted; the unit
    installs it only if it is newer than its own (never downgrades).
  * Remote controls are commands, not state: each one waits here at most
    COMMAND_TTL_S - show control must not happen late - and goes out on the
    unit's held /api/v1/wait request at once, or with its next heartbeat. They
    are what UDP can do (play, pause, restart, override, triggers) plus volume.

Identity: a unit's id comes from its Pi's serial number; on first contact its
key (a random secret made on the unit) is pinned here. A different key under a
known id is refused - "Forget" the unit to let it join again.

Writes to disk only on change - never per heartbeat, because this may itself
run on a Pi's SD card. Live state (last seen, what is playing, commands) is in
memory: after a restart every unit shows as not seen until its next heartbeat.

Environment (saw-manager.service; the password in /etc/saw-manager/manager.env):
  SAW_MANAGER_DATA      data directory           (/var/lib/saw-manager)
  SAW_MANAGER_PORT      port                     (5080)
  SAW_MANAGER_NAME      name shown on the units  (the hostname)
  SAW_MANAGER_PASSWORD  (v1/v2 only) the pages' password in plain text. From v3 the
                        password is a salted PBKDF2 hash in <data>/auth.json,
                        set by install.sh (it asks) or from the Software page;
                        this is used only while there is no auth.json. The
                        units' API (/api/v1/) uses the unit keys instead.

Operator routes (pages and their API; every POST needs the X-SAW header, which
a page on another site cannot send without the browser asking first):
  /  /content  /software  /unit/<id>               pages
  /api/units                 GET   every unit, live, and recent activity
  /api/units/<id>/assign     POST  {"slot", "sha256" | null}
  /api/units/<id>/group      POST  {"group": "..."}
  /api/units/<id>/forget     POST
  /api/assign                POST  {"sha256", "slot", "units": [ids]}
  /api/command               POST  {"units": [ids], "action", "volume"?}
  /api/library               GET;  POST a clip (raw body, X-Filename)
  /api/library/<sha>/delete  POST
  /api/players               GET;  POST a player script (raw body, X-Filename)
  /api/players/github        POST  fetch the latest published player release
  /api/players/<sha>/delete  POST
  /api/update                POST  {"sha256" | null, "units": [ids]}
  /api/password              GET   is one set;  POST {"current", "new"} change it
  /api/self                  GET   this manager's version and its update job's state
  /api/self/check|update     POST  ask the root update job (self-update.sh) to check
                                   or update - the request is one word, nothing else
Unit routes (unit id and key):
  /api/v1/heartbeat  POST   /api/v1/wait  POST
  /api/v1/content/<sha256>  GET   /api/v1/player/<sha256>  GET
  /api/v1/manual/<sha256>   GET   the manual published with the unit's target version
"""
from __future__ import annotations

import ast
import calendar
import collections
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Optional
from urllib.parse import unquote

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

MANAGER_VERSION = 5
PROTOCOL        = 1

DATA_DIR  = os.environ.get("SAW_MANAGER_DATA", "/var/lib/saw-manager")
PORT      = int(os.environ.get("SAW_MANAGER_PORT", "5080"))
NAME      = (os.environ.get("SAW_MANAGER_NAME", "").strip() or socket.gethostname())[:64]
ENV_PASSWORD = os.environ.get("SAW_MANAGER_PASSWORD", "")   # v1/v2 installs, until migrated
AUTH_FILE       = "auth.json"
AUTH_ITERATIONS = 200_000
PASSWORD_MIN, PASSWORD_MAX = 8, 128

INTERVAL_S       = 10        # told to every unit in each reply
OFFLINE_AFTER_S  = 35        # three heartbeats missed, and a little
SWEEP_S          = 5
WAIT_HOLD_S      = 25        # /api/v1/wait answers within this, commands or not
COMMAND_TTL_S    = 30        # a command not delivered by then is dropped, not run late
ACTIVITY_KEEP    = 300
SLOTS            = ("loop", "trigger1", "trigger2", "trigger3", "override")
CLIP_EXTENSIONS  = (".mp4",)  # what the player's own upload accepts
MAX_CLIP         = 32 * 1024 ** 3
DISK_RESERVE     = 512 * 1024 ** 2
MAX_HEARTBEAT    = 64 * 1024
MAX_GROUP        = 40

ACTIONS = {"play": "Play", "pause": "Pause", "restart": "Restart loop",
           "override_on": "Override on", "override_off": "Override off",
           "trigger_1": "Trigger 1", "trigger_2": "Trigger 2", "trigger_3": "Trigger 3",
           "volume": "Volume"}

# Player releases - the same contract as the player's own _validate_script()
# (tests/test_end_to_end.py checks the two lists agree).
PLAYER_SIGNATURE   = 'SAW_PLAYER_SIGNATURE = "SAW-PLAYER-AUTHENTIC"'
PLAYER_REQUIRED    = ("play_trigger", "mpv_watchdog", "udp_listener", "switch_video",
                      "SAW_PLAYER_SIGNATURE")
PLAYER_MIN_VERSION = 23      # the first with the manager client: older ones cannot take it
PLAYER_MAX_SIZE    = 4 * 1024 * 1024
MANUAL_PATH        = "docs/USER_MANUAL.pdf"   # in the player's public repo; fixed, never from a request
MANUAL_MAX_SIZE    = 32 * 1024 * 1024         # the player's UPDATE_MAX_MANUAL
# Fixed in code, as on the players (their constraint 17): never from a request.
GITHUB_SOURCES = (
    "https://raw.githubusercontent.com/stageaudioworks/pi-video-player-dist/main/{path}",
    "https://api.github.com/repos/stageaudioworks/pi-video-player-dist/contents/{path}?ref=main",
)
GITHUB_TIMEOUT = 20

# Updating this manager from its own page (see UPDATING THIS MANAGER). Written by
# install.sh / self-update.sh as root; only read here.
SELF_SOURCE  = os.environ.get("SAW_MANAGER_UPDATE_SOURCE", "/etc/saw-manager/update-source")
SELF_OUT     = os.environ.get("SAW_MANAGER_UPDATE_DIR", "/var/lib/saw-manager-update")
SELF_STALE_S = 30 * 60       # a "running" older than this was cut off (a reboot)
SELF_PENDING_S = 30          # a request no job picked up in this long is stuck (see self_view)

UNIT_ID_RE = re.compile(r"^(pi|mac|host)-[a-z0-9-]{1,48}$")
KEY_RE     = re.compile(r"^[0-9a-f]{32}$")
SHA_RE     = re.compile(r"^[0-9a-f]{64}$")

HERE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(HERE, "templates"),
            static_folder=os.path.join(HERE, "static"))

logger = logging.getLogger("saw_manager")
# Every open dashboard polls /api/units, every unit beats every 10 s and holds
# a wait open: werkzeug's per-request log would be most of the journal.
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def now_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clip_str(v, n: int = 128) -> Optional[str]:
    return v[:n] if isinstance(v, str) else None


def _clip_int(v) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


# ══════════════════════════════════════════════════════════════════════════════
# STORE - registry, library and player versions on disk; live state in memory
# ══════════════════════════════════════════════════════════════════════════════

class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.content_dir = os.path.join(data_dir, "content")
        self.player_dir = os.path.join(data_dir, "players")
        self.manual_dir = os.path.join(data_dir, "manuals")
        for d in (self.content_dir, self.player_dir, self.manual_dir):
            os.makedirs(d, exist_ok=True)
        self.lock = threading.Lock()
        self.cond = threading.Condition(self.lock)   # wakes held /api/v1/wait requests
        self.units = self._load("units.json").get("units", {})
        self.clips = self._load("library.json").get("clips", {})
        self.players = self._load("players.json").get("players", {})
        self.live = {}            # unit id -> what its last heartbeat said, and when
        self.queues = {}          # unit id -> commands not yet delivered
        self.activity = collections.OrderedDict()   # command id -> what became of it
        for d in (self.content_dir, self.player_dir, self.manual_dir):
            for name in os.listdir(d):
                if name.startswith(".upload-"):
                    try:
                        os.unlink(os.path.join(d, name))
                    except OSError:
                        pass

    def _load(self, name: str) -> dict:
        try:
            with open(os.path.join(self.dir, name)) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logger.exception("Could not read %s - starting with it empty", name)
            return {}

    def _save(self, name: str, obj: dict):
        """Temp file + os.replace: never a half-written registry."""
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix="." + name + ".")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f, indent=1, sort_keys=True)
            os.replace(tmp, os.path.join(self.dir, name))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save_units(self):
        self._save("units.json", {"units": self.units})

    def save_clips(self):
        self._save("library.json", {"clips": self.clips})

    def save_players(self):
        self._save("players.json", {"players": self.players})

    def clip_path(self, sha: str) -> str:
        return os.path.join(self.content_dir, sha)

    def player_path(self, sha: str) -> str:
        return os.path.join(self.player_dir, sha + ".py")

    def manual_path(self, sha: str) -> str:
        return os.path.join(self.manual_dir, sha + ".pdf")

    def online(self, uid: str, now: Optional[float] = None) -> bool:
        live = self.live.get(uid)
        return bool(live) and (now or time.monotonic()) - live["seen"] <= OFFLINE_AFTER_S


store: Optional[Store] = None


def stream_to_file(directory: str, length: int):
    """The request body to a temp file in `directory`, hashed on the way -
    no copy in RAM, none in /tmp. -> (temp path, sha256). Raises ValueError."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".upload-", suffix=".tmp")
    h, got = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = request.stream.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > length:
                    raise ValueError("more data than announced")
                h.update(chunk)
                f.write(chunk)
        if got != length:
            raise ValueError("upload incomplete (%d of %d bytes)" % (got, length))
    except Exception:
        os.unlink(tmp)
        raise
    return tmp, h.hexdigest()


def keep_file(tmp: str, final: str) -> bool:
    """Move an upload into place; if the same content is already there, drop
    it. -> True if it was a duplicate."""
    if os.path.exists(final):
        os.unlink(tmp)
        return True
    os.replace(tmp, final)
    return False


# ══════════════════════════════════════════════════════════════════════════════
# COMMANDS - remote controls (call with store.lock held)
# ══════════════════════════════════════════════════════════════════════════════

def _activity_add(entry: dict):
    store.activity[entry["id"]] = entry
    while len(store.activity) > ACTIVITY_KEEP:
        store.activity.popitem(last=False)


def queue_commands(uids: list, action: str, volume: Optional[int] = None) -> list:
    """Queue one command per unit. An offline unit's is recorded as not sent:
    it would only expire here, and the operator should know now."""
    now = time.monotonic()
    out = []
    for uid in uids:
        u = store.units.get(uid)
        if u is None:
            continue
        cid = "c" + secrets.token_hex(6)
        label = ACTIONS[action] + (" %d" % volume if action == "volume" else "")
        entry = {"id": cid, "at": now_iso(), "unit": uid, "name": u.get("name") or uid,
                 "label": label, "state": "queued", "message": ""}
        if store.online(uid, now):
            cmd = {"id": cid, "action": action, "expires": now + COMMAND_TTL_S}
            if action == "volume":
                cmd["volume"] = volume
            store.queues.setdefault(uid, []).append(cmd)
        else:
            entry.update(state="failed", message="offline - not sent")
        _activity_add(entry)
        out.append(entry)
    store.cond.notify_all()
    return out


def take_commands(uid: str) -> list:
    """Everything still fresh for this unit, marked sent; expired ones dropped."""
    now = time.monotonic()
    out = []
    for cmd in store.queues.pop(uid, []):
        entry = store.activity.get(cmd["id"])
        if cmd["expires"] < now:
            if entry:
                entry.update(state="expired", message="not delivered in %d s" % COMMAND_TTL_S)
            continue
        if entry:
            entry["state"] = "sent"
        out.append({k: v for k, v in cmd.items() if k != "expires"})
    return out


def expire_commands():
    now = time.monotonic()
    for uid in list(store.queues):
        keep = []
        for cmd in store.queues[uid]:
            if cmd["expires"] < now:
                entry = store.activity.get(cmd["id"])
                if entry:
                    entry.update(state="expired", message="not delivered in %d s" % COMMAND_TTL_S)
            else:
                keep.append(cmd)
        if keep:
            store.queues[uid] = keep
        else:
            del store.queues[uid]


def record_results(raw):
    if not isinstance(raw, list):
        return
    for r in raw[:50]:
        if not isinstance(r, dict) or not isinstance(r.get("id"), str):
            continue
        entry = store.activity.get(r["id"])
        if entry and entry["state"] in ("sent", "queued"):
            entry.update(state="done" if r.get("ok") is True else "failed",
                         message=_clip_str(r.get("message"), 200) or "")


# ══════════════════════════════════════════════════════════════════════════════
# THE UNITS' API
# ══════════════════════════════════════════════════════════════════════════════

def clean_status(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"state":    _clip_str(raw.get("state"), 16),
            "mode":     _clip_str(raw.get("mode"), 16),
            "trigger":  _clip_int(raw.get("trigger")),
            "video":    _clip_str(raw.get("video")),
            "position": _clip_int(raw.get("position")),
            "length":   _clip_int(raw.get("length")),
            "volume":   _clip_int(raw.get("volume"))}


def clean_health(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    disk = raw.get("disk") if isinstance(raw.get("disk"), dict) else {}
    temp = raw.get("temp_c")
    return {"temp_c": round(float(temp), 1) if isinstance(temp, (int, float))
            and not isinstance(temp, bool) else None,
            "uptime_s": _clip_int(raw.get("uptime_s")),
            "undervoltage": raw.get("undervoltage") is True,
            "undervoltage_since_boot": raw.get("undervoltage_since_boot") is True,
            "disk_free_mb": _clip_int(disk.get("free_mb")),
            "disk_total_mb": _clip_int(disk.get("total_mb"))}


def clean_content(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for slot in SLOTS:
        item = raw.get(slot)
        if isinstance(item, dict) and isinstance(item.get("sha256"), str) \
                and SHA_RE.match(item["sha256"]):
            out[slot] = {"sha256": item["sha256"], "size": _clip_int(item.get("size"))}
        else:
            out[slot] = None
    return out


def clean_job(raw) -> Optional[dict]:
    if not isinstance(raw, dict) or raw.get("slot") not in SLOTS + ("player", "manual"):
        return None
    return {"slot": raw["slot"], "sha256": _clip_str(raw.get("sha256"), 64),
            "size": _clip_int(raw.get("size")), "done": _clip_int(raw.get("done")),
            "resumed_from": _clip_int(raw.get("resumed_from"))}


def clean_failed(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {s: {"sha256": _clip_str(v.get("sha256"), 64), "error": _clip_str(v.get("error"), 300)}
            for s, v in raw.items() if s in SLOTS + ("player", "manual") and isinstance(v, dict)}


def clean_manual_report(raw) -> Optional[dict]:
    """The manual a unit (v25+) reports it has: {"sha256", "size"} or None."""
    if isinstance(raw, dict) and isinstance(raw.get("sha256"), str) and SHA_RE.match(raw["sha256"]):
        return {"sha256": raw["sha256"], "size": _clip_int(raw.get("size"))}
    return None


def _unit_auth(uid, key):
    """-> (http status, error), or None for a known unit with its own key.
    Call with store.lock held."""
    if not (isinstance(uid, str) and UNIT_ID_RE.match(uid) and isinstance(key, str)
            and KEY_RE.match(key)):
        return 400, "unit_id or key missing or malformed"
    u = store.units.get(uid)
    if u is None:
        return 403, "unknown unit"
    if not hmac.compare_digest(u.get("key", ""), key):
        return 403, ("this manager already knows %s with another key. If the unit was "
                     "reinstalled, Forget it on the manager and it joins again." % uid)
    return None


def handle_heartbeat(body, remote_addr: str):
    """-> (http status, reply). Pure enough to test without HTTP."""
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": "not JSON"}
    uid, key = body.get("unit_id"), body.get("key")
    if not (isinstance(uid, str) and UNIT_ID_RE.match(uid) and isinstance(key, str)
            and KEY_RE.match(key)):
        return 400, {"ok": False, "error": "unit_id or key missing or malformed"}
    if body.get("protocol") != PROTOCOL:
        return 400, {"ok": False, "error": "this manager speaks protocol %d; the unit sent %r - "
                     "update the one that is older" % (PROTOCOL, body.get("protocol"))}
    now = time.monotonic()
    ident = {"name": _clip_str(body.get("name"), 64) or "",
             "hostname": _clip_str(body.get("hostname"), 64) or "",
             "version": _clip_int(body.get("version")),
             "address": _clip_str(body.get("address"), 64),
             "web_port": _clip_int(body.get("web_port")) or 5000}
    joined = back = False
    with store.lock:
        u = store.units.get(uid)
        if u is not None:
            refused = _unit_auth(uid, key)
            if refused:
                return refused[0], {"ok": False, "error": refused[1]}
        changed = False
        if u is None:
            u = {"key": key, "first_seen": now_iso(), "assign": {}, "group": ""}
            store.units[uid] = u
            changed = joined = True
        for k, v in ident.items():
            if u.get(k) != v:
                u[k] = v
                changed = True
        prev = store.live.get(uid)
        back = prev is not None and prev.get("offline_logged", False)
        store.live[uid] = {"seen": now, "seen_wall": time.time(), "remote": remote_addr,
                           "status": clean_status(body.get("status")),
                           "health": clean_health(body.get("health")),
                           "content": clean_content(body.get("content")),
                           "job": clean_job(body.get("job")),
                           "failed": clean_failed(body.get("failed")),
                           "manual": clean_manual_report(body.get("manual")),
                           "offline_logged": False}
        record_results(body.get("results"))
        content = {slot: {"sha256": sha, "size": store.clips[sha]["size"]}
                   for slot, sha in u["assign"].items() if sha in store.clips}
        player = manual = None
        target = u.get("player")
        if target in store.players:
            p = store.players[target]
            player = {"version": p["version"], "sha256": target, "size": p["size"]}
            if p.get("manual"):
                manual = dict(p["manual"], version=p["version"])
        commands = take_commands(uid)
        if changed:
            store.save_units()
    if joined:
        logger.info("New unit joined: %s %r (%s, v%s)", uid, ident["name"],
                    ident["address"] or remote_addr, ident["version"])
    elif back:
        logger.info("Unit back online: %s %r", uid, ident["name"])
    reply = {"ok": True, "protocol": PROTOCOL, "interval": INTERVAL_S,
             "server_name": NAME, "content": content, "wait": True, "commands": commands}
    if player:
        reply["player"] = player
    if manual:          # players v25+: the manual published with that version
        reply["manual"] = manual
    return 200, reply


@app.route("/api/v1/heartbeat", methods=["POST"])
def heartbeat_route():
    if (request.content_length or 0) > MAX_HEARTBEAT:
        return jsonify({"ok": False, "error": "heartbeat too large"}), 413
    code, reply = handle_heartbeat(request.get_json(silent=True), request.remote_addr or "")
    return jsonify(reply), code


@app.route("/api/v1/wait", methods=["POST"])
def wait_route():
    """Held until there is a command for this unit, or WAIT_HOLD_S pass. One
    thread per held request: fine for the dozens of units a site has."""
    if (request.content_length or 0) > 4096:
        abort(413)
    body = request.get_json(silent=True) or {}
    uid, key = body.get("unit_id"), body.get("key")
    deadline = time.monotonic() + WAIT_HOLD_S
    with store.cond:
        refused = _unit_auth(uid, key)
        if refused:
            return jsonify({"ok": False, "error": refused[1]}), refused[0]
        while True:
            if uid not in store.units:
                return jsonify({"ok": False, "error": "forgotten"}), 403
            if store.queues.get(uid):
                commands = take_commands(uid)
                if commands:
                    break
            left = deadline - time.monotonic()
            if left <= 0:
                commands = []
                break
            store.cond.wait(left)
    return jsonify({"ok": True, "commands": commands})


def _unit_from_headers():
    uid, key = request.headers.get("X-SAW-Unit", ""), request.headers.get("X-SAW-Key", "")
    with store.lock:
        if _unit_auth(uid, key):
            abort(403)
        return uid, dict(store.units[uid])


@app.route("/api/v1/content/<sha>")
def content_route(sha):
    """A clip, to a unit it is assigned to. Range requests are answered, so a
    unit resumes a fetch that was cut off."""
    if not SHA_RE.match(sha):
        abort(404)
    uid, u = _unit_from_headers()
    with store.lock:
        if sha not in u.get("assign", {}).values() or sha not in store.clips:
            abort(404)
    path = store.clip_path(sha)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/octet-stream", conditional=True, max_age=0)


@app.route("/api/v1/player/<sha>")
def player_route(sha):
    """A player script, to a unit it is the target of."""
    if not SHA_RE.match(sha):
        abort(404)
    uid, u = _unit_from_headers()
    with store.lock:
        if u.get("player") != sha or sha not in store.players:
            abort(404)
    path = store.player_path(sha)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/octet-stream", max_age=0)


@app.route("/api/v1/manual/<sha>")
def manual_route(sha):
    """The user manual published with the unit's target player version (v5).
    Units v25+ ask for it, to stay current where they cannot reach GitHub."""
    if not SHA_RE.match(sha):
        abort(404)
    uid, u = _unit_from_headers()
    with store.lock:
        p = store.players.get(u.get("player") or "")
        if not p or (p.get("manual") or {}).get("sha256") != sha:
            abort(404)
    path = store.manual_path(sha)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="application/pdf", max_age=0)


def sweeper():
    """Logs a unit going offline - once, not every sweep - and drops commands
    that waited too long."""
    while True:
        time.sleep(SWEEP_S)
        now = time.monotonic()
        gone = []
        with store.lock:
            for uid, live in store.live.items():
                if not live["offline_logged"] and now - live["seen"] > OFFLINE_AFTER_S:
                    live["offline_logged"] = True
                    gone.append((uid, store.units.get(uid, {}).get("name", "")))
            expire_commands()
        for uid, name in gone:
            logger.warning("Unit offline: %s %r (no heartbeat for %d s)", uid, name, OFFLINE_AFTER_S)


# ══════════════════════════════════════════════════════════════════════════════
# PLAYER VERSIONS
# ══════════════════════════════════════════════════════════════════════════════

def check_player_script(content: bytes):
    """-> the version it declares. Raises ValueError with a reason. The same
    checks as the player's _validate_script(), plus the version rules."""
    if len(content) > PLAYER_MAX_SIZE:
        raise ValueError("larger than %d MB - not a player script" % (PLAYER_MAX_SIZE // 1048576))
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not UTF-8 text")
    try:
        ast.parse(text)
    except SyntaxError as e:
        raise ValueError("Python syntax error at line %s: %s" % (e.lineno, e.msg))
    if PLAYER_SIGNATURE not in text:
        raise ValueError("no SAW_PLAYER_SIGNATURE - not a SAW player script")
    for name in PLAYER_REQUIRED:
        if name not in text:
            raise ValueError("required identifier %r not found" % name)
    m = re.search(r"^PLAYER_VERSION\s*=\s*(\d+)\s*$", text, re.M)
    if not m:
        raise ValueError("it declares no PLAYER_VERSION")
    version = int(m.group(1))
    if version < PLAYER_MIN_VERSION:
        raise ValueError("v%d has no manager client - units can only be updated from here to "
                         "v%d or later" % (version, PLAYER_MIN_VERSION))
    return version


def add_player(content: bytes, source: str) -> dict:
    """Store a checked player script. -> {"sha256", "version", "duplicate"}."""
    version = check_player_script(content)
    sha = hashlib.sha256(content).hexdigest()
    with store.lock:
        for other, p in store.players.items():
            if p["version"] == version and other != sha:
                raise ValueError("a different v%d is already here (%s). Versions must never "
                                 "change meaning - delete that one first if it is wrong."
                                 % (version, other[:12]))
        duplicate = sha in store.players
        if not duplicate:
            fd, tmp = tempfile.mkstemp(dir=store.player_dir, prefix=".upload-")
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            os.replace(tmp, store.player_path(sha))
            store.players[sha] = {"version": version, "size": len(content),
                                  "added": now_iso(), "source": source}
            store.save_players()
    if not duplicate:
        logger.info("Player v%d added (%s, %s)", version, source, sha[:12])
    return {"sha256": sha, "version": version, "duplicate": duplicate}


def _github_get(path: str, max_bytes: int) -> bytes:
    problems = []
    for tpl in GITHUB_SOURCES:
        url = tpl.format(path=path)
        host = url.split("/")[2]
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.raw",
                                                   "User-Agent": "saw-manager"})
        try:
            with urllib.request.urlopen(req, timeout=GITHUB_TIMEOUT) as r:
                data = r.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError("%s sent more than %d bytes for %s" % (host, max_bytes, path))
            return data
        except urllib.error.HTTPError as e:
            problems.append("%s: HTTP %d" % (host, e.code))
        except (urllib.error.URLError, OSError) as e:
            problems.append("%s: %s" % (host, getattr(e, "reason", e)))
    raise ValueError("could not reach GitHub (%s). Without internet here, download the "
                     "player script elsewhere and upload it." % "; ".join(problems))


def fetch_github_player() -> dict:
    """The latest published player: release.json, then the script it names,
    checked against its SHA-256 - the same checks a unit makes."""
    raw = _github_get("release.json", 64 * 1024)
    try:
        m = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ValueError("release.json is not valid JSON")
    if not isinstance(m, dict):
        raise ValueError("release.json is not a JSON object")
    v, f, sha = m.get("version"), m.get("file"), m.get("sha256")
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        raise ValueError("release.json has no valid version")
    if f != "RPi_Video_Player_v%d.py" % v:      # it becomes part of a URL
        raise ValueError("release.json names %r" % f)
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise ValueError("release.json has no valid SHA-256")
    content = _github_get(f, PLAYER_MAX_SIZE)
    if hashlib.sha256(content).hexdigest() != sha:
        raise ValueError("the downloaded script does not match release.json's SHA-256")
    result = add_player(content, "GitHub release v%d" % v)
    # The manual published with it (v15+ releases): fetched from the same fixed
    # source, checked against release.json. A problem with it is reported and
    # never stops the player version from being added - as on the units.
    msha, msize = m.get("manual_sha256"), m.get("manual_size")
    if isinstance(msha, str) and SHA_RE.match(msha) and isinstance(msize, int) \
            and not isinstance(msize, bool) and 0 < msize <= MANUAL_MAX_SIZE:
        with store.lock:
            have = (store.players.get(result["sha256"], {}).get("manual") or {}).get("sha256")
        if have == msha and os.path.exists(store.manual_path(msha)):
            result["manual"] = "already here"
        else:
            try:
                pdf = _github_get(MANUAL_PATH, MANUAL_MAX_SIZE)
                if len(pdf) != msize or hashlib.sha256(pdf).hexdigest() != msha:
                    raise ValueError("the manual does not match release.json's SHA-256")
                attach_manual(result["sha256"], pdf, "GitHub release v%d" % v)
                result["manual"] = "added"
            except ValueError as e:
                result["manual"] = "not added: %s" % e
                logger.warning("Manual for player v%d not added: %s", v, e)
    else:
        result["manual"] = "none published with this release"
    return result


def attach_manual(player_sha: str, pdf: bytes, source: str):
    """Keep the user manual that goes with a player version. Raises ValueError."""
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("not a PDF")
    if len(pdf) > MANUAL_MAX_SIZE:
        raise ValueError("larger than %d MB" % (MANUAL_MAX_SIZE // 1048576))
    sha = hashlib.sha256(pdf).hexdigest()
    with store.lock:
        p = store.players.get(player_sha)
        if p is None:
            raise ValueError("no such player version here")
        fd, tmp = tempfile.mkstemp(dir=store.manual_dir, prefix=".upload-")
        with os.fdopen(fd, "wb") as f:
            f.write(pdf)
        os.replace(tmp, store.manual_path(sha))
        old = (p.get("manual") or {}).get("sha256")
        p["manual"] = {"sha256": sha, "size": len(pdf), "source": source, "added": now_iso()}
        store.save_players()
        _drop_unused_manual(old)
    logger.info("Manual for player v%d: %s (%d KB, %s)", p["version"], source, len(pdf) // 1024, sha[:12])


def _drop_unused_manual(sha: Optional[str]):
    """Delete a manual file no player version refers to any more (lock held)."""
    if not sha or any((p.get("manual") or {}).get("sha256") == sha for p in store.players.values()):
        return
    try:
        os.unlink(store.manual_path(sha))
    except OSError:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# THE OPERATOR'S PAGES AND API
# ══════════════════════════════════════════════════════════════════════════════

# ── The pages' password ───────────────────────────────────────────────────────
# A salted PBKDF2-SHA256 hash in <data>/auth.json (the service's own directory:
# it cannot write /etc). Set by install.sh, which asks for it, or from the
# Software page. Checking a password costs PBKDF2 once; the dashboard polls
# every 2 s, so the last password that checked out against this hash is
# remembered as a cheap salted digest - a change of password changes the hash
# and forgets it. While there is no auth.json, SAW_MANAGER_PASSWORD (v1/v2)
# is used; install.sh moves it into auth.json on the next update.

_auth_ok = {"hash": None, "digest": None}


def _auth_path(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or (store.dir if store else DATA_DIR), AUTH_FILE)


def load_auth(data_dir: Optional[str] = None) -> Optional[dict]:
    try:
        with open(_auth_path(data_dir)) as f:
            a = json.load(f)
    except (OSError, ValueError):
        return None
    ok = (isinstance(a, dict) and a.get("scheme") == "pbkdf2_sha256"
          and isinstance(a.get("iterations"), int) and isinstance(a.get("salt"), str)
          and isinstance(a.get("hash"), str))
    return a if ok else None


def password_required() -> bool:
    return load_auth() is not None or bool(ENV_PASSWORD)


def check_password(pw) -> bool:
    if not isinstance(pw, str):
        return False
    a = load_auth()
    if a is None:
        return hmac.compare_digest(pw.encode(), ENV_PASSWORD.encode()) if ENV_PASSWORD else True
    salt = bytes.fromhex(a["salt"])
    quick = hashlib.sha256(salt + pw.encode()).hexdigest()
    if _auth_ok["hash"] == a["hash"] and hmac.compare_digest(_auth_ok["digest"] or "", quick):
        return True
    good = hmac.compare_digest(
        hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, a["iterations"]).hex(), a["hash"])
    if good:
        _auth_ok.update(hash=a["hash"], digest=quick)
    return good


def password_problem(pw) -> Optional[str]:
    if not isinstance(pw, str) or len(pw) < PASSWORD_MIN:
        return "at least %d characters" % PASSWORD_MIN
    if len(pw) > PASSWORD_MAX:
        return "at most %d characters" % PASSWORD_MAX
    if re.search(r"[\x00-\x1f\x7f]", pw):
        return "no control characters"
    return None


def set_password(pw: str, data_dir: Optional[str] = None):
    """Hash and store a new password (temp file + rename, mode 600)."""
    problem = password_problem(pw)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(16)
    record = {"scheme": "pbkdf2_sha256", "iterations": AUTH_ITERATIONS, "salt": salt.hex(),
              "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, AUTH_ITERATIONS).hex(),
              "changed": now_iso()}
    path = _auth_path(data_dir)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".auth.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@app.before_request
def _guard():
    if request.path.startswith("/api/v1/"):
        return None                         # units: unit id and key
    if password_required():
        a = request.authorization
        if not (a and a.password is not None and check_password(a.password)):
            return Response("Password required", 401,
                            {"WWW-Authenticate": 'Basic realm="Pi Video Player Manager"'})
    # A page on another site can make the browser POST here, with the cached
    # password - but not with a custom header, without asking first (CORS).
    if request.method == "POST" and request.headers.get("X-SAW") != "1":
        return jsonify({"ok": False, "error": "missing X-SAW header"}), 403
    return None


def status_label(online: bool, seen: bool, st: dict):
    """(text, tone) for the dashboard's pill. tone: ok / info / warn / err / off."""
    if not seen:
        return "Not seen yet", "off"
    if not online:
        return "Offline", "off"
    s, mode = st.get("state"), st.get("mode")
    if s == "starting":
        return "Starting", "warn"
    if s == "no_screen":
        return "No screen", "err"
    if s == "stopped":
        return "Stopped", "err"
    if s == "paused":
        return "Paused", "warn"
    if s == "playing":
        if mode == "override":
            return "Override", "info"
        if mode == "trigger":
            return ("Playing trigger %d" % st["trigger"]) if st.get("trigger") else "Playing trigger", "info"
        return "Looping", "ok"
    return "Unknown", "warn"


def slot_view(slot: str, assigned: Optional[str], have: Optional[dict], job, failed) -> dict:
    """One slot of one unit: what it should hold, what it holds, and where that stands."""
    clip = store.clips.get(assigned) if assigned else None
    have_sha = (have or {}).get("sha256")
    have_clip = store.clips.get(have_sha) if have_sha else None
    view = {"slot": slot, "assigned": assigned, "assigned_name": clip["filename"] if clip else None,
            "have": have_sha, "have_name": have_clip["filename"] if have_clip else None,
            "have_size": (have or {}).get("size")}
    if not assigned:
        view["sync"], view["sync_text"] = "unmanaged", ("set on the unit" if have_sha else "empty")
    elif have_sha == assigned:
        view["sync"], view["sync_text"] = "ok", "in place"
    elif job and job.get("slot") == slot and job.get("sha256") == assigned:
        pct = int(100 * (job.get("done") or 0) / job["size"]) if job.get("size") else 0
        view["sync"], view["sync_text"] = "fetching", "fetching %d %%%s" % (
            pct, " (resumed)" if job.get("resumed_from") else "")
    elif slot in failed and failed[slot].get("sha256") == assigned:
        view["sync"], view["sync_text"] = "failed", "failed: %s" % failed[slot].get("error")
    else:
        view["sync"], view["sync_text"] = "pending", "waiting for the unit"
    return view


def update_view(u: dict, live: dict) -> Optional[dict]:
    target = u.get("player")
    p = store.players.get(target) if target else None
    if not p:
        return None
    have = u.get("version") or 0
    failed = (live.get("failed") or {}).get("player")
    job = live.get("job") or {}
    if have >= p["version"]:
        state, text = "ok", "up to date"
    elif job.get("slot") == "player":
        state, text = "fetching", "installing"
    elif failed and failed.get("sha256") == target:
        state, text = "failed", "failed: %s" % failed.get("error")
    else:
        state, text = "pending", "waiting for the unit"
    view = {"sha256": target, "version": p["version"], "state": state, "text": text}
    want = (p.get("manual") or {}).get("sha256")
    if want:
        mfailed = (live.get("failed") or {}).get("manual")
        if (live.get("manual") or {}).get("sha256") == want:
            view["manual"] = {"state": "ok", "text": "manual in place"}
        elif have and have < 25:
            view["manual"] = {"state": "off", "text": "manual: needs player v25+"}
        elif have > p["version"]:
            view["manual"] = {"state": "off", "text": "manual: the unit runs a newer version"}
        elif job.get("slot") == "manual":
            view["manual"] = {"state": "fetching", "text": "manual: fetching"}
        elif mfailed and mfailed.get("sha256") == want:
            view["manual"] = {"state": "failed", "text": "manual failed: %s" % mfailed.get("error")}
        else:
            view["manual"] = {"state": "pending", "text": "manual: waiting for the unit"}
    return view


def unit_view(uid: str, u: dict, now: float) -> dict:
    live = store.live.get(uid) or {}
    seen = bool(live)
    online = seen and now - live["seen"] <= OFFLINE_AFTER_S
    st = live.get("status") or {}
    text, tone = status_label(online, seen, st)
    addr = u.get("address") or live.get("remote")
    host = ("[%s]" % addr) if addr and ":" in addr else addr
    content = live.get("content") or {}
    return {
        "id": uid, "name": u.get("name") or "", "hostname": u.get("hostname") or "",
        "group": u.get("group") or "", "version": u.get("version"),
        "address": addr, "remote": live.get("remote"),
        "web_url": "http://%s:%d/" % (host, u.get("web_port") or 5000) if host else None,
        "online": online, "seen": seen, "first_seen": u.get("first_seen"),
        "last_seen_ago": round(now - live["seen"]) if seen else None,
        "status_text": text, "status_tone": tone, "status": st,
        "health": live.get("health") or {},
        "slots": [slot_view(s, u.get("assign", {}).get(s), content.get(s), live.get("job"),
                            live.get("failed") or {}) for s in SLOTS],
        "update": update_view(u, live),
    }


def units_snapshot() -> list:
    now = time.monotonic()
    with store.lock:
        views = [unit_view(uid, u, now) for uid, u in store.units.items()]
    return sorted(views, key=lambda v: (v["group"] == "", v["group"].lower(),
                                        (v["name"] or v["hostname"] or v["id"]).lower(), v["id"]))


def join_address() -> str:
    """What to type into a unit: the address this page was opened on, unless
    that was localhost - then this machine's outward address."""
    host = (request.host or "").rsplit(":", 1)[0].strip("[]")
    if host and host not in ("localhost", "127.0.0.1", "::1"):
        return host if PORT == 5080 else "%s:%d" % (host, PORT)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))     # TEST-NET: nothing is sent
            ip = s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        ip = socket.gethostname()
    return ip if PORT == 5080 else "%s:%d" % (ip, PORT)


def _page(template: str, **kw):
    return render_template(template, name=NAME, version=MANAGER_VERSION, **kw)


@app.route("/")
def index():
    return _page("index.html", join=join_address(), page="units")


@app.route("/content")
def content_page():
    return _page("content.html", page="content", slots=SLOTS)


@app.route("/software")
def software_page():
    return _page("software.html", page="software", min_version=PLAYER_MIN_VERSION)


@app.route("/unit/<uid>")
def unit_page(uid):
    with store.lock:
        if uid not in store.units:
            abort(404)
    return _page("unit.html", page="units", uid=uid, slots=SLOTS)


@app.route("/api/units")
def api_units():
    with store.lock:
        activity = list(reversed(list(store.activity.values())))[:15]
        groups = sorted({u.get("group") for u in store.units.values() if u.get("group")},
                        key=str.lower)
    return jsonify({"units": units_snapshot(), "offline_after": OFFLINE_AFTER_S,
                    "activity": activity, "groups": groups})


def _ids(data) -> Optional[list]:
    uids = data.get("units")
    if not isinstance(uids, list) or not uids or not all(isinstance(u, str) for u in uids):
        return None
    return list(dict.fromkeys(uids))


def _assign(uids, slot, sha) -> Optional[str]:
    """Record an assignment (sha None: unassign). -> an error, or None."""
    if slot not in SLOTS:
        return "unknown slot"
    if sha is not None and sha not in store.clips:
        return "no such clip in the library"
    missing = [u for u in uids if u not in store.units]
    if missing:
        return "unknown unit %s" % missing[0]
    for uid in uids:
        assign = store.units[uid].setdefault("assign", {})
        if sha is None:
            assign.pop(slot, None)
        else:
            assign[slot] = sha
    store.save_units()
    return None


@app.route("/api/units/<uid>/assign", methods=["POST"])
def api_unit_assign(uid):
    data = request.get_json(silent=True) or {}
    with store.lock:
        err = _assign([uid], data.get("slot"), data.get("sha256") or None)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    logger.info("Assigned %s on %s: %s", data.get("slot"), uid, data.get("sha256") or "(none)")
    return jsonify({"ok": True})


@app.route("/api/assign", methods=["POST"])
def api_assign():
    data = request.get_json(silent=True) or {}
    uids = _ids(data)
    if not uids:
        return jsonify({"ok": False, "error": "choose at least one unit"}), 400
    with store.lock:
        err = _assign(uids, data.get("slot"), data.get("sha256") or None)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    logger.info("Assigned %s to %s on %d unit(s)", data.get("sha256"), data.get("slot"), len(uids))
    return jsonify({"ok": True})


@app.route("/api/units/<uid>/group", methods=["POST"])
def api_unit_group(uid):
    data = request.get_json(silent=True) or {}
    group = data.get("group")
    if not isinstance(group, str):
        return jsonify({"ok": False, "error": "group must be text"}), 400
    group = re.sub(r"[\x00-\x1f\x7f]", "", group).strip()[:MAX_GROUP]
    with store.lock:
        u = store.units.get(uid)
        if u is None:
            return jsonify({"ok": False, "error": "unknown unit"}), 404
        if u.get("group", "") != group:
            u["group"] = group
            store.save_units()
    return jsonify({"ok": True, "group": group})


@app.route("/api/units/<uid>/forget", methods=["POST"])
def api_unit_forget(uid):
    with store.lock:
        if store.units.pop(uid, None) is None:
            return jsonify({"ok": False, "error": "unknown unit"}), 404
        store.live.pop(uid, None)
        store.queues.pop(uid, None)
        store.save_units()
        store.cond.notify_all()
    logger.info("Unit forgotten: %s (it joins again on its next heartbeat, if it still reports here)", uid)
    return jsonify({"ok": True})


@app.route("/api/command", methods=["POST"])
def api_command():
    data = request.get_json(silent=True) or {}
    uids, action = _ids(data), data.get("action")
    if not uids:
        return jsonify({"ok": False, "error": "choose at least one unit"}), 400
    if action not in ACTIONS:
        return jsonify({"ok": False, "error": "unknown action"}), 400
    volume = data.get("volume")
    if action == "volume" and (not isinstance(volume, int) or isinstance(volume, bool)
                               or not 0 <= volume <= 100):
        return jsonify({"ok": False, "error": "volume must be 0-100"}), 400
    with store.lock:
        entries = queue_commands(uids, action, volume if action == "volume" else None)
    sent = sum(1 for e in entries if e["state"] == "queued")
    logger.info("Command %s to %d unit(s)%s", ACTIONS[action], sent,
                "" if sent == len(entries) else " (%d offline)" % (len(entries) - sent))
    return jsonify({"ok": True, "sent": sent, "offline": len(entries) - sent,
                    "ids": [e["id"] for e in entries]})


@app.route("/api/library", methods=["GET"])
def api_library():
    with store.lock:
        used = {}
        for uid, u in store.units.items():
            for slot, sha in u.get("assign", {}).items():
                used.setdefault(sha, []).append({"unit": uid, "name": u.get("name") or uid, "slot": slot})
        clips = [dict(c, sha256=sha, used=used.get(sha, [])) for sha, c in store.clips.items()]
    clips.sort(key=lambda c: c.get("added") or "", reverse=True)
    try:
        du = shutil.disk_usage(store.content_dir)
        disk = {"free_mb": du.free // 1048576, "total_mb": du.total // 1048576}
    except OSError:
        disk = None
    return jsonify({"clips": clips, "disk": disk})


def clean_filename(raw: str, extensions) -> Optional[str]:
    name = os.path.basename(unquote(raw or "").replace("\\", "/")).strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:120]
    if not name or not name.lower().endswith(extensions):
        return None
    return name


@app.route("/api/library", methods=["POST"])
def api_library_upload():
    name = clean_filename(request.headers.get("X-Filename", ""), CLIP_EXTENSIONS)
    if not name:
        return jsonify({"ok": False, "error": "only %s files" % ", ".join(CLIP_EXTENSIONS)}), 400
    length = request.content_length
    if not length:
        return jsonify({"ok": False, "error": "no length given"}), 411
    if length > MAX_CLIP:
        return jsonify({"ok": False, "error": "larger than %d GB" % (MAX_CLIP // 1024 ** 3)}), 413
    free = shutil.disk_usage(store.content_dir).free
    if length > free - DISK_RESERVE:
        return jsonify({"ok": False, "error": "not enough space on the manager (%d MB free)"
                        % (free // 1048576)}), 507
    try:
        tmp, sha = stream_to_file(store.content_dir, length)
        keep_file(tmp, store.clip_path(sha))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e) or type(e).__name__}), 400
    with store.lock:
        known = sha in store.clips
        if not known:
            store.clips[sha] = {"filename": name, "size": length, "added": now_iso()}
            store.save_clips()
        filename = store.clips[sha]["filename"]
    if not known:
        logger.info("Clip added: %s (%.1f MB, %s)", name, length / 1048576, sha[:12])
    return jsonify({"ok": True, "sha256": sha, "duplicate": known, "filename": filename})


@app.route("/api/library/<sha>/delete", methods=["POST"])
def api_library_delete(sha):
    with store.lock:
        if sha not in store.clips:
            return jsonify({"ok": False, "error": "no such clip"}), 404
        users = [u.get("name") or uid for uid, u in store.units.items()
                 if sha in u.get("assign", {}).values()]
        if users:
            return jsonify({"ok": False, "error": "still assigned to %s - unassign it first"
                            % ", ".join(users[:5])}), 409
        clip = store.clips.pop(sha)
        store.save_clips()
    try:
        os.unlink(store.clip_path(sha))
    except OSError:
        pass
    logger.info("Clip deleted: %s (%s)", clip["filename"], sha[:12])
    return jsonify({"ok": True})


@app.route("/api/players", methods=["GET"])
def api_players():
    with store.lock:
        used = {}
        for uid, u in store.units.items():
            if u.get("player"):
                used.setdefault(u["player"], []).append(u.get("name") or uid)
        players = [dict(p, sha256=sha, used=used.get(sha, [])) for sha, p in store.players.items()]
    players.sort(key=lambda p: p["version"], reverse=True)
    return jsonify({"players": players, "min_version": PLAYER_MIN_VERSION})


@app.route("/api/players", methods=["POST"])
def api_players_upload():
    name = clean_filename(request.headers.get("X-Filename", ""), (".py",))
    length = request.content_length or 0
    if not name:
        return jsonify({"ok": False, "error": "a player script is a .py file"}), 400
    if not 0 < length <= PLAYER_MAX_SIZE:
        return jsonify({"ok": False, "error": "not a player script (size)"}), 400
    content = request.get_data(cache=False)
    try:
        r = add_player(content, "uploaded %s" % name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    return jsonify(dict(r, ok=True))


@app.route("/api/players/github", methods=["POST"])
def api_players_github():
    try:
        r = fetch_github_player()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 502
    return jsonify(dict(r, ok=True))


@app.route("/api/players/<sha>/delete", methods=["POST"])
def api_players_delete(sha):
    with store.lock:
        if sha not in store.players:
            return jsonify({"ok": False, "error": "no such version"}), 404
        users = [u.get("name") or uid for uid, u in store.units.items() if u.get("player") == sha]
        if users:
            return jsonify({"ok": False, "error": "still the target of %s - change that first"
                            % ", ".join(users[:5])}), 409
        p = store.players.pop(sha)
        store.save_players()
        _drop_unused_manual((p.get("manual") or {}).get("sha256"))
    try:
        os.unlink(store.player_path(sha))
    except OSError:
        pass
    logger.info("Player v%d deleted (%s)", p["version"], sha[:12])
    return jsonify({"ok": True})


@app.route("/api/players/<sha>/manual", methods=["POST"])
def api_players_manual(sha):
    """Attach a manual PDF to a player version by hand - for a manager that
    cannot reach GitHub itself (raw body, X-Filename ending .pdf)."""
    name = clean_filename(request.headers.get("X-Filename", ""), (".pdf",))
    length = request.content_length or 0
    if not name:
        return jsonify({"ok": False, "error": "the manual is a .pdf file"}), 400
    if not 0 < length <= MANUAL_MAX_SIZE:
        return jsonify({"ok": False, "error": "not a manual (size)"}), 400
    try:
        attach_manual(sha, request.get_data(cache=False), "uploaded %s" % name)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 422
    return jsonify({"ok": True})


@app.route("/api/update", methods=["POST"])
def api_update():
    data = request.get_json(silent=True) or {}
    uids = _ids(data)
    sha = data.get("sha256") or None
    if not uids:
        return jsonify({"ok": False, "error": "choose at least one unit"}), 400
    with store.lock:
        if sha is not None and sha not in store.players:
            return jsonify({"ok": False, "error": "no such player version here"}), 400
        missing = [u for u in uids if u not in store.units]
        if missing:
            return jsonify({"ok": False, "error": "unknown unit %s" % missing[0]}), 400
        for uid in uids:
            if sha is None:
                store.units[uid].pop("player", None)
            else:
                store.units[uid]["player"] = sha
        store.save_units()
        version = store.players[sha]["version"] if sha else None
    logger.info("Player target %s for %d unit(s)", "v%d" % version if version else "cleared", len(uids))
    return jsonify({"ok": True})


# ══════════════════════════════════════════════════════════════════════════════
# UPDATING THIS MANAGER
# ══════════════════════════════════════════════════════════════════════════════
#
# The manager runs as its own user under ProtectSystem=strict: it cannot
# write its code or its unit, by design. So its page can only ASK. It writes
# one word, "check" or "update", to <data>/self-update-request;
# saw-manager-update.path sees it and starts saw-manager-update.service, a
# root job (self-update.sh) that fetches the root-owned checkout from its
# origin - the public dist repo - and runs install.sh, putting the previous
# version back if that fails. The manager restarts during the update; the
# page notices the new version when it is back.

def _self_request_path() -> str:
    return os.path.join(store.dir, "self-update-request")


def _iso_age(stamp) -> Optional[float]:
    try:
        return time.time() - calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError, OverflowError):
        return None


def self_view() -> dict:
    available = os.path.isfile(SELF_SOURCE)
    try:
        with open(os.path.join(SELF_OUT, "state.json")) as f:
            state = json.load(f)
        state = state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        state = {}
    try:
        with open(os.path.join(SELF_OUT, "update.log"), errors="replace") as f:
            log = [line.rstrip()[:300] for line in f.readlines()[-40:]]
    except OSError:
        log = []
    age = _iso_age(state.get("updated"))
    running = state.get("state") == "running" and (age is None or age < SELF_STALE_S)
    # A request written while the previous job's unit was still stopping can
    # be missed by the path unit, and would then block every later ask. One
    # that no job has picked up in SELF_PENDING_S is not "pending" any more:
    # asking again replaces the file, and that new file starts the job.
    try:
        pending = time.time() - os.path.getmtime(_self_request_path()) < SELF_PENDING_S
    except OSError:
        pending = False
    view = {"installed": MANAGER_VERSION, "available": available, "state": state,
            "log": log, "running": running, "pending": pending,
            "newer": isinstance(state.get("available"), int)
                     and state["available"] > MANAGER_VERSION}
    if not available:
        view["reason"] = ("This manager was installed from a copy, not with the online installer, "
                          "so it cannot update itself. Run the install line (or the Proxmox line) "
                          "once; from then on this button works.")
    return view


@app.route("/api/password", methods=["GET"])
def api_password():
    a = load_auth()
    return jsonify({"set": password_required(), "changed": a.get("changed") if a else None})


@app.route("/api/password", methods=["POST"])
def api_password_change():
    """{"current", "new"}. The current one is asked for even though the browser
    has just sent it: a page left open on someone else's screen should not be
    enough to lock everybody out."""
    data = request.get_json(silent=True) or {}
    if password_required() and not check_password(data.get("current")):
        return jsonify({"ok": False, "error": "the current password is not right"}), 403
    problem = password_problem(data.get("new"))
    if problem:
        return jsonify({"ok": False, "error": "the new password needs %s" % problem}), 400
    set_password(data["new"])
    logger.info("Password changed from the web page")
    return jsonify({"ok": True})


@app.route("/api/self")
def api_self():
    return jsonify(self_view())


@app.route("/api/self/<action>", methods=["POST"])
def api_self_action(action):
    if action not in ("check", "update"):
        abort(404)
    view = self_view()
    if not view["available"]:
        return jsonify({"ok": False, "error": view["reason"]}), 409
    if view["running"] or view["pending"]:
        return jsonify({"ok": False, "error": "an update job is already running"}), 409
    fd, tmp = tempfile.mkstemp(dir=store.dir, prefix=".self-update-")
    with os.fdopen(fd, "w") as f:
        f.write(action)
    os.replace(tmp, _self_request_path())
    logger.info("Manager %s asked for from the web page", "update" if action == "update" else "update check")
    return jsonify(dict(self_view(), ok=True))


# ══════════════════════════════════════════════════════════════════════════════
# STARTUP
# ══════════════════════════════════════════════════════════════════════════════

def init(data_dir: str = DATA_DIR):
    global store
    store = Store(data_dir)
    return store


def cli_set_password() -> int:
    """`saw_manager.py --set-password`: one line on stdin -> <data>/auth.json.
    What install.sh runs (as the service user), so the password never appears
    on a command line or in the settings file."""
    pw = sys.stdin.readline().rstrip("\n")
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        set_password(pw, DATA_DIR)
    except (ValueError, OSError) as e:
        print("password not set: %s" % e, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--set-password"]:
        sys.exit(cli_set_password())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    init()
    threading.Thread(target=sweeper, daemon=True, name="sweeper").start()
    logger.info("Pi Video Player Manager v%d (%s) starting on port %d, data in %s%s", MANAGER_VERSION, NAME,
                PORT, DATA_DIR, "" if password_required() else " - NO PASSWORD SET")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
