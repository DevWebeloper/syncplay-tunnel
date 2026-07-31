# Syncplay Tunnel — Handoff

**Status:** feature-complete, syntax- and unit-tested, **never run on real hardware**
**Last updated:** 31 July 2026

---

## 1. Context

Two people watch things together over Syncplay. One machine must not expose its
own IP to the streaming/debrid service, so all HTTP(S) traffic from Syncplay and
mpv has to exit from the other machine.

| Role | Machine | Notes |
|---|---|---|
| **Host / exit point** | CachyOS (Arch) laptop | Reached on its Tailscale address. An ordinary laptop that someone also uses — not a separate server. |
| **Client** | Fedora Silverblue laptop | Syncplay + mpv originally in a distrobox container. |
| **Link** | Tailscale | `100.x` CGNAT range. No port forwarding anywhere. |

The reason this matters: a debrid account flagged for IP mismatch is the failure
mode being designed against. That is why the system **fails closed** — if the
tunnel drops, playback stops rather than silently continuing from the wrong
address.

---

## 2. Behaviour fixed up front

Settled before any code was written, because they decide the shape of everything
else:

- the tunnel is `ssh -N -D <port>`, dialled outward, so neither end needs port
  forwarding
- mpv is launched through a generated wrapper rather than configured by hand
- a watchdog tests the tunnel every 10 s; 3 consecutive failures tear everything
  down, notification included
- the container is stopped along with it, when one was used

That is the fail-closed contract. The one part that looked right and was not is
in §4.

---

## 3. Requirements as gathered

Collected across two rounds of questions:

| Question | Answer |
|---|---|
| Build & ship | Python + GTK4 → AppImage |
| What is the "clients" field | Tailscale IP of the client laptop |
| Where does the GUI run on Silverblue | Either — host **and** inside the container |
| Is the host machine the CachyOS laptop | Yes, same machine |
| What should "Test connection" prove | That traffic exits via the host machine and nowhere else |
| Extra config | No preference |

Added later in the same conversation:

- Must run **natively**, not only in a container
- Detect available distroboxes, list them, and **prioritise ones that actually
  have Syncplay and mpv**
- Detect whether the host system itself has Syncplay and mpv
- **Two modes** — "I'm the host" (uses its own IP) and "I'm the client" (picks
  from a list of available hosts)
- Scan and list Tailscale machines
- A **confirmation dialog with a password field** for `ssh-copy-id`, because the
  key-refusal error was being hit often
- Fix the environment selector on Fedora Silverblue

---

## 4. The important finding: mpv was leaking

The obvious wrapper is this, and it leaks:

```bash
mpv --http-proxy=socks5://127.0.0.1:8080 \
    --ytdl-raw-options=proxy=socks5://127.0.0.1:8080
```

**mpv passes `--http-proxy` straight to FFmpeg, and FFmpeg's HTTP protocol only
speaks HTTP CONNECT.** It does not understand `socks5://`, ignores the value, and
streams direct — from the client's real IP.

yt-dlp *does* support SOCKS, so link resolution went through the tunnel while the
actual video stream did not. The video stream is precisely the traffic the debrid
account cares about.

### The fix

A local HTTP CONNECT proxy, written in stdlib Python (~150 lines), listening on
`127.0.0.1:8118` and forwarding over the SSH SOCKS5 tunnel:

```bash
mpv --http-proxy=http://127.0.0.1:8118 \
    --ytdl-raw-options=proxy=socks5h://127.0.0.1:8080
```

- Nothing extra is installed on the host machine (no tinyproxy/privoxy)
- Hostnames go to the far side unresolved (`socks5h` semantics), so DNS doesn't
  leak either
- When the SOCKS side is gone the bridge answers **502**, so mpv errors out
  instead of quietly falling back to a direct connection

Implementation: `socks5_connect()` (raw SOCKS5, no auth, ATYP 0x03),
`_BridgeHandler` (handles both `CONNECT` and absolute-form requests),
`_relay()` (bidirectional `select` pump), `HttpBridge` (threading TCP server).

---

## 5. Route verification

Seven checks, run in order. This is the "prove it goes through the host machine"
requirement.

| # | Check | Method |
|---|---|---|
| 1 | Host reachable on Tailscale | `tailscale ping -c 1` |
| 2 | SSH accepts our key | `ssh -o BatchMode=yes … true` |
| 3 | Tunnel listening | SOCKS5 port + HTTP bridge come up |
| 4 | **This machine's own public IP** | `curl --noproxy '*'` — the baseline |
| 5 | Public IP through SOCKS5 | `curl --socks5-hostname` |
| 6 | Public IP through the **mpv path** | `curl -x http://127.0.0.1:8118` |
| 7 | **The host's own public IP** | `ssh host curl …` — ground truth |

**Passes only if** 5 and 6 agree, both differ from 4, and both equal 7.

Each failure mode gets its own message: same-as-baseline is a leak, 5≠6 means the
mpv path diverges from the tunnel, 5≠7 means whatever is exiting is not the host.

Check 6 is the one that matters most — it is the path mpv will actually use, and
it is what would have caught the bug in §4.

`BatchMode=yes` is used throughout so SSH can never sit waiting at an invisible
password prompt. Echo services are tried in order: ipify → ifconfig.me →
icanhazip.

---

## 6. Environment detection

Syncplay can live in three places. All are scanned in the background on startup.

- **This system** — `syncplay` and `mpv` on `PATH`; failing that, Flatpak
- **Every distrobox** — parsed from `distrobox list` (ANSI codes stripped)

Ranking: environments with **both** Syncplay and mpv sort first, native winning
ties. The saved choice is re-selected on the next run if it still exists.
Incomplete environments can be selected but not launched — the app names which of
the two is missing and where.

**Stopped containers** are left alone and shown as *not scanned*, because looking
inside one means starting it. There is a switch to opt into that. Running
containers are probed with `podman exec` (instant). The container the app is
*sitting in* is probed directly — no container manager needed.

**Flatpak matching** uses the last component of the app ID for mpv, so
`com.example.mpvtools` cannot be mistaken for it. If the chosen Syncplay is a
Flatpak, proxy settings are passed as `--env=` flags and the mpv wrapper calls
`flatpak run`. The sandbox may reject a player path in `~/.local/bin`; the app
logs the `flatpak override --user --filesystem=home` fix when relevant.

### Running from inside a container

**There is no `distrobox` binary inside a distrobox.** The listing therefore goes
out through `distrobox-host-exec`.

| Situation | Result |
|---|---|
| On the host | every container |
| Inside, with `distrobox-host-exec` | every container |
| Inside, without it | the current container only, and the log explains why |

The current container name is read from `$CONTAINER_ID`, falling back to parsing
`name=` out of `/run/.containerenv`.

---

## 7. Two modes

A switcher at the top of the **Route** view (`Gtk.Stack` + `Gtk.StackSwitcher`),
titled **Client** and **Host** — it lived in the title bar until §18. The stack child names stay `client`/`host`, so `cfg["role"]` and
every branch reading it are unaffected by the label change.

**Client** — lists every Tailscale peer with hostname, address, OS and
online state, online first. Peers with no IPv4 are skipped. Selecting an offline
peer is allowed but warns. A plain address field remains for anything Tailscale
doesn't know about.

**Host** — no tunnel is opened, because traffic already exits here;
*Start watching* launches Syncplay locally with no proxy. The page reports what a
client needs: Tailscale name and address, whether anything is listening on the
configured SSH port, how many keys are in `authorized_keys`, who is connected
right now, and the `user@address` line with a Copy button.

### Connected devices

`ssh_clients(port)` runs `ss -tnH state established '( sport = :<port> )'` and
takes the peer column, deduped — one machine holds several channels open. A 5 s
`GLib.timeout_add_seconds` drives it while the Host page is selected and stops
itself when the role changes; the `ss` call itself runs in a worker thread.
Addresses inside `100.64.0.0/10` are matched against the Tailscale peer list for
names, anything else is labelled *not Tailscale*. The count also goes in the
header pill.

---

## 8. SSH key enrolment

Triggered automatically when the route check hits `Permission denied
(publickey)`, and available any time from **Set up SSH key…**.

A dialog takes the host password once and installs the public key. If no key
exists at all, an ed25519 one is generated first.

**How the password is handled — this was deliberate:**

- `ssh-copy-id` runs on a **pseudo-terminal** (`pty.fork()`) and the prompt is
  answered directly
- **`sshpass -p` was rejected** — it puts the password in the process list where
  any other user on the machine can read it with `ps`
- The password lives in a `bytearray` that is zeroed the moment the command
  returns
- It never reaches the config file, the log, the environment, or a command line

Afterwards the app re-tests with `BatchMode=yes`, so a key that copied but still
doesn't authenticate is reported as a failure, not a success.

Three outcomes are distinguished because they need different fixes:

| Outcome | Meaning |
|---|---|
| Host rejected the password | Retype it |
| Host never asked for a password | Keys-only sshd, or wrong username |
| Copied but still won't log in | Something else is wrong server-side |

`-o PubkeyAuthentication=no` forces the password path, since we only get here
after key auth already failed.

### What the key may do once installed — added in §16.7

`ssh-copy-id` installs an **unrestricted** line, i.e. a full shell on the host for
whoever holds that private key. After the `BatchMode=yes` re-test passes,
`restrict_authorized_key()` goes back in over the key that was just proved to
work and rewrites our own line as:

```
restrict,port-forwarding ssh-ed25519 AAAA... client@laptop
```

Note the direction of all this: enrolment is one-way, client → host. The host
gets nothing on the client, and two clients enrolled on the same host cannot
reach each other.

---

## 9. Bugs found and fixed

Roughly in the order they surfaced.

| # | Bug | Fix |
|---|---|---|
| 1 | **mpv leaked** — FFmpeg ignores `socks5://` in `--http-proxy` | Local HTTP CONNECT → SOCKS5 bridge (§4) |
| 2 | `pkill -f syncplay` matches **`syncplay-tunnel` itself** — the app would kill itself | Kill by process group; `pkill -x mpv` as backup |
| 3 | `_stopping` latch never cleared — a second launch in one app run silently did nothing | `Session.begin()` |
| 4 | Log pane collapsed to ~1 character wide | `hexpand` + horizontal policy `NEVER` |
| 5 | `install.sh` container detection: `a \|\| b && c` under `set -e` exits when all false | Rewritten as `if`  |
| 6 | `ssh-copy-id` classified "denied, never prompted" as a wrong password | Check `not sent` **before** the denial regex |
| 7 | `list_distroboxes()` returned `[]` inside a container → nothing to select on Silverblue | Route through `distrobox-host-exec`; fall back to current container |
| 8 | `--launch` waited on the GTK main thread, freezing the window | All waiting moved into a worker thread |
| 9 | Subprocesses inherited stdin — a container probe could block forever | `stdin=subprocess.DEVNULL` everywhere |
| 10 | Flatpak substring match would treat `…mpvtools` as mpv | Match the last component of the app ID |
| 11 | CSS provider looked up the display via a nonsense expression | `Gdk.Display.get_default()` |
| 12 | Dead `finally` block and an unused `Pango` import | Removed |

**#7 was the reported Silverblue symptom.** The dropdown was not broken — it was
empty, which looks identical when clicked. `distrobox-export` makes it easy to
launch the container copy by accident, since the exported launcher looks the same
in the app menu.

The selector was also changed from `Gtk.DropDown` to a `Gtk.ListBox`: every
environment is visible with its status, and there is no popover to behave
differently across GTK builds.

**See §16.1 for the round trip this widget took** — dropdown, then list again.

---

## 10. Files

| File | Purpose |
|---|---|
| `syncplay-tunnel.py` | The whole application. Single file, no pip dependencies. |
| `install.sh` | Native install to `~/.local`. Detects Arch/Debian/Fedora/Silverblue and host vs container. |
| `build-appimage.sh` | AppImage build via linuxdeploy + gtk plugin. |
| `syncplay-tunnel.desktop` | Launcher, with a *Check route and start watching* action. |
| `syncplay-tunnel.svg` | Icon. |
| `README.md` | User-facing documentation. |

**Dependencies:** PyGObject with GTK4 **and libadwaita**, `ssh`, `curl`. Optional: `tailscale`,
`distrobox`, `podman`, `flatpak`, `notify-send`. Everything else is stdlib.

### Code layout inside `syncplay-tunnel.py`

```
helpers            which / in_container / run / notify / port_open / free_port
                   ssh_clients() / is_tailscale_addr()
Config             JSON at ~/.config/syncplay-tunnel/config.json, mode 600
JsonStore          atomic 0600 writes; Cache and History extend it
Peer, tailscale_status()
ensure_ssh_key(), ssh_copy_id()      pty-based enrolment
Runtime, probe_native(), list_distroboxes(), probe_container(), scan_runtimes()
install_plan(), stream_command()     installing syncplay/mpv per environment
syncplay_ini_path(), prepare_syncplay_ini()
redact(), curl_json(), curl_final_url()      library networking, via curl
Series, Episode, Source, parse_source()
cinemeta_search/_episodes, torrentio_url/_sources, pick_source()
SyncplayPush       joins the room, sets the shared playlist, leaves
socks5_connect(), _BridgeHandler, HttpBridge
Session            tunnel, watchdog, wrapper, launch, teardown
Row, clear_list(), list_row(), scrolled_list(), block_scroll_steal()
BrowseWindow       search / episodes / review / sources
Window, App        libadwaita UI: split view, five views, preferences page
```

---

## 11. Install

### Native (recommended)

```bash
chmod +x install.sh && ./install.sh
```

Installs to `~/.local/bin` and `~/.local/share/applications`. No root; nothing
touches Silverblue's immutable base. Run it wherever you want the launcher.
Inside a container it also calls `distrobox-export`.

If PyGObject/GTK4, `ssh` or `curl` are missing it prints the command and offers
to run it under sudo, then re-checks. rpm-ostree systems are never layered
automatically — that needs a reboot, so the command is printed instead. Syncplay
and mpv are **not** handled here; the app installs those into the environment you
picked (§16).

| System | Dependencies |
|---|---|
| CachyOS / Arch | `sudo pacman -S --needed python-gobject gtk4 openssh curl` |
| sync-ubuntu | `sudo apt install -y python3-gi gir1.2-gtk-4.0 libgtk-4-1 openssh-client curl` |
| Fedora Silverblue | already in the base image |

### AppImage

```bash
distrobox enter sync-ubuntu -- bash build-appimage.sh
```

**Build inside the Ubuntu container, not on CachyOS.** AppImages only run forward
across glibc versions — an Arch-built one will not start on Fedora. Needs network
on first run to fetch linuxdeploy.

The native install is less fragile; GTK4 AppImages are finicky and all three
systems already ship GTK4.

---

## 12. Configuration reference

`~/.config/syncplay-tunnel/config.json`, mode `600`.

| Key | Default | Meaning |
|---|---|---|
| `role` | `client` | `client` or `host` |
| `host_ip` | `""` | Exit machine address |
| `host_user` | current login | SSH user on the host |
| `host_ssh_port` | `22` | |
| `client_ip` | `""` | Recorded for reference only |
| `socks_port` | `8080` | Used by yt-dlp and `ALL_PROXY` |
| `http_port` | `8118` | Used by mpv/FFmpeg and `http_proxy` |
| `runtime_kind` | `auto` | `native` or `distrobox` |
| `container` | `""` | Selected container name |
| `scan_stopped` | `false` | Start stopped containers while scanning |
| `check_interval` | `10` | Watchdog period, seconds |
| `max_fails` | `3` | Consecutive failures before stopping |
| `syncplay_server` | `""` | Optional; blank uses Syncplay's own settings |
| `syncplay_room` | `""` | |
| `syncplay_user` | `""` | |
| `play_url` | `""` | Passed to Syncplay as its positional file; lands on the shared playlist |
| `skip_syncplay_dialog` | `true` | Writes `forceguiprompt = False` into Syncplay's ini. Needs `play_url` to have any effect |
| `trust_play_domain` | `true` | Appends `play_url`'s hostname to Syncplay's `trustedDomains` |
| `mpv_extra` | `""` | Appended to the wrapper |
| `require_verified` | `true` | **Leave on.** Blocks launch until the route passes, and blocks link resolution while the tunnel is down |
| `stop_container_on_drop` | `true` | Stops the distrobox when the tunnel dies |
| `rd_api_key` | `""` | Real-Debrid token. Travels inside the Torrentio URL; redacted from every log |
| `torrentio_opts` | `sort=qualitysize` | Pipe-joined Torrentio path options. A `realdebrid=` here is replaced by the key above |
| `preferred_quality` | `1080p` | Matched exactly first, then as a substring |
| `library_series_id` | `""` | Bookmark: last series browsed |
| `library_series_name` | `""` | Bookmark: its display name |
| `library_season` | `0` | Bookmark: last season |
| `library_episode` | `0` | Bookmark: last episode queued; the browser preselects the next one |

Logs: `~/.local/share/syncplay-tunnel/session.log`, plus the live Activity pane.

---

## 13. Things that are true about the environment

Worth knowing before changing anything:

- **`$HOME` is shared** between the Silverblue host and every distrobox. This is
  why one config file and one mpv wrapper at `~/.local/bin/mpv-proxied` serve
  every environment. The wrapper's *contents* are rewritten on each launch to
  match the chosen target.
- **distrobox runs with `--network host`**, which is why `127.0.0.1:8080` inside
  the container is the same socket the app opened on the host. Switching the
  container to a private network would require republishing both ports.
- The tunnel **dials outward**, so nothing needs port forwarding at either end.
- Flatpak apps share the host network namespace by default, so `127.0.0.1:8118`
  is reachable from inside a Flatpak.

---

## 14. Testing performed

All automated, all offline, using mock servers and fake binaries.

**HTTP bridge** — against a mock SOCKS5 server and a local origin server:

- plain HTTP through the bridge (absolute-form request) ✓
- `CONNECT` tunnelling, i.e. the HTTPS path ✓
- raw `socks5_connect()` ✓
- **502 when the SOCKS side is down** — the fail-closed guarantee ✓

**Scanner** — `distrobox list` parsing with realistic ANSI-coloured output;
ranking and ordering; Flatpak detection including the `mpvtools` negative case.

**Container discovery** — three cases: on the host; inside with
`distrobox-host-exec`; inside without it. Plus `/run/.containerenv` parsing and
the no-podman-needed self-probe.

**SSH enrolment** — against a fake `ssh-copy-id` that mimics the real prompt:
correct password, wrong password, and never-prompted.

**Tailscale parsing** — self + peers, online-first ordering, IPv6-only peers
skipped.

**UI** — headless construction with a GTK stub: window builds, role toggles both
ways, `collect()` survives missing widgets, launch guards fire for "no
environment" and "incomplete environment", selection plumbing reaches
`selected_runtime()`, host-mode command contains no proxy exports.

---

## 15. Open items

Ordered by how likely they are to bite.

1. **Nothing has been run on real hardware.** The GUI has never been displayed.
   Expect small GTK issues on first launch.
2. **`distrobox-export`** behaviour after `install.sh` inside the container is
   unverified.
3. **`ssh-copy-id` has only been tested against a fake binary.** The real one's
   prompt wording is matched by regex (`password|passcode|passphrase`) — worth
   confirming against a real host.
4. **The AppImage build has never been executed** (it needs network). The GTK4 +
   PyGObject bundling in `build-appimage.sh` is the most likely thing to need
   iteration.
5. **The Flatpak Syncplay app ID** in the override hint is written as
   `pl.syncplay.Syncplay`. Detection itself is generic (substring match), so
   detection will work regardless; only the printed hint could name the wrong ID.
6. **Host-mode sshd check** is a TCP probe of `host_ssh_port` (was hardcoded 22).
   It still won't notice sshd bound to the wrong interface.
7. **IPv6 exit addresses** pass the regex but the comparison logic has only been
   reasoned about for IPv4.
8. The watchdog's connectivity probe uses
   `connectivitycheck.gstatic.com/generate_204`, inherited from the original
   script. Fine, but it is a hardcoded third party.

### First-run checklist

1. Install on the Silverblue **host** (not the container) and confirm the
   Activity log's first line says *on the host system*.
2. Confirm the environment list shows both *This system* and *distrobox:
   sync-ubuntu* with correct status.
3. Run the route check without a key installed and confirm the password dialog
   opens by itself.
4. Complete enrolment, re-check, and confirm all seven rows pass and the verdict
   names the host's public IP.
5. Start watching, play something, then **pull Tailscale down on the host machine**
   and confirm playback stops within ~30 s with a notification.

Step 5 is the one that actually matters. Everything else is convenience.

---

## 16. Second round — after the first real use

Reported: on Silverblue the environment selector rendered as nothing (clicking
did nothing, while *Rescan* logged the containers correctly); on CachyOS it was
fine. Plus five requests, below.

### 16.1 Selector: dropdown, then a scrolling list again

First attempt at the Silverblue report was a `Gtk.DropDown` over a
`Gtk.StringList` — no `Gtk.SignalListItemFactory`, no expression, on the theory
that the old `ListBox`-in-a-`ScrolledWindow` drew at zero height. That theory was
never confirmed on Silverblue hardware.

**Current state, by request:** the selector matches *Pick a host to route
through* — a `Gtk.ListBox` (`SelectionMode.SINGLE`, `boxed-list`) inside a
`Gtk.ScrolledWindow` with `NEVER`/`AUTOMATIC` policy, `min_content_height=120`
and `max_content_height=220`, so it grows to a few rows then scrolls. Rows are
plain `Gtk.Label`s; incomplete environments get the `dim` class.

`_set_env_rows(found, placeholder, select)` is the single place rows are built —
it clears, refills, and selects by index. Each row carries `row.runtime`, so
`selected_runtime()` is just `getattr(row, "runtime", None)`; there is no index
arithmetic and no `Gtk.INVALID_LIST_POSITION` guard left (the constant was
deleted). An empty result leaves one non-selectable placeholder row, which is
why `selected_runtime()` still returns `None` and the launch guard still fires.

Underneath sits a detail label with the selected environment's status, plus the
install button (§16.3).

### 16.2 Mode labels

`I'm the client` / `I'm the host` → `Client` / `Host`. Stack **child names** are
untouched (`client`, `host`), so nothing that reads `cfg["role"]` changed.

### 16.3 Installing Syncplay and mpv

Deliberately not `install.sh`'s job — that script runs once, in one place, and
the thing that knows which environment you actually watch in is the app.

`install_plan(rt, missing)` returns an argv without running it:

| Target | Route | Why |
|---|---|---|
| distrobox | `distrobox enter <name> -- bash -lc 'sudo -n …'` | distrobox grants passwordless sudo, so no prompt is needed or possible |
| native + pacman/apt/dnf/zypper | `pkexec` | one polkit dialog, no terminal emulator to hunt for |
| native + rpm-ostree | `flatpak install -y --user flathub pl.syncplay.Syncplay io.mpv.Mpv` | layering needs a reboot; a user Flatpak needs neither, and `probe_native()` already detects Flatpak installs |

`stream_command()` pumps output into the Activity log line by line. A failed plan
returns `(None, reason, …)` and the reason is logged rather than guessed at.

### 16.4 URL playback for both sides

`play_url` is appended to the Syncplay command line as its positional `file`.
Syncplay does the rest: `client.py:852` turns a command-line file into
`delayedLoadPath` when `sharedPlaylistEnabled` (default), and `loadDelayedPath`
(`client.py:1856`) calls `addFileToPlaylist` → `changePlaylist` →
`_protocol.setPlaylist`, which is what pushes it to the whole room. Only one end
needs to enter it.

### 16.5 Why the setup dialog kept ignoring Advanced

`ConfigurationGetter.py:565`:

```python
if (self._config['forceGuiPrompt'] == "True" or not self._config['file']) and not self._config['noGui'] ...
```

Two independent triggers, so both had to go: `prepare_syncplay_ini()` writes
`forceguiprompt = False` into Syncplay's own ini, and `play_url` supplies the
file argument. **With no URL the dialog is unavoidable** — there is no flag for
it. That is a Syncplay constraint, not a missing feature here.

Editing that ini has three traps, all handled:

- Syncplay escapes `%` as `%%`, so `RawConfigParser` is mandatory — the default
  parser's interpolation raises on those values.
- The file is UTF-8 **with a BOM** (`codecs.open(..., "utf_8_sig")` in
  `_saveConfig`), so it is read and written as `utf-8-sig`.
- `trustedDomains` is stored as a Python list literal and read back with
  `ast.literal_eval` (`ConfigurationGetter.py:269`), so it is written with
  `repr()`.

`trust_play_domain` appends the URL's hostname to that list, because
`onlySwitchToTrustedDomains` defaults to True and otherwise the *other* party
gets a confirmation prompt instead of an automatic switch. It only covers this
machine — the other end confirms once per domain unless they run this app too.

Known side effect: Syncplay's `_saveConfig` persists the `--player-path` it was
given, so a later plain `syncplay` run uses `~/.local/bin/mpv-proxied` and fails
closed with no tunnel up. Logged at launch.

### 16.6 Testing this round

Same style as §14 — offline, no hardware:

- `prepare_syncplay_ini()` against a **copy of the real `~/.config/syncplay.ini`**
  (BOM and `%%` values present): flag flips, domain appended, `youtube.com` kept,
  every other key byte-identical, second run idempotent, and a missing file is
  created from nothing.
- `ssh_clients()` on captured `ss` output: multi-channel dedupe, non-Tailscale
  peer kept and labelled, bracketed IPv6, failure and empty cases.
- `syncplay_command()`: URL last and quoted, absent when unset, host mode still
  exporting no proxy.
- `install_plan()` for all three routes.
- The **real** window built on the real display and driven without being shown:
  role toggles both ways, `collect()` round-trips `play_url` and both switches,
  and the launch guard fires with nothing selected.
- The environment list, same way (`test_envlist.py`, 24 checks): scroller policy
  and height cap, placeholder before the scan, one row per runtime with matching
  labels, selecting each row in turn resolves to its runtime, dim class on
  incomplete rows, the detail text and install button for ready / half / neither
  / not-scanned, and an empty rescan going back to a placeholder with no
  selection.

Still unrun on hardware: the Silverblue render itself, and the install button on
a machine that is actually missing something.

### 16.7 Restricting the enrolled key

Question that prompted it: does enrolment give everyone SSH into everyone's
machine? No — it is one-way, client → host, and two clients on the same host
cannot reach each other. But the key was installed **unrestricted**, so it was a
full shell on the host for anyone holding it.

`KEY_OPTIONS = "restrict,port-forwarding"` is now applied in two places:

- `restrict_authorized_key(ssh_cmd, pub_path)` — after enrolment, over the key
  auth that was just verified, so no password is involved. An awk program
  prefixes only the line containing our own blob, and only when its first field
  is a key type (i.e. it has no options field yet). Exit 1 from awk's `END` means
  nothing needed changing, which the caller reports rather than treating as an
  error.
- `restrict_local_keys()` — the Host page's **Restrict existing keys** button, for
  keys enrolled before this existed. Adds options only, drops no line, writes
  `authorized_keys.bak`, and is behind a confirmation dialog because a mistake
  here locks clients out. The Host page's key row now counts unrestricted lines,
  and the button only appears when there are any.

Two things that matter in the remote snippet:

- It is wrapped in `sh -c`, because the remote **login shell is the host user's**
  — and on the host machine that is fish, which does not read POSIX `$(…)`/`[ ]`
  syntax the same way. Tested against sh, bash and fish.
- No forced `command=`. Route check 2 (`ssh host true`) and check 7
  (`ssh host curl …`, the ground truth) both need to run arbitrary commands.

Tests: the awk rewrite driven through all three shells with a fake `ssh`
(someone else's key untouched, comments kept, mode 600, idempotent,
hand-written `command=` lines preserved, missing file reported as failure), plus
the local retrofit over a file mixing open, already-restricted, forced-command
and `sk-` entries.

### 16.8 Application ID

`APP_ID` is `io.github.DevWebeloper.SyncplayTunnel`, matching the repository so a
future Flatpak can keep the same identity. The desktop entry and the icon keep
their short `syncplay-tunnel` names — renaming them would break an already
installed launcher and `distrobox-export` — so the entry carries
`StartupWMClass=io.github.DevWebeloper.SyncplayTunnel` instead, which is what
makes the compositor pair the window with the right icon.

Not covered by the app, worth setting on the host by hand: `ListenAddress
<tailscale-ip>` and `AllowUsers <your-user>` in `sshd_config` — §15.6 still stands,
the host-mode check is only a TCP probe and cannot see the bound interface.

---

## 17. Third round — the library browser

The manual loop this removes: open Stremio, let the Torrentio+RD addon put an
episode on the debrid server, copy the link, unrestrict it, paste it into
Syncplay's playlist. Repeat per episode. Stremio does nothing there that this app
cannot — both addons are plain JSON over HTTP.

### 17.1 Shapes confirmed against the live services

Not derived from documentation; captured and asserted against in the tests.

**Cinemeta** (`https://v3-cinemeta.strem.io`, no key):

- `/catalog/series/top/search=<q>.json` → `metas[]`, each `{id: "tt0903747",
  name, releaseInfo: "2008-2013", poster}`.
- `/meta/series/<id>.json` → `meta.videos[]`, each `{id: "tt0903747:1:2",
  season, episode, name, released}`. Breaking Bad returns 67 videos across
  seasons **0–5**; season 0 is specials and is dropped, leaving 62.

**Torrentio** (`https://torrentio.strem.fun/<opts>/stream/series/<id>:<s>:<e>.json`).
The debrid key changes the reply shape, which is why `parse_source()` accepts both:

| | without a key | with `realdebrid=<key>` |
|---|---|---|
| streams for `tt0903747:1:2` | 50 | 82 |
| identifier | `infoHash` + `fileIdx` | `url` (a `/resolve/realdebrid/<key>/<hash>/null/<idx>/<name>` endpoint) |
| cache marker | none | `[RD+]` cached / `[RD download]` not — 46 of 82 were cached |

`name` is `"<addon>\n<quality>"`; `title` is `"<torrent>\n<file>\n👤 seeders 💾 size ⚙️ provider"`.

**Resolution** — GET the `url` with `-r 0-0` (a HEAD gets 405 from some CDNs, and
without a range this downloads the episode). Lands on
`https://<n>-<n>.download.real-debrid.com/d/<id>/<filename>`, HTTP 206, ~141
characters.

### 17.2 Resolution is slow, and must not be parallelised

Measured, twice, on a source marked `[RD+]`: **80.7s and 84.1s**. Not a
warm-up — the second run was no faster.

The obvious response was a small thread pool. It was implemented, measured, and
**reverted**: three concurrent resolutions all timed out past 180s, where one
alone took 80s. Torrentio's resolver serialises per account, so overlapping the
requests starves every one of them. `_resolve_worker` is deliberately a plain
loop, and `curl_final_url`'s timeout is 180s for the same reason.

Sequentially, the same three episodes took **318s total** — about 106s each, and
all three succeeded. Consequence worth knowing: a five-episode queue is roughly
nine minutes of resolving before anything plays. If that ever needs fixing, the
route is resolving the first episode, launching, and resolving the rest in the
background before the push — not concurrency.

### 17.3 Setting the shared playlist

Syncplay's command line takes exactly one positional file, so a queue cannot be
handed over at launch. The playlist is room state on the server, so `SyncplayPush`
joins the room as an ordinary client, sets it, and leaves.

Confirmed from `/usr/lib/syncplay/syncplay/`:

- `protocols.py:350-362` — `{"Set": {"playlistChange": {"files": [...]}}}` and
  `{"Set": {"playlistIndex": {"index": n}}}`.
- `server.py:234-241` — accepted if `room.canControl(watcher)` and
  `playlistIsValid(files)`.
- `utils.py:440-445` + `constants.py:84-85` — **250 items, 10000 characters
  total**. At ~141 characters a link that is about 70 episodes.
- `server.py:498-506` — `_deleteRoomIfEmpty` destroys the room *and its
  playlist* when the last watcher leaves.

That last one is the whole reason the push happens after `launch_player`, not
before: a playlist set in an empty room is discarded.

**The trap that cost the most time.** The client must *ask* for the roster.
Dumping raw frames after `Hello` showed the server sends `Set/ready`,
`Set/playlistChange`, `Set/playlistIndex`, `Hello`, then `State` pings — and **no
`List`, and no `Set/user` for anyone already in the room**. `Set/user` only
arrives when somebody joins *after* you. A client that merely listens therefore
concludes it is alone and refuses to push. Syncplay's own client sends
`{"List": null}` (`protocols.py:252`, called from `:234`), and the server replies
`{"List": {"<room>": {"<user>": {...}}}}`. `SyncplayPush` re-asks every 3s while
waiting, with a 2s socket timeout so the loop keeps turning.

It also answers `State` pings, so the server does not treat it as a dead client
during the wait.

### 17.4 One link, one address

Each episode is resolved **once**, through the tunnel, and the resolved link is
what goes on the shared playlist — so both machines fetch the identical URL
through their own tunnels to the same exit. Resolving per-machine would put two
addresses on the debrid account, which is the constraint the whole app exists to
satisfy. Hence `on_add()` refuses to resolve while the tunnel is down and
`require_verified` is on.

The key itself goes to Torrentio, which touches the account from its own address.
That is unchanged from the Stremio addon already in use and was the user's
explicit choice; the alternative (calling the debrid API directly from here) was
offered and declined.

### 17.5 Keeping the key out of the logs

The key sits in the Torrentio URL path, so any message carrying one would leak it
into `session.log`. `redact(text, *secrets)` blanks it, `BrowseWindow._log()`
routes every message through it, and secrets shorter than 8 characters are
ignored so a blank key cannot redact the whole message. The entry field uses
`set_visibility(False)`.

### 17.6 Testing this round

- `test_library.py`, **61 checks**, offline against fixtures captured from the
  live services: redaction, Cinemeta search and episode parsing (specials
  dropped, seasons 1–5 kept), Torrentio parsing (quality, 84 seeders, 5.17 GB,
  ThePirateBay, infoHash kept, cache unknown without a key), the `[RD+]` /
  `[RD download]` markers, source ranking (**exact quality beats a partial match**
  so `1080p` does not land on `1080p 3D SBS`; cached beats uncached; seeders
  break ties), URL building including replacement of a stale `realdebrid=` in the
  options, the playlist message shapes byte-for-byte against Syncplay's own, room
  membership parsing from both frame types, and multi-URL `trustedDomains` with
  the BOM and `%%` escaping intact.
- `test_browse_ui.py`, **38 checks**, the real windows built on the real display
  and never shown: the masked key field, `collect()` round-tripping the new keys,
  bookmarks surviving `collect()`, `adopt_queue` filling the URL field and the
  queue note, all four stack pages, multi-select on episodes, the review list,
  Change… replacing a pick and returning, the busy guards, both refusals firing
  before any network call, and the log redaction.
- **Live against a real Syncplay server**: playlist push arrives at the other
  client with the index; **it survives the pusher disconnecting and is handed to a
  client that joins afterwards** (the girlfriend's-machine case); an empty room is
  refused with the reason; a dead port and a blank server are refused cleanly.
- **Live against the real services**, driving `BrowseWindow._resolve_worker`
  itself with three real episodes: 91/82/83 sources found, all three picks
  `[RD+]`, all three resolved (318s), links distinct, none carrying the key, the
  queue reaching the main window, the URL field filled from it, and the bookmark
  advanced to S01E03. 398 characters for three links, so ~75 fit the cap.

Not covered: the Silverblue render, and a real two-machine watch-through, which
needs her laptop online — it was offline (last seen 3h) throughout this round, so
the protocol work was verified against a local `syncplay-server` instead.

---

## 18. Fourth round — the interface rebuild

Four asks: the scroll bug, caching, a watch history, and the layout itself.

### 18.1 Scrolling over Advanced changed the values

Not a quirk — a real defect that had already corrupted the live config. When the
new settings page first rendered it showed **8081 / 8119 / 23 / 11 / 9** where the
defaults are 8080 / 8118 / 22 / 10 / 3. Every one had been nudged by scrolling
past it, and `host_ssh_port` had reached **23**, which would have broken SSH
outright. Restored by hand.

The cause, from `observe_controllers()`:

| Widget | Scroll controllers |
|---|---|
| `Gtk.ScrolledWindow` | one **capture**, one **bubble** |
| `Gtk.SpinButton` | one **bubble** |
| `Adw.SpinRow` (outer) | none — its spin button is an internal child |

Bubble runs deepest-first, so the spin button's controller fired before the
scroller's and consumed the event.

`block_scroll_steal()` adds a **capture**-phase controller, which runs top-down
and therefore before the spin button. Returning `True` there also stops the
scroller's bubble handler, so the page would have frozen instead — the handler
therefore scrolls the ancestor `Gtk.ScrolledWindow` itself. Attaching it to the
`Adw.SpinRow` covers the nested spin button for the same top-down reason.

`dy` arrives already in scroll units. An early version multiplied it by three,
which made the page jump ~177px per notch over a spin row and ~59px everywhere
else; the multiplier is gone.

Tested by emitting a real scroll at the guard and asserting the value did not
move while the adjustment did — 14 checks, including clamping at both ends and
a spin row with no ancestor scroller.

### 18.2 Cache and history

`JsonStore` holds the shared plumbing (atomic `.tmp` + replace, 0600, a lock, a
corrupt file starting empty rather than raising). `Cache` and `History` extend it.

TTLs: **searches 7 days, episode lists 24 hours, source lists 6 hours** — sources
shortest because cache state moves and new releases appear. Capped at 400
entries, evicted oldest-first. `cinemeta_search`, `cinemeta_episodes` and
`torrentio_sources` each take `cache=` and `refresh=`, and the browser's Refresh
button sets `refresh`.

**The debrid key must not reach the cache.** Torrentio embeds it in the URL path,
so `Source.to_dict(key)` swaps it for `KEY_TOKEN` and `from_dict(key)` puts it
back; the cache *key* for a source list is built from the options string with
`redact()` applied. Asserted by reading the written file's bytes.

Resolved links are not cached at all — they expire, and a stale one fails
mid-episode.

`History` keeps 30 entries, most recent first, de-duplicated by series id so a
re-watch moves to the front and updates the episode. The old single-slot
`library_*` bookmark still exists and is carried into the history once by
`_seed_history()`.

### 18.3 The rebuild

`Adw.ApplicationWindow` → `Adw.ToastOverlay` → `Adw.NavigationSplitView`, with a
five-row sidebar (`VIEWS`) driving an `Adw.ViewStack`: Watch, Route, Where, Setup,
Activity. Start and Stop live in the content `Adw.ToolbarView`'s bottom bar, so
they are reachable from every view.

**What made this tractable**: the exploration produced a cross-reference of every
widget mutated from outside its builder, and those attribute names were kept
verbatim — `pill`, `verdict`, `where_note`, `env_detail`, `btn_install`,
`btn_test`, `results`, `rows`, `btn_stop`, `btn_launch`, `logview`, `logbuf`,
`queue_note`, `e_play_url`, `env_list`, `peer_list`, `host_*_label`,
`host_share`, `btn_restrict`, `runtimes`, `stack`. Every worker and handler was
left alone.

`collect()` needed **no changes at all**: `Adw.EntryRow` implements
`Gtk.Editable`, `Adw.SpinRow` has `value`, `Adw.SwitchRow` has `active`, so the
`e_`/`s_`/`w_` prefix convention kept working through the swap.

Two ordering traps:

- `_build_sidebar()` selects its first row as it builds, which fires
  `on_nav_selected`, which needs the views and `content_page` to exist. Content
  is therefore built first.
- The host status labels stayed `Gtk.Label`s inside `Adw.ActionRow` suffixes
  rather than becoming row subtitles, because the workers call `set_text()` on
  each by name.

`Adw.EntryRow` has no placeholder and no subtitle, so entry hints moved to
tooltips and to `Adw.PreferencesGroup` descriptions. `Adw.SwitchRow` and
`Adw.SpinRow` do have subtitles, which is where the long explanatory paragraphs
went — a readability win over the dim labels they replace.

**Settings now save themselves** ~900ms after the last edit, so the "Save
settings" button is gone. This bit during testing: suites that build the real
`Window` were writing to the real config, and one of them left
`require_verified` **off**. Restored, and every UI suite now points
`st.CONFIG_FILE` at a scratch directory.

### 18.4 Verifying it without a screenshot

The compositor refuses `org.gnome.Shell.Screenshot` (`AccessDenied`), so each
view is rendered offscreen instead: `Gtk.WidgetPaintable` → `Gtk.Snapshot` →
`renderer.render_texture()` → `save_to_png`. `render.py` writes one PNG per view.
That is how the corrupted port values in §18.1 were spotted at all.

### 18.5 Testing this round

- `test_scroll.py` — 14 checks, above.
- `test_store.py` — 43 checks: TTLs against a faked clock, capping and
  oldest-first eviction, reload, 0600, corrupt-file recovery, **the key absent
  from the written bytes**, history ordering/dedupe/cap/forget/clear, and
  `entries()` returning copies.
- `test_shell.py` — 43 checks: five views, sidebar navigation and title
  tracking, **every `DEFAULTS` key still bound except the eight that were never
  widgets**, row types, a scroll guard on all five spins, `collect()` round trip,
  Continue watching including the next-episode subtitle, forget, the stored-data
  counters, and Resume carrying season/episode into the browser.
- `test_library.py` (61) and `test_browse_ui.py` (40) still pass, the latter
  updated for the Adw widgets.

Not covered: the app under a real user's hands on Silverblue, and a two-machine
watch-through — her laptop was offline for this round too.

### 18.6 Follow-ups from first real use

**Torrentio 522s.** A queue died with `Could not resolve S03E02 (HTTP 522)` three
times, then `not JSON (Expecting value: line 1 column 1 (char 0))`. Both are the
same thing: Cloudflare cannot reach the addon, so it answers 522, and for the
JSON endpoint it returns an HTML error page. Confirmed live — `manifest.json`
would not load at all while a stream query took **19.4s**.

Two fixes. `curl_json` now captures `%{http_code}`, so a gateway failure is
reported as `HTTP 522` instead of a JSON parse error, and an unparseable body
quotes what actually arrived. Both it and `curl_final_url` retry on
`TRANSIENT_HTTP` (408/425/429/5xx/52x) with a 2/5/10s backoff; anything else,
`404` included, fails immediately. Proven against a local server: 522 retries
three times, 404 retries none, and a source that fails twice then succeeds now
comes back.

**Ticks on picked episodes.** Multi-select highlighting alone was easy to lose
track of across a season, so `check_row()` puts an `object-select-symbolic` in
each row and `sync_checks()` shows it on exactly the selected ones. The icon
stays in the layout at zero opacity so text does not shift as rows are ticked.
Selection itself is unchanged, so `get_selected_rows()` still drives everything.

**Remembering the environment.** `runtime_kind`/`container` were already saved
and re-selected, but a stopped container came back as "not scanned" and could not
be launched without a manual rescan. `start_container()` runs `<mgr> start
<name>` — the same call that proved reliable in the systemd unit, and a no-op
when already up — and `_scan_worker(autostart=True)` calls it for the remembered
container before scanning. Only on the launch scan, never on a manual rescan.
`autostart_container` (default on) controls it.

Tests: `test_extras.py`, 20 checks — tick state across pick, unpick, select-all
and unselect-all; selection still feeding `find_sources`; and autostart firing
only for a remembered distrobox, only with the switch on, and never on a rescan.

### 18.7 Host could not queue anything

`BrowseWindow.on_add` refused unless `session.tunnel_alive()`. In host mode no
tunnel is ever opened — that machine is the exit point — so the host was locked
out of the playlist permanently while the client worked fine.

The gate now skips host mode entirely. `_socks()` already returns `None` there,
so links resolve straight from the host, which is the correct address by
definition. Tested with no tunnel running in either role: the client is still
blocked, the host queues.

### 18.8 Falling back to what the account already holds

When Torrentio returns nothing — outage or otherwise — the episode may already be
on the debrid account, and then no torrent index is needed at all.

`rd_fallback_sources()` tries two things:

1. `GET /downloads?limit=200` — entries carry a `download` field that is already
   a final CDN link. A match is returned as a `Source` with **`direct=True`**.
2. failing that, `GET /torrents?limit=200` for a `downloaded` torrent whose name
   matches the series, then `/torrents/info/{id}` to find the episode among its
   **selected** files. `links` corresponds to selected files **in order**, so the
   index of the matching file indexes `links`; that link goes through
   `/unrestrict/link` for a fresh URL.

`Source.direct` means the URL needs no resolving, and `_resolve_worker` passes it
through untouched — saving the ~80s round trip these would otherwise cost.

`file_is_episode()` is loose on the name and strict on the numbering: `SxxEyy`
and `4x01` are both understood, `S04E010` does not match E01, a year in the
series title is ignored, and every remaining title word must appear. Getting this
wrong plays the wrong episode, which is worse than finding nothing.

**The token stays out of `ps`.** `rd_auth_file()` writes `header = "Authorization:
Bearer …"` to a 0600 temp file passed as `curl -K`, rather than a `-H` argument,
and removes it afterwards.

Verified against the live account: the fallback found `The.Flash.2014.S04E01`
already in `/downloads`, returned it as a direct 1080p/678 MB source, and the key
appeared nowhere in the resulting URL.

### 18.9 The role stopped being remembered

`cfg["role"]` has no widget, so `collect()` never touches it — it is set by
`on_role_changed`. That was fine while a Save button existed; once settings
started saving themselves (§18.3), switching role and closing the app lost the
choice. `on_role_changed` now schedules the same debounced save. Asserted by
writing, reloading `Config()`, and reading the role back.

---

## 19. Split into a package

One 4,700-line file became `syncplay_tunnel/`, so a change can be made without
reading the whole thing. `INDEX.md` lists every module with its purpose and its
symbols, and is the thing to read first.

| Module | Holds |
|---|---|
| `constants.py` | every fixed value; imports nothing from the package |
| `util.py` | `run`, the curl fetchers, ports, `redact` |
| `store.py` | `Config`, `JsonStore`, `Cache`, `History` |
| `tailscale.py`, `sshkeys.py` | peers; key enrolment and restriction |
| `runtimes.py` | environments and the per-environment installer |
| `syncplay_ini.py` | Syncplay's own config file |
| `library.py` | Cinemeta, Torrentio, the debrid account |
| `playlist.py` | `SyncplayPush` |
| `proxy.py`, `session.py` | the CONNECT bridge; the tunnel and watchdog |
| `ui/widgets.py`, `ui/browse.py`, `ui/window.py`, `ui/app.py` | the interface |

`__init__.py` re-exports everything, so `import syncplay_tunnel as st` still
gives one flat namespace.

**Three traps, all hit and all fixed:**

- `gi.require_version` has to run before the first `from gi.repository import`.
  The old header did that; the slices did not, and the app exited immediately
  with a `PyGIWarning`. `gtk_setup.py` now owns it and is imported first by
  everything that touches GTK.
- A lazy `__getattr__` on the package to defer the GTK imports recursed to death.
  Replaced with direct imports — importing GTK needs no display, only using it
  does.
- **Modules import names directly, so patching the package no longer reaches
  them.** `st.run = fake` used to redirect every caller; now `runtimes.py` holds
  its own reference. Tests patch at the binding site instead. For the same
  reason `Config` gained a `path` argument rather than relying on a patchable
  `CONFIG_FILE` global.

A static pass for names used but never defined or imported caught seven modules
with missing imports before anything was run — worth repeating after any further
moves.

`install.sh` now copies the package to `~/.local/share/syncplay-tunnel-app` and
writes a launcher onto `PATH` that points at it.

### 19.1 Episodes are a checklist

The episode list was `SelectionMode.MULTIPLE`, which needed ctrl-click to add a
second episode. It is now `SelectionMode.NONE` with a real `Gtk.CheckButton` per
row: click anywhere on a row to tick it, click again to untick. `checked_rows()`
replaces `get_selected_rows()` as what the queue is built from.

### 19.2 The setup dialog with no URL

Asked for: skip Syncplay's setup dialog even when no URL is set.

`ConfigurationGetter.py:565` is `(forceGuiPrompt == "True" or not file) and not
noGui`. The flag half is already handled; the **no file** half cannot be turned
off except with `--no-gui`, and `clientManager.py:12` shows that drops Syncplay
to a console interface with no playlist at all — useless here.

So the nearest thing that keeps the GUI: `last_play_url` remembers whatever was
launched, and a blank URL falls back to it. With nothing remembered the dialog is
unavoidable, and the log says so plainly rather than pretending.
