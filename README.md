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

It also finds what to watch: search a series, pick episodes, and it resolves them
through the tunnel onto Syncplay's shared playlist, so neither of you copies a
link again.

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

A switcher at the top of **Route** picks which end of the link this machine is.

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

## Finding episodes without leaving the app

**Browse…**, next to the URL field, replaces the loop of opening Stremio, letting
the Torrentio addon put an episode on the debrid server, copying the link out,
unrestricting it and pasting it into Syncplay's playlist.

Search a series, pick a season, tick one episode or a dozen, and the app looks up
sources for each of them. It picks the best one itself — already on the debrid
server first, then your preferred quality, then seeders — and shows you what it
chose, with **Change…** on any row to see the full list for that episode:
quality, size, seeders, provider, and whether it is ready or would need
downloading first. **Add to playlist** resolves them and fills in the URL field.

Metadata comes from [Cinemeta](https://v3-cinemeta.strem.io) and sources from
[Torrentio](https://torrentio.strem.fun) — the same two addons Stremio itself
uses, spoken directly over plain JSON.

### When the source addon is having a bad day

Torrentio sits behind Cloudflare, which answers `522` when it cannot reach the
addon itself. A failed lookup is retried three times with a growing pause before
it gives up, so one bad moment does not end a whole queue, and the log names the
status rather than blaming the JSON. A `404` is not retried — that one means what
it says.

If everything fails at once, the service is down — and the app then asks your
debrid account directly. Anything you have already watched or already have a
torrent for is still reachable without a torrent index at all: matching files in
your downloads are offered as-is, and a matching file inside a torrent you
already hold is unrestricted on the spot. Those come back marked *already on
Real-Debrid* and skip resolving entirely, because they are final links.

Matching is loose on the release name and strict on the numbering — `S04E01` or
`4x01`, with the series name having to appear — so the right episode plays or
none does.

### One link, one address

Each episode is resolved **once, through the tunnel**, and the resolved link is
what goes on the shared playlist. Both of you then fetch the identical URL
through your own tunnels to the same exit, so the debrid account sees one link
being fetched from one address. Resolving separately on each machine would defeat
that, which is why the app refuses to resolve at all while the tunnel is down and
**Require a verified route** is on.

In **Host** mode there is no tunnel and none is wanted: that machine already *is*
the exit point, so links are resolved straight from it. Either of you can fill
the playlist, whichever end you are.

Resolution is not instant — Torrentio has to get a link back from the debrid
service, which measured anywhere from a couple of seconds to about ninety even
for a source marked ready. A queue is resolved a few episodes at a time so a
season does not take longer than an episode.

### Queueing more than one

Syncplay's command line takes exactly one file, so only the first episode can be
handed over at launch. The rest are pushed to the room afterwards, over Syncplay's
own protocol, once the app's Syncplay has joined — a playlist set in an empty room
is discarded by the server, so the timing is deliberate. This needs **Syncplay
server** and **Room** set in Advanced; without them only the first episode plays,
and the app says so.

Syncplay caps a playlist at 250 items and 10000 characters total. Debrid links run
about 140 characters, so roughly 70 episodes fit. Past that the app refuses with
the count rather than letting the server drop it silently.

Every queued episode's domain is added to Syncplay's `trustedDomains` up front,
because a season can span more than one debrid host and an untrusted one
interrupts playback with a confirmation halfway through.

### The API key

**Real-Debrid API key** in Advanced, from
[real-debrid.com/apitoken](https://real-debrid.com/apitoken). It is stored in
`~/.config/syncplay-tunnel/config.json`, which the app writes owner-only, and it
grants full access to that account.

The key travels inside the Torrentio URL, exactly as it does in the Stremio addon
today — so Torrentio's server can see it and touches the debrid account from its
own address. Nothing here changes that; it is the same arrangement already in use.
What the app does guarantee is that the key never reaches the activity log or the
log file: anything that might carry one of those URLs is redacted first.

---

## Remembering things

Two files sit next to the log in `~/.local/share/syncplay-tunnel/`, both written
owner-only.

**`cache.json`** keeps what Cinemeta and Torrentio answered, so re-opening a
series you looked at a minute ago is instant instead of another round trip.
Searches are kept for a week, episode lists for a day, and source lists for six
hours — shortest, because what the debrid server already holds changes, and new
releases appear. The Refresh button beside the search box ignores all of it and
asks again, so a stale list is never a dead end.

Resolved debrid links are **not** cached. They expire, and a stale one fails in
the middle of an episode.

The Real-Debrid key travels inside Torrentio's URL, so a cached source list would
otherwise write it to disk. Stored entries keep the key replaced by a
placeholder and it is put back on the way out — the file never contains it.

**`history.json`** is the last 30 series, most recent first. Each one appears
under *Continue watching* with the episode you reached and a **Resume** button
that opens the browser at the next one. Both files can be emptied from **Setup ▸
Stored data**, which also shows how much is in them.

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
| CachyOS / Arch | `sudo pacman -S --needed python-gobject gtk4 libadwaita openssh curl` |
| Ubuntu / Debian container | `sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-1 openssh-client curl` |
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

The window has a sidebar with five places to be. **Start watching** and
**Stop session** sit under all of them, because they are the point.

1. **Route** — pick Client or Host at the top. As a client, choose a host from
   the Tailscale list; as a host, check the reported details. Then **Check the
   route** and watch the seven rows. Client mode only; a host has nothing to
   route through.
2. **Where** — pick the system or container to launch from. *Rescan* re-checks,
   and *Install missing* fills in whatever that environment lacks.
3. **Setup** — the Real-Debrid key, Syncplay server and room, ports, and the
   safety switches. **Settings save themselves** a moment after you change them,
   to `~/.config/syncplay-tunnel/config.json`, mode `600`. Because distrobox
   shares `$HOME`, the container and the host read the same file.
4. **Watch** — *Continue watching* lists what you have been through, each with
   **Resume**, which opens the browser at the next episode. **Browse…** finds
   something new. Run *Check the route* first: links are resolved through the
   tunnel.
5. **Start watching** — writes the mpv wrapper, opens the tunnel, starts the
   watchdog, launches Syncplay.

**Activity** holds the running log, with Copy and Clear.

Quick launch: the desktop entry has a **Check route and start watching** action
(right-click the icon), or `syncplay-tunnel --launch`. It runs the check and
only launches if it passes.

---

## Setup

| Setting | Default | Notes |
|---|---|---|
| SOCKS5 port | 8080 | Used by yt-dlp and `ALL_PROXY`. |
| HTTP bridge port | 8118 | Used by mpv/FFmpeg and `http_proxy`. |
| Start the environment I used last | on | Brings that container up when the app opens. Lives on **Where**. |
| Start stopped containers | off | Scanning every stopped container means starting all of them. Lives on **Where**. |
| Failures before stopping | 3 | Consecutive failures before everything is killed. |
| Extra mpv flags | — | Appended to the wrapper, e.g. `--cache=yes --demuxer-max-bytes=200M`. |
| Watchdog interval | 10 s | How often the tunnel is tested. |
| Real-Debrid API key | — | From [real-debrid.com/apitoken](https://real-debrid.com/apitoken). Needed by **Browse…**; kept out of every log. |
| Torrentio options | `sort=qualitysize` | Pipe-joined, e.g. `sort=seeders\|qualityfilter=480p,scr,cam`. A `realdebrid=` here is replaced by the key above. |
| Preferred quality | `1080p` | Matched exactly first, so it won't settle for `1080p 3D SBS` while a plain 1080p release exists. |
| Require a verified route | on | Leave this on. It's the whole point — it also blocks link resolution while the tunnel is down. |
| Stop the container on drop | on | Shuts the distrobox down too when the tunnel dies. |
| Skip Syncplay's setup dialog | on | Writes `forceguiprompt = False` to `~/.config/syncplay.ini`. Only takes effect when a URL is set. |
| Trust the domain of the URL | on | Adds the URL's hostname to Syncplay's `trustedDomains`, on this machine only. |

Syncplay server, room, and display name are optional — leave them blank to use
Syncplay's own saved settings. **Queueing more than one episode needs the server
and room filled in**, because the queue is pushed to that room over Syncplay's
protocol after launch.

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
