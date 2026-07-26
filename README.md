# Syncplay Tunnel

A GTK4 app that routes Syncplay and mpv through a remote machine over SSH — and
proves the traffic actually leaves from there before anything starts playing.

It fails closed: if the tunnel drops, playback stops rather than quietly
continuing from the wrong address.

```
┌──────────────────────┐        SSH SOCKS5        ┌──────────────────────┐
│  client machine      │ ───────────────────────► │  host machine        │ ──► internet
│  Syncplay + mpv      │      Tailscale 100.x     │  100.x.y.z           │
│  native or distrobox │                          │  the exit point      │
└──────────────────────┘                          └──────────────────────┘
```

Syncplay does not have to live in a container. The app scans this system and
every distrobox you have, reports which ones actually have Syncplay and mpv, and
lists the complete ones first.

---

## The leak this fixes

The obvious way to point mpv at a SOCKS proxy is this, and it does not work:

```bash
mpv --http-proxy=socks5://127.0.0.1:8080 ...
```

mpv hands `--http-proxy` straight to FFmpeg, and **FFmpeg's HTTP protocol only
speaks HTTP CONNECT.** It does not understand `socks5://`. It ignores the value
and streams direct — from the client's real IP. The yt-dlp half worked (yt-dlp
does support SOCKS), so link resolution went through the tunnel while the actual
video stream did not. That's the traffic the debrid account cares about.

This app runs a small HTTP CONNECT proxy locally that forwards over the SSH
SOCKS5 tunnel, then points mpv at that:

```bash
mpv --http-proxy=http://127.0.0.1:8118 \
    --ytdl-raw-options=proxy=socks5h://127.0.0.1:8080
```

Both halves now exit from the host. Nothing extra is installed on the host machine.
When the tunnel dies the bridge returns `502`, so mpv errors out instead of
quietly falling back to a direct connection.

---

## What "Check the route" actually does

1. Tailscale ping to the host.
2. `ssh -o BatchMode=yes host true` — confirms key auth without a password prompt.
3. Opens the SOCKS5 tunnel and the HTTP bridge.
4. Reads this machine's public IP **with proxies disabled** — the baseline.
5. Reads the public IP through SOCKS5.
6. Reads the public IP through the HTTP bridge — the path mpv will use.
7. SSHes into the host and asks it for **its own** public IP. This is ground truth.

It passes only if 5 and 6 agree, both differ from 4, and both equal 7. Any other
combination fails with the reason spelled out. With **Require a verified route
before launching** on (default), the launch button stays blocked until it passes.

---

## Two modes

A switcher in the title bar picks which end of the link this machine is.

**Client** — the machine that needs its traffic to come out somewhere else. It
lists every Tailscale peer with hostname, address, OS and online state, online
ones first. Pick one and it becomes the exit point. There's still a plain
address field for anything Tailscale doesn't know about.

**Host** — the exit point itself. No tunnel is opened, because traffic already
leaves from here; *Start watching* just launches Syncplay locally. The page
reports what a client needs to reach you:

- your Tailscale name and address
- whether anything is listening on your SSH port
- how many client keys are in `authorized_keys`
- **who is connected right now** — refreshed every 5 seconds from the
  established connections on your sshd, named from Tailscale where possible, so
  you can watch a client attach. The header pill shows the count too.
- the `user@address` line to hand over, with a Copy button

Peers with no IPv4 address are skipped, and an offline peer can still be
selected — the app just warns you the route check will fail until it wakes up.

---

## Authorising a client

When the route check hits `Permission denied (publickey)`, a dialog opens
instead of telling you to go and run a command. Type the host password once and
it copies your public key over. If you have no key at all it generates an
ed25519 one first.

The password is handled carefully:

- `ssh-copy-id` runs on a **pseudo-terminal** and the prompt is answered
  directly. It is not passed through `sshpass -p`, which would expose it in the
  process list to anyone else on the machine.
- It lives in a `bytearray` that is zeroed the moment the command finishes.
- It never reaches the config file, the log, the environment, or a command line.

Afterwards the app re-tests with `BatchMode=yes` to confirm the host really
stopped asking, so a key that copied but doesn't work is reported as a failure
rather than a success.

Three outcomes are distinguished, because they need different fixes: a wrong
password, a host that never asked for one (keys-only, or wrong username), and a
key that copied but still won't authenticate.

You can also open this any time from **Set up SSH key…** on the client page.

### What the key is allowed to do

Once the copy is confirmed working, the app goes back in over that key and
narrows it down, so the line on the host reads:

```
restrict,port-forwarding ssh-ed25519 AAAA... client@laptop
```

`restrict` turns off pty allocation, agent and X11 forwarding and user rc files;
`port-forwarding` puts back the one thing the tunnel needs. Without this, an
enrolled key is a **full shell** on the host for anyone who ends up holding the
private key — the client's owner, root on that machine, or whoever picks up a
stolen laptop.

A forced `command=` is deliberately not used: route check 2 runs `ssh host true`
and check 7 asks the host for its own public IP, and a forced command would
break the strongest half of the verification.

Only the line holding your own key is touched, and only when it has no options
yet, so re-running changes nothing and hand-written entries are left alone.

Keys enrolled before this existed stay as they were. The Host page counts them —
*"2 client keys authorised — 1 unrestricted (full shell)"* — and offers
**Restrict existing keys**, which adds the same options to every unrestricted
line, drops nothing, and keeps the old file as `authorized_keys.bak`.

Worth doing on the host as well, since the app can't see either:

```
# /etc/ssh/sshd_config
ListenAddress <your-tailscale-IP>
AllowUsers <your-user>
```

The host-mode SSH row is only a TCP probe of the port — it cannot tell you which
interface sshd is bound to.

---

## Where to play

On startup the app scans, in the background:

- **This system** — `syncplay` and `mpv` on `PATH`, and failing that, Flatpak.
  A Flatpak is matched by app ID, using the last component for mpv so that
  something like `org.mozilla.firefox` can never be mistaken for it.
- **Every distrobox** — parsed from `distrobox list`.

Running containers are checked through `podman exec`, which costs nothing.
Stopped ones are left alone and shown as *not scanned*, because looking inside
one means starting it. Turn on **Start stopped containers while scanning** if you
want them checked too. The container you are sitting in is probed directly, so
it needs no container manager at all.

### Running the app from inside a container

There is no `distrobox` binary inside a distrobox, so the listing goes out
through `distrobox-host-exec`. Three cases:

| Situation | What you get |
|---|---|
| On the host | every container |
| Inside, with `distrobox-host-exec` | every container |
| Inside, without it | the current container only, and the log says why |

If you only ever see *This system*, check the Activity log — it names which of
these applies. `distrobox-host-exec` ships with distrobox's container setup; if
it's absent, run the app on the Silverblue host instead.

Environments are a scrolling list, built the same way as the host picker: plain
rows in a `Gtk.ListBox`, no custom item factory, no popover. Every environment is
visible at once with its status, and the list scrolls once there are more than a
few. Results are sorted so environments with both Syncplay and mpv come first,
with this system winning ties. Your choice is saved and re-selected on the next
run, as long as it still exists.

Under the list is the status of whatever is selected. If something is
missing, an **Install** button appears and puts it in *that* environment:

| Where | How |
|---|---|
| a distrobox | `sudo -n` inside the container — distrobox gives you passwordless sudo, so nothing prompts |
| a normal system | `pkexec` with pacman/apt/dnf/zypper — one polkit dialog |
| Silverblue and other rpm-ostree systems | `flatpak install --user` of `pl.syncplay.Syncplay` and `io.mpv.Mpv` — no root, no reboot, and the Flatpak detection picks the result up |

Output goes to the Activity log and a rescan runs when it finishes. If none of
those routes exist, the exact command to run yourself is logged.

### Flatpak Syncplay

If the chosen Syncplay is a Flatpak, proxy settings are passed as `--env=` flags
and the mpv wrapper calls `flatpak run` too. One caveat: the sandbox may refuse
a player path in `~/.local/bin`. If Syncplay complains it can't find the player:

```bash
flatpak override --user --filesystem=home pl.syncplay.Syncplay
```

The app prints this hint in the Activity log whenever you launch a Flatpak
Syncplay.

---

## Play a URL for everyone

**What to play** takes a URL, in either mode. It is handed to Syncplay as its
positional file argument, which lands on the **shared playlist** — so it starts
for everybody in the room, no matter which of you typed it. Only one side has to.

It also gets rid of Syncplay's setup dialog. That dialog appears when
`forceGuiPrompt` is true in `~/.config/syncplay.ini` **or** when Syncplay was
given no file at all, so both halves have to be handled:

- **Skip Syncplay's setup dialog** (Advanced, on by default) sets
  `forceguiprompt = False` in Syncplay's own ini, leaving every other key alone.
- The URL supplies the missing file argument.

With no URL the dialog comes back, whatever the switch says — Syncplay forces it
and there is no flag to override that.

**Trust the domain of the URL being played** (Advanced, on by default) appends
the URL's hostname to Syncplay's `trustedDomains`. Without it, Syncplay refuses
to switch to a URL outside `youtube.com`/`youtu.be` without a confirmation. This
only covers *this* machine — the other side confirms once per domain unless they
run this app with the same URL.

One side effect worth knowing: Syncplay saves the player path it was launched
with, so a later plain `syncplay` run also uses `~/.local/bin/mpv-proxied` and
will fail while no tunnel is up. The app logs this when it happens.

---

## Install

### Recommended: native (all three targets have GTK4 already)

```bash
chmod +x install.sh
./install.sh
```

Installs to `~/.local/bin` and `~/.local/share/applications`. No root, nothing
written to Silverblue's immutable base. Run it in whichever place you want the
launcher: a Silverblue host, inside a distrobox, or on any normal distro.

If the app's own dependencies (PyGObject with GTK4, `ssh`, `curl`) are missing,
the script prints the exact command and offers to run it with sudo, then
re-checks. On rpm-ostree systems it refuses to layer anything by itself, because
that needs a reboot — it prints the `rpm-ostree` line instead. Syncplay and mpv
are deliberately not its business: they belong to whichever environment you
watch in, and the app installs those from **Where to play**.

If you install it inside a container, it calls `distrobox-export` so the
launcher shows up in the host's app menu.

**Dependencies**, if the script reports any missing:

| System | Command |
|---|---|
| CachyOS / Arch | `sudo pacman -S --needed python-gobject gtk4 openssh curl` |
| Ubuntu / Debian container | `sudo apt install -y python3-gi gir1.2-gtk-4.0 libgtk-4-1 openssh-client curl` |
| Fedora Silverblue | already present in the base image |

### AppImage

```bash
distrobox enter <your-ubuntu-box> -- bash build-appimage.sh
```

Build it **inside the oldest-glibc container you have**, not on a rolling distro. AppImages only run
forward across glibc versions, so an Arch-built one will not start on Fedora.
The container's older glibc gives you a binary that runs everywhere.

Output: `SyncplayTunnel-x86_64.AppImage`. Needs network on first run to fetch
linuxdeploy.

Honestly: the native install is less fragile. GTK4 AppImages are finicky and all
current distributions already ship GTK4. Use the AppImage only if you want one
file to hand to someone.

---

## Setup, once

On the **client** (on the host system or inside a distrobox — `$HOME` is shared,
so it only needs doing once):

```bash
ssh-keygen -t ed25519          # if there's no key yet
ssh-copy-id user@100.x.y.z    # the host's Tailscale address
```

The app uses `BatchMode=yes`, so it will never sit there waiting on a password
prompt you can't see. If the key isn't set up, the SSH row tells you to run
exactly that command.

On the **host machine**: sshd running, Tailscale up. Nothing else.

---

## Using it

1. **Pick your mode** in the title bar, then choose a host from the peer list
   (Client) or check the reported details (Host).
2. **What to play** — optional. A URL here starts for both of you and skips
   Syncplay's setup dialog.
3. **Where to play** — pick the system or container from the list. *Rescan*
   re-checks, and *Install* fills in whatever is missing there.
4. **Check the route** — watch the seven rows. Client mode only; the host
   has nothing to route through.
5. **Save settings** — written to `~/.config/syncplay-tunnel/config.json`, mode
   `600`. Because distrobox shares `$HOME`, the container and the Silverblue host
   read the same file automatically.
6. **Start watching** — writes the mpv wrapper, opens the tunnel, starts the
   watchdog, launches Syncplay.

Quick launch: the desktop entry has a **Check route and start watching** action
(right-click the icon), or `syncplay-tunnel --launch`. It runs the check and
only launches if it passes.

---

## Advanced settings

| Setting | Default | Notes |
|---|---|---|
| SOCKS5 port | 8080 | Used by yt-dlp and `ALL_PROXY`. |
| HTTP bridge port | 8118 | Used by mpv/FFmpeg and `http_proxy`. |
| Start stopped containers | off | Scanning a stopped container requires starting it. |
| Watchdog interval | 10 s | How often the tunnel is tested. |
| Failures before stopping | 3 | Consecutive failures before everything is killed. |
| Extra mpv flags | — | Appended to the wrapper, e.g. `--cache=yes --demuxer-max-bytes=200M`. |
| Require a verified route | on | Leave this on. It's the whole point. |
| Stop the container on drop | on | Shuts the distrobox down too when the tunnel dies. |
| Skip Syncplay's setup dialog | on | Writes `forceguiprompt = False` to `~/.config/syncplay.ini`. Only takes effect when a URL is set. |
| Trust the domain of the URL | on | Adds the URL's hostname to Syncplay's `trustedDomains`, on this machine only. |

Syncplay server, room, and display name are optional — leave them blank to use
Syncplay's own saved settings.

---

## When the tunnel drops

Three consecutive failed checks and the app kills Syncplay and mpv, stops the
container if you launched into one, tears down the tunnel,
and sends a desktop notification. That is the fail-closed contract,
with the extra guarantee that the HTTP bridge refuses connections the moment the
SOCKS side is gone.

Logs: `~/.local/share/syncplay-tunnel/session.log`, plus the live Activity pane.

---

## Notes

- The tunnel dials outward, so nothing needs port-forwarding on either end.
  The client's own address is recorded for reference only.
- `distrobox` runs with `--network host`, which is why `127.0.0.1:8080` inside
  the container is the same socket the app opened on the host. If you ever
  switch the container to a private network, both ports need republishing.
- `$HOME` is shared between the host and every distrobox, so the mpv wrapper is
  written once to `~/.local/bin/mpv-proxied` and is visible everywhere. Its
  contents are rewritten on each launch to match the chosen environment.
- Playback is killed by process group, not by `pkill -f syncplay` — that pattern
  would have matched `syncplay-tunnel` itself.
- No pip packages. Everything is stdlib plus PyGObject.
