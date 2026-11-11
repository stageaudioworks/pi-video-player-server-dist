# Stage Audio Works video player — central manager

One web page for a whole installation of Stage Audio Works Raspberry Pi video
players:

- **Every player, live** — Looping, Playing trigger 2, Override, Paused, No screen,
  Offline — with its health (temperature, under-voltage, free space), in groups.
- **Remote controls** for one player, a group or all of them: Override on/off,
  Restart loop, Play, Pause, Triggers 1–3, Volume. They act within a second.
- **Content**: upload a clip once, send it to any slot on any players. Each player
  fetches it, checks it and swaps it in; a download cut short carries on where it
  stopped; a player that is off catches up when it is back.
- **Software**: keep players up to date from here, for sites where they have no
  internet access of their own - the user manual goes with each version (players
  v25 and later) - and update the manager itself from its own page.
- **Open web UI** on any player, for everything else.

It runs on a **Raspberry Pi** or a **Linux machine or VM**: Raspberry Pi OS
Bookworm or later, Debian 12+, Ubuntu 22.04+ (Fedora on a best-effort basis) -
anything with systemd, Python 3.9+ and Flask 2.0+. A Pi 4 or 5 is plenty; the
clips live on its card or disk, so give it room for the library. Players need
software **v23 or later**.

## Install

On the machine that will be the manager, with internet access:

```bash
curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/bootstrap.sh | sudo bash
```

It installs Python's Flask if needed, **asks you to choose a password** for the
web pages (type it twice; press Enter instead for a random one, shown once), sets
the manager up as a service on port **5080**, and prints the dashboard's address.
Log in with any user name and that password. Run the same command again to update.

**The password** can be changed on the dashboard: Software → Password. It is
stored hashed, never in plain text. Forgot it? On the manager:
`sudo bash /opt/saw-manager-src/install.sh --password` (asks for a new one).
Installed by automation, with no terminal to ask at, a random password is made and
printed instead.

Options go after `-s --`:

```bash
curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/bootstrap.sh | sudo bash -s -- --port 8080 --name "Museum"
```

| Option | |
|---|---|
| `--port N` | listen on another port (default 5080) |
| `--name "..."` | the name the players show for this manager (default: the hostname) |
| `--password` | choose a new password (asks; needs a terminal) |
| `--new-password` | make a random password and print it |
| `--password-file F` | the password from file F, which is removed after reading (for automation) |
| `--no-password` | first install only: no password (anyone on the network can control the players) |
| `--uninstall` | remove the service and code; keeps the library and settings |
| `--uninstall --purge` | remove everything |

### On Proxmox VE, as a container

In the Proxmox host's **Shell** (not inside a container):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/proxmox-lxc.sh)
```

It **asks for the password first**, right there on the host (Enter for a random
one), then creates an unprivileged Debian 12 container - 1 core, 512 MB, 32 GB for
the clip library, starting with the host - installs the manager in it, and prints
the dashboard's address. The password goes into the container as a file the
installer reads and deletes, never on a command line. The container is tagged
`saw-manager`, and its Notes say where the dashboard is. **Run the same line again
to update it**: it finds the container by its tag. Forgot the password? Run the
line again with `NEW_PASSWORD=1` in front.

If the container cannot reach the internet, it says which part fails - no
address, no gateway (usually the wrong bridge or VLAN) or no DNS - and how to fix
that in place; then run the line again and it carries on in the same container.

Settings go in front, e.g. a fixed address (recommended - the players are given
this address) on VLAN 20:

```bash
IP=192.0.2.80/24 GW=192.0.2.1 VLAN=20 bash <(curl -fsSL https://raw.githubusercontent.com/stageaudioworks/pi-video-player-server-dist/main/proxmox-lxc.sh)
```

`CTID`, `HN` (hostname), `BRIDGE`, `VLAN`, `IP`/`GW`, `STORAGE`, `TEMPLATE_STORE`,
`DISK` (GB), `CORES`, `RAM` (MB), `DEBIAN_VERSION` (12 or 13), `PORT`, `NAME`,
`NO_PASSWORD=1`, `NEW_PASSWORD=1`. The library lives inside the container, so Proxmox backups include
it. Installing by hand in a container you made yourself also works - give it
**nesting** (Options → Features), which the service's sandbox needs.

### Without internet

Copy this repository's files across and run `sudo bash install.sh`
with the same options (it needs Flask already installed: `python3-flask`).

If the machine has a firewall, open the port: `sudo ufw allow 5080/tcp`. The
installer says so when it sees one.

## Join a player

On the player's own web page, **Central Manager** panel: enter the manager's IP
address (and `:port` if it is not 5080), press **Join**. It appears on the
dashboard within 10 seconds. That is all — nothing is set up on the manager.

The player does all the calling, so it works through NAT and firewalls that allow
outbound connections. **Open web UI** is the one thing that needs your browser to
reach the player directly.

## Good to know

- **The manager's clip wins** in a slot it manages. Uploading something else
  there through the player's own page is undone within 10 seconds. Set the slot to
  "Not managed" to hand it back.
- **Commands are never late.** A command waits at most 30 seconds; a player that
  was offline does not play a trigger when it comes back.
- **Software** never goes backwards: a player installs only a newer version than it
  runs, never while a trigger is playing, and restarts with it (about 10 seconds
  without video).
- **A reinstalled player** keeps its identity (its Pi's serial number) but makes a
  new key, and the manager refuses it. Open it on the dashboard, **Forget this unit**,
  and it joins again on its own.
- **Groups** are set on the manager (Manage → Group).
- **Ports:** the manager's pages and the players' reports both use **5080**
  (`--port` / `PORT` to change it). Each player's own web UI stays on 5000.
- **Updating the manager:** Software → This manager → **Check for updates**, then
  **Update**. It fetches the published release, keeps settings, library and units,
  restarts (a few seconds; the players carry on), and puts the previous version back
  if the new one will not install. Running the install line again does the same.
  A manager installed from a copied folder rather than the install line cannot
  update itself until the install line has been run once.
- **Diagnostics:** `sudo systemctl status saw-manager`, `journalctl -u saw-manager -f`.


## Licence

GPL-3.0-or-later. See `LICENSE`.
