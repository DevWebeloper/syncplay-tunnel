#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
syncplay-tunnel — route Syncplay/mpv through a remote machine over SSH, and
prove the traffic actually leaves from there before anything starts playing.

Runs on:
  * CachyOS / Arch            (host side, or client side)
  * Fedora Silverblue host    (drives distrobox)
  * Inside a distrobox/toolbx container (runs syncplay directly)

Zero pip dependencies. Needs: python3-gobject + gtk4, ssh, curl.
"""

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

import ast
import configparser
import ipaddress
import json
import os
import re
import select
import shlex
import getpass
import shutil
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

APP_ID = "io.github.DevWebeloper.SyncplayTunnel"
APP_NAME = "Syncplay Tunnel"

# Gtk.DropDown reports "nothing selected" with this sentinel.
INVALID_SELECTION = getattr(Gtk, "INVALID_LIST_POSITION", 0xFFFFFFFF)

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "syncplay-tunnel"
CONFIG_FILE = CONFIG_DIR / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "syncplay-tunnel"
LOG_FILE = DATA_DIR / "session.log"

# Public-IP echo services, tried in order. Plain HTTP variants are kept as a
# fallback because some SOCKS paths choke on TLS through odd MTUs.
IP_ECHOS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

DEFAULTS = {
    "host_ip": "",
    # Most setups use the same account name on both ends, so this is the least
    # wrong guess available before anything is configured.
    "host_user": getpass.getuser(),
    "host_ssh_port": 22,
    "client_ip": "",
    "socks_port": 8080,
    "http_port": 8118,
    "role": "client",
    "runtime_kind": "auto",
    "container": "",
    "scan_stopped": False,
    "check_interval": 10,
    "max_fails": 3,
    "syncplay_server": "",
    "syncplay_room": "",
    "syncplay_user": "",
    "play_url": "",
    "skip_syncplay_dialog": True,
    "trust_play_domain": True,
    "mpv_extra": "",
    "require_verified": True,
    "stop_container_on_drop": True,
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def which(cmd):
    return shutil.which(cmd) is not None


def in_container():
    """True when we're running inside distrobox / toolbx / docker."""
    return (
        Path("/run/.containerenv").exists()
        or Path("/.dockerenv").exists()
        or bool(os.environ.get("CONTAINER_ID"))
    )


def stamp():
    return datetime.now().strftime("%H:%M:%S")


def run(cmd, timeout=15, env=None):
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "timed out after %ss" % timeout
    except FileNotFoundError:
        return 127, "", "%s not found" % cmd[0]
    except Exception as exc:  # pragma: no cover
        return 1, "", str(exc)


def notify(title, body, urgent=False):
    args = ["notify-send"]
    if urgent:
        args += ["-u", "critical"]
    args += [title, body]
    if in_container() and which("distrobox-host-exec"):
        args = ["distrobox-host-exec"] + args
    if which(args[0]):
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout):
            return True
    except OSError:
        return False


TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def ssh_clients(port=22):
    """Peer addresses with an established connection to our sshd.

    Host mode uses this to answer "how many devices are on me right now".
    One machine can hold several SSH channels open, so addresses are deduped.
    """
    rc, out, _ = run(
        ["ss", "-tnH", "state", "established", "( sport = :%d )" % int(port)],
        timeout=8,
    )
    if rc != 0:
        return []
    found = []
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        peer = cols[3]
        # ss prints host:port, and IPv6 as [addr]:port
        addr = peer.rsplit(":", 1)[0].strip("[]")
        if addr and addr not in found:
            found.append(addr)
    return found


def is_tailscale_addr(addr):
    try:
        return ipaddress.ip_address(addr) in TAILSCALE_NET
    except ValueError:
        return False


def free_port(port):
    """Best-effort clear of a stuck listener from a previous crash."""
    if which("fuser"):
        run(["fuser", "-k", "%d/tcp" % port], timeout=5)
    elif which("lsof"):
        rc, out, _ = run(["lsof", "-ti", "tcp:%d" % port], timeout=5)
        for pid in out.split():
            try:
                os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

class Config(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self.load()

    def load(self):
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text())
                for k, v in data.items():
                    if k in DEFAULTS:
                        self[k] = v
        except Exception:
            pass

    def save(self):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(self), indent=2) + "\n")
        tmp.replace(CONFIG_FILE)
        CONFIG_FILE.chmod(0o600)


# --------------------------------------------------------------------------- #
# tailscale
# --------------------------------------------------------------------------- #

class Peer:
    def __init__(self, name, ip, online, os_name="", is_self=False):
        self.name = name
        self.ip = ip
        self.online = online
        self.os_name = os_name
        self.is_self = is_self

    def label(self):
        state = "online" if self.online else "offline"
        bits = [self.name, self.ip, state]
        if self.os_name:
            bits.insert(2, self.os_name)
        return "  ·  ".join(bits)


def tailscale_status():
    """Return (self_peer, [peers]). Either may be None/empty."""
    if not which("tailscale"):
        return None, []
    rc, out, _ = run(["tailscale", "status", "--json"], timeout=20)
    if rc != 0:
        return None, []
    try:
        data = json.loads(out)
    except Exception:
        return None, []

    def build(entry, is_self=False):
        ips = entry.get("TailscaleIPs") or []
        v4 = next((i for i in ips if "." in i), None)
        if not v4:
            return None
        return Peer(
            entry.get("HostName") or entry.get("DNSName", "").split(".")[0] or "?",
            v4,
            bool(entry.get("Online")) or is_self,
            entry.get("OS", ""),
            is_self,
        )

    me = build(data.get("Self") or {}, is_self=True)
    peers = [p for p in (build(e) for e in (data.get("Peer") or {}).values()) if p]
    peers.sort(key=lambda p: (not p.online, p.name.lower()))
    return me, peers


# --------------------------------------------------------------------------- #
# SSH key enrolment
#
# ssh-copy-id has no way to take a password on stdin, and `sshpass -p` would put
# it in the process list where anyone on the box can read it. So we run the real
# command on a pseudo-terminal and answer its prompt directly — stdlib only, and
# the password never touches argv, the environment, or disk.
# --------------------------------------------------------------------------- #

KEY_CANDIDATES = ["id_ed25519", "id_ecdsa", "id_rsa"]


def find_ssh_key():
    ssh_dir = Path.home() / ".ssh"
    for name in KEY_CANDIDATES:
        pub = ssh_dir / (name + ".pub")
        if pub.exists():
            return pub
    return None


def ensure_ssh_key(log=None):
    """Return the public key path, generating an ed25519 key if there is none."""
    existing = find_ssh_key()
    if existing:
        return existing, None
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    priv = ssh_dir / "id_ed25519"
    if log:
        log("No SSH key found — generating one.")
    rc, _, err = run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(priv),
         "-C", "syncplay-tunnel@%s" % socket.gethostname()],
        timeout=60,
    )
    if rc != 0:
        return None, err or "ssh-keygen failed"
    return priv.with_suffix(".pub"), None


# What our key is allowed to do on the host once it is installed.
#
# `restrict` turns everything off — pty, agent and X11 forwarding, user rc —
# and `port-forwarding` puts back the one thing the tunnel actually needs. A
# forced command= is deliberately NOT used: route check 2 runs `ssh host true`
# and check 7 runs `ssh host curl …` for ground truth, and a forced command
# would break the strongest half of the verification.
KEY_OPTIONS = "restrict,port-forwarding"

# Lines whose first field is a key type carry no options yet. Anything else
# already has an options field and is left exactly as the owner wrote it.
_RESTRICT_AWK = r'''
BEGIN { changed = 0 }
{
    if (index($0, blob) > 0 && $1 ~ /^(ssh-|ecdsa-|sk-)/) {
        print opts " " $0
        changed = 1
    } else {
        print $0
    }
}
END { exit changed ? 0 : 1 }
'''


def restrict_authorized_key(ssh_cmd, pub_path, log=None):
    """Prefix our key line on the host with KEY_OPTIONS.

    Runs after enrolment, over the key auth that was just proved to work, so no
    password is involved. Only the line holding our own key blob is touched, and
    only when it has no options field yet — so re-running is a no-op and nobody
    else's entry is rewritten.
    """
    def say(msg):
        if log:
            log(msg)

    try:
        parts = Path(pub_path).read_text().split()
    except OSError as exc:
        say("Could not read %s: %s" % (pub_path, exc))
        return False
    if len(parts) < 2:
        say("%s does not look like a public key." % pub_path)
        return False
    blob = parts[1]

    remote = (
        'f=~/.ssh/authorized_keys; '
        '[ -f "$f" ] || exit 3; '
        't=$(mktemp "$f.XXXXXX") || exit 4; '
        'awk -v blob=%s -v opts=%s %s "$f" > "$t"; rc=$?; '
        'if [ $rc -ne 0 ]; then rm -f "$t"; exit $rc; fi; '
        'chmod 600 "$t" && mv "$t" "$f"'
        % (shlex.quote(blob), shlex.quote(KEY_OPTIONS), shlex.quote(_RESTRICT_AWK))
    )

    # Wrapped in sh -c because the remote login shell is whatever the host user
    # picked — fish, for one, does not read this syntax.
    rc, _, err = run(list(ssh_cmd) + ["sh -c " + shlex.quote(remote)], timeout=30)
    if rc == 0:
        say("Key restricted on the host: %s. It can forward ports and run the "
            "route check, nothing else." % KEY_OPTIONS)
        return True
    if rc == 1:
        say("No key line needed changing on the host — it already carries its own "
            "options.")
        return True
    say("Could not restrict the key on the host (exit %s). It works, but it is a "
        "full shell login: %s" % (rc, err or "no error output"))
    return False


def key_line_is_open(line):
    """True when an authorized_keys line carries no options — a full shell."""
    line = line.strip()
    if not line or line.startswith("#"):
        return False
    return line.split()[0].startswith(("ssh-", "ecdsa-", "sk-"))


def restrict_local_keys(path=None, log=None):
    """Add KEY_OPTIONS to every unrestricted line in our own authorized_keys.

    For keys enrolled before this existed. Options are only ever added, never
    removed, no line is dropped, and the previous file is kept as .bak — a
    mistake here locks clients out, so it stays reversible.
    Returns (restricted, total).
    """
    def say(msg):
        if log:
            log(msg)

    keys = Path(path) if path else (Path.home() / ".ssh/authorized_keys")
    try:
        original = keys.read_text()
    except OSError as exc:
        say("No authorized_keys to change: %s" % exc)
        return 0, 0

    out, changed, total = [], 0, 0
    for line in original.splitlines():
        if line.strip() and not line.strip().startswith("#"):
            total += 1
        if key_line_is_open(line):
            out.append("%s %s" % (KEY_OPTIONS, line.strip()))
            changed += 1
        else:
            out.append(line)

    if not changed:
        say("Every authorised key already has options set — nothing to change.")
        return 0, total

    try:
        keys.with_suffix(keys.suffix + ".bak").write_text(original)
        tmp = keys.with_suffix(keys.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.chmod(0o600)
        tmp.replace(keys)
    except OSError as exc:
        say("Could not rewrite %s: %s" % (keys, exc))
        return 0, total

    say("Restricted %d of %d authorised key%s to %s. Previous file kept as %s.bak."
        % (changed, total, "" if total == 1 else "s", KEY_OPTIONS, keys.name))
    return changed, total


PROMPT_PW = re.compile(r"(password|passcode|passphrase)\s*:", re.I)
PROMPT_YN = re.compile(r"\(yes/no", re.I)
DENIED = re.compile(r"permission denied|too many authentication failures", re.I)


def ssh_copy_id(user, host, port, password, log=None, timeout=90):
    """Install our public key on the host. Returns (ok, message).

    `password` is a bytearray so the caller can zero it afterwards.
    """
    import pty

    pub, err = ensure_ssh_key(log=log)
    if pub is None:
        return False, "Could not create an SSH key: %s" % err

    argv = [
        "ssh-copy-id",
        "-i", str(pub),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "PubkeyAuthentication=no",          # force the password path
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "ConnectTimeout=10",
        "-o", "NumberOfPasswordPrompts=1",
        "-p", str(port),
        "%s@%s" % (user, host),
    ]
    if not which("ssh-copy-id"):
        return False, "ssh-copy-id is not installed on this machine."

    try:
        pid, fd = pty.fork()
    except OSError as exc:
        return False, "Could not start a terminal for ssh-copy-id: %s" % exc

    if pid == 0:                                   # child
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)

    transcript = ""
    sent = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            transcript += text

            if not sent and PROMPT_PW.search(text):
                os.write(fd, bytes(password) + b"\n")
                sent = True
            elif PROMPT_YN.search(text):
                os.write(fd, b"yes\n")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    try:
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except Exception:
        code = 1

    tail = transcript.strip().splitlines()
    tail = tail[-1] if tail else ""

    if code == 0:
        return True, "Key installed on %s@%s." % (user, host)
    # Order matters: a refusal with no prompt is not a wrong password.
    if not sent:
        return False, ("The host never asked for a password. It may accept keys only, "
                       "or the account name is wrong. Last line: %s" % tail)
    if DENIED.search(transcript):
        return False, "The host rejected that password."
    return False, "ssh-copy-id exited with %s. %s" % (code, tail)


# --------------------------------------------------------------------------- #
# playback environments
#
# Syncplay can live in three places: straight on this system, in a Flatpak, or
# inside any distrobox container. We find all of them, check which ones actually
# have both syncplay and mpv, and put the complete ones at the top of the list.
# --------------------------------------------------------------------------- #

ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
PROBE = "command -v syncplay >/dev/null 2>&1 && echo HAVE_SP; " \
        "command -v mpv >/dev/null 2>&1 && echo HAVE_MPV"


class Runtime:
    """One place Syncplay could be launched from."""

    def __init__(self, kind, name="", image="", running=True):
        self.kind = kind            # "native" | "distrobox"
        self.name = name            # container name, "" for native
        self.image = image
        self.running = running
        self.has_syncplay = None    # None = not probed yet
        self.has_mpv = None
        self.syncplay_flatpak = None
        self.mpv_flatpak = None

    @property
    def key(self):
        return "native" if self.kind == "native" else "distrobox:" + self.name

    @property
    def complete(self):
        return bool(self.has_syncplay) and bool(self.has_mpv)

    @property
    def rank(self):
        """Lower sorts first: complete environments win."""
        if self.has_syncplay is None:
            return 3
        if self.complete:
            return 0
        if self.has_syncplay or self.has_mpv:
            return 1
        return 2

    def status_text(self):
        if self.has_syncplay is None:
            return "not scanned"
        bits = []
        if self.has_syncplay:
            bits.append("Syncplay" + (" (Flatpak)" if self.syncplay_flatpak else ""))
        if self.has_mpv:
            bits.append("mpv" + (" (Flatpak)" if self.mpv_flatpak else ""))
        if not bits:
            return "neither installed"
        if self.complete:
            return " + ".join(bits)
        missing = "mpv" if self.has_syncplay else "Syncplay"
        return "%s found, %s missing" % (bits[0], missing)

    def label(self):
        if self.kind == "native":
            head = "This system"
        else:
            head = "distrobox: %s" % self.name
            if not self.running:
                head += " (stopped)"
        return "%s — %s" % (head, self.status_text())


def flatpak_apps():
    if not which("flatpak"):
        return []
    rc, out, _ = run(["flatpak", "list", "--app", "--columns=application"], timeout=15)
    if rc != 0:
        return []
    return [l.strip() for l in out.splitlines() if l.strip()]


def probe_native(rt):
    rt.has_syncplay = which("syncplay")
    rt.has_mpv = which("mpv")
    if rt.has_syncplay and rt.has_mpv:
        return rt
    for app in flatpak_apps():
        tail = app.rsplit(".", 1)[-1].lower()
        if not rt.has_syncplay and "syncplay" in app.lower():
            rt.has_syncplay = True
            rt.syncplay_flatpak = app
        if not rt.has_mpv and tail == "mpv":
            rt.has_mpv = True
            rt.mpv_flatpak = app
    return rt


def host_prefix():
    """Prefix needed to run something on the host from inside a container."""
    if in_container() and which("distrobox-host-exec"):
        return ["distrobox-host-exec"]
    return []


def host_run(cmd, timeout=25):
    return run(host_prefix() + cmd, timeout=timeout)


def host_has(cmd):
    """Is a command available where it actually needs to run?"""
    if not in_container():
        return which(cmd)
    if not which("distrobox-host-exec"):
        return False
    rc, _, _ = host_run(["sh", "-c", "command -v " + shlex.quote(cmd)], timeout=15)
    return rc == 0


def current_container():
    """Name of the container we're inside, or None."""
    name = os.environ.get("CONTAINER_ID")
    if name:
        return name
    try:
        for line in Path("/run/.containerenv").read_text().splitlines():
            if line.startswith("name="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def container_manager():
    for mgr in (os.environ.get("DBX_CONTAINER_MANAGER"), "podman", "docker"):
        if mgr and host_has(mgr):
            return mgr
    return None


def list_distroboxes(log=None):
    """Parse `distrobox list` into Runtime objects.

    Inside a container there is no `distrobox` binary, so the listing has to go
    out through distrobox-host-exec. If even that is unavailable we can still
    offer the container we are sitting in, read from /run/.containerenv.
    """
    if not host_has("distrobox"):
        here = current_container()
        if here:
            if log:
                log("distrobox isn't reachable from in here — offering '%s' only. "
                    "Install distrobox-host-exec, or run the app on the host, to see them all."
                    % here)
            return [Runtime("distrobox", here, running=True)]
        if log:
            log("distrobox not found, so no containers are listed.")
        return []

    rc, out, err = host_run(["distrobox", "list"], timeout=40)
    if rc != 0:
        if log:
            log("distrobox list failed: %s" % (err or "exit %s" % rc))
        here = current_container()
        return [Runtime("distrobox", here, running=True)] if here else []
    found = []
    for line in ANSI.sub("", out).splitlines():
        if "|" not in line:
            continue
        cols = [c.strip() for c in line.split("|")]
        if len(cols) < 3 or cols[1].upper() == "NAME" or not cols[1]:
            continue
        name, status = cols[1], cols[2]
        image = cols[3] if len(cols) > 3 else ""
        found.append(Runtime("distrobox", name, image, running=status.lower().startswith("up")))
    return found


def probe_container(rt, mgr, allow_start=False, log=None):
    """Check for syncplay/mpv inside a container.

    A running container is checked through the container manager, which costs
    nothing. A stopped one has to be started to be looked inside, so that only
    happens when explicitly asked for.
    """
    out = ""
    if rt.name and rt.name == current_container():
        # we're inside it already — no container manager needed
        rc, out, _ = run(["bash", "-lc", PROBE], timeout=20)
        rt.running = True
        if rc != 0:
            out = ""
    elif rt.running and mgr:
        rc, out, _ = host_run([mgr, "exec", rt.name, "bash", "-lc", PROBE], timeout=30)
        if rc != 0:
            out = ""
    elif not rt.running and allow_start:
        if log:
            log("Starting %s to look inside it…" % rt.name)
        rc, out, _ = host_run(["distrobox", "enter", rt.name, "--", "bash", "-lc", PROBE],
                              timeout=180)
        if rc == 0:
            rt.running = True
        else:
            out = ""
    else:
        return rt

    rt.has_syncplay = "HAVE_SP" in out
    rt.has_mpv = "HAVE_MPV" in out
    return rt


def scan_runtimes(allow_start=False, log=None):
    """Return every launch target, best first."""
    runtimes = [probe_native(Runtime("native"))]
    mgr = container_manager()
    if log and mgr is None:
        log("No podman or docker reachable — running containers can't be inspected.")
    for rt in list_distroboxes(log=log):
        runtimes.append(probe_container(rt, mgr, allow_start=allow_start, log=log))
    # complete environments first; native breaks ties
    runtimes.sort(key=lambda r: (r.rank, r.kind != "native", r.name))
    return runtimes


# --------------------------------------------------------------------------- #
# installing Syncplay/mpv into a chosen environment
#
# install.sh only handles what the app itself needs (GTK, ssh, curl). Syncplay
# and mpv belong to whichever environment you picked to watch in, which the app
# knows and a shell script run once does not.
# --------------------------------------------------------------------------- #

FLATPAK_IDS = {"syncplay": "pl.syncplay.Syncplay", "mpv": "io.mpv.Mpv"}


def _pm_install_shell(pm, packages):
    """Shell snippet that installs `packages`, minus any privilege prefix."""
    names = " ".join(shlex.quote(p) for p in packages)
    if pm == "pacman":
        return "pacman -S --needed --noconfirm " + names
    if pm == "apt-get":
        return "apt-get update && apt-get install -y " + names
    if pm in ("dnf", "zypper"):
        return "%s install -y %s" % (pm, names)
    return None


def _detect_pm(probe):
    """First package manager `probe` finds. probe(cmd) -> bool."""
    for pm in ("pacman", "apt-get", "dnf", "zypper"):
        if probe(pm):
            return pm
    return None


def install_plan(rt, missing):
    """Work out how to install `missing` into runtime `rt`.

    Returns (argv, description, needs_terminal_note) or (None, reason, False).
    Nothing is executed here so the caller can show the command first.
    """
    if not missing:
        return None, "Nothing is missing there.", False

    if rt.kind == "distrobox":
        # distrobox gives the user passwordless sudo inside the container, so
        # this needs no prompt of any kind.
        def probe(cmd):
            rc, _, _ = host_run(
                ["distrobox", "enter", rt.name, "--",
                 "sh", "-c", "command -v " + shlex.quote(cmd)], timeout=25)
            return rc == 0

        pm = _detect_pm(probe)
        if pm is None:
            return None, ("No supported package manager inside '%s'." % rt.name), False
        cmd = "sudo -n sh -c %s" % shlex.quote(_pm_install_shell(pm, missing))
        argv = host_prefix() + ["distrobox", "enter", rt.name, "--", "bash", "-lc", cmd]
        return argv, "%s in distrobox '%s'" % (pm, rt.name), False

    # native
    if which("rpm-ostree"):
        # Silverblue and friends: layering needs a reboot, a user Flatpak does
        # not — and probe_native() already recognises Flatpak installs.
        ids = [FLATPAK_IDS[m] for m in missing if m in FLATPAK_IDS]
        if not which("flatpak"):
            return None, ("This is an rpm-ostree system. Install with Flatpak "
                          "(flatpak is missing) or layer with rpm-ostree and reboot."), False
        argv = ["flatpak", "install", "-y", "--user", "flathub"] + ids
        return argv, "Flatpak (user install, no root, no reboot)", False

    pm = _detect_pm(which)
    if pm is None:
        return None, "No supported package manager on this system.", False
    inner = _pm_install_shell(pm, missing)
    if not which("pkexec"):
        return None, ("pkexec is missing, so this one needs a terminal: sudo sh -c %s"
                      % shlex.quote(inner)), True
    return ["pkexec", "sh", "-c", inner], "%s via pkexec" % pm, False


def stream_command(argv, log, timeout=900):
    """Run a command, feeding its output into the Activity log line by line."""
    log("Running: %s" % " ".join(shlex.quote(a) for a in argv))
    try:
        p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError:
        log("%s is not available here." % argv[0])
        return False
    except Exception as exc:
        log("Could not start it: %s" % exc)
        return False

    deadline = time.time() + timeout
    for line in p.stdout:
        line = line.rstrip()
        if line:
            log("  " + line[:200])
        if time.time() > deadline:
            p.kill()
            log("Gave up after %ds." % timeout)
            return False
    return p.wait() == 0


# --------------------------------------------------------------------------- #
# Syncplay's own configuration file
#
# Syncplay shows its setup dialog when forceGuiPrompt is True *or* when no file
# was given on the command line (ConfigurationGetter.py, "if
# (self._config['forceGuiPrompt'] == "True" or not self._config['file'])").
# Both have to be handled for playback to start on its own, and the trusted
# domain list decides whether a URL switches without a confirmation.
# --------------------------------------------------------------------------- #

SYNCPLAY_SECTION = "client_settings"


def syncplay_ini_path():
    """Same search order Syncplay uses: ~/.syncplay first, then XDG."""
    legacy = Path.home() / ".syncplay"
    if legacy.is_file():
        return legacy
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg / "syncplay.ini"


def prepare_syncplay_ini(url, trust_domain, log=None, path=None):
    """Turn off Syncplay's setup dialog, and optionally trust the URL's domain.

    Values are read and written raw: Syncplay escapes % as %% and configparser's
    default interpolation would blow up on it. The file is utf-8 with a BOM,
    which is what Syncplay itself writes.
    """
    def say(msg):
        if log:
            log(msg)

    ini = Path(path) if path else syncplay_ini_path()
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    if ini.exists():
        try:
            with ini.open("r", encoding="utf-8-sig") as fh:
                parser.read_file(fh)
        except Exception as exc:
            say("Could not read %s: %s" % (ini, exc))
            return False
    if not parser.has_section(SYNCPLAY_SECTION):
        parser.add_section(SYNCPLAY_SECTION)

    changed = []
    if parser.get(SYNCPLAY_SECTION, "forceguiprompt", fallback="True") != "False":
        parser.set(SYNCPLAY_SECTION, "forceguiprompt", "False")
        changed.append("forceguiprompt = False")

    host = urlsplit(url).hostname if url else None
    if trust_domain and host:
        raw = parser.get(SYNCPLAY_SECTION, "trusteddomains", fallback="[]")
        try:
            domains = ast.literal_eval(raw)
            if not isinstance(domains, list):
                domains = []
        except Exception:
            domains = []
        if host not in domains:
            domains.append(host)
            parser.set(SYNCPLAY_SECTION, "trusteddomains", repr(domains))
            changed.append("trusteddomains += %s" % host)

    if not changed:
        say("Syncplay's config already lets playback start on its own.")
        return True

    try:
        ini.parent.mkdir(parents=True, exist_ok=True)
        tmp = ini.with_suffix(ini.suffix + ".syncplay-tunnel.tmp")
        with tmp.open("w", encoding="utf-8-sig") as fh:
            parser.write(fh)
        tmp.replace(ini)
    except OSError as exc:
        say("Could not write %s: %s" % (ini, exc))
        return False
    say("Syncplay config (%s): %s" % (ini, "; ".join(changed)))
    return True


# --------------------------------------------------------------------------- #
# SOCKS5 client + HTTP proxy bridge
#
# Why this exists: mpv hands --http-proxy straight to FFmpeg, and FFmpeg's HTTP
# protocol only speaks HTTP CONNECT — it silently ignores a socks5:// value and
# streams direct. That is a real IP leak on the exact traffic we care about.
# So we expose a local HTTP CONNECT proxy that forwards over the SSH SOCKS5
# tunnel, and point mpv at that instead.
# --------------------------------------------------------------------------- #

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("socks: connection closed early")
        buf += chunk
    return buf


def socks5_connect(dest_host, dest_port, proxy_port, timeout=15):
    """Open a TCP stream to dest via a no-auth SOCKS5 proxy on localhost.

    Hostnames are sent to the proxy verbatim (socks5h semantics), so DNS is
    resolved on the far side and never leaks locally.
    """
    s = socket.create_connection(("127.0.0.1", proxy_port), timeout)
    s.settimeout(timeout)
    s.sendall(b"\x05\x01\x00")
    ver, method = _recv_exact(s, 2)
    if ver != 5 or method != 0:
        s.close()
        raise OSError("socks: handshake refused")

    host_b = dest_host.encode("idna") if not _is_ip(dest_host) else dest_host.encode()
    if len(host_b) > 255:
        s.close()
        raise OSError("socks: hostname too long")
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(host_b)]) + host_b + struct.pack(">H", dest_port))

    head = _recv_exact(s, 4)
    if head[1] != 0:
        s.close()
        raise OSError("socks: server replied error %d" % head[1])
    atyp = head[3]
    if atyp == 1:
        _recv_exact(s, 4)
    elif atyp == 3:
        _recv_exact(s, _recv_exact(s, 1)[0])
    elif atyp == 4:
        _recv_exact(s, 16)
    _recv_exact(s, 2)
    s.settimeout(None)
    return s


def _is_ip(value):
    try:
        socket.inet_aton(value)
        return True
    except OSError:
        return ":" in value


def _relay(a, b):
    """Pump bytes both ways until either side hangs up."""
    socks = [a, b]
    try:
        while True:
            r, _, x = select.select(socks, [], socks, 300)
            if x or not r:
                break
            for src in r:
                dst = b if src is a else a
                data = src.recv(65536)
                if not data:
                    return
                dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


class _BridgeHandler(socketserver.StreamRequestHandler):
    timeout = 60

    def handle(self):
        try:
            line = self.rfile.readline(65536).decode("latin-1").strip()
        except Exception:
            return
        if not line:
            return

        parts = line.split()
        if len(parts) < 3:
            self._fail(400, "Malformed request line")
            return
        method, target, version = parts[0], parts[1], parts[2]

        headers = []
        while True:
            raw = self.rfile.readline(65536)
            if not raw or raw in (b"\r\n", b"\n"):
                break
            headers.append(raw.decode("latin-1").rstrip("\r\n"))

        port = self.server.socks_port
        if method.upper() == "CONNECT":
            host, _, p = target.rpartition(":")
            try:
                upstream = socks5_connect(host, int(p or 443), port)
            except Exception as exc:
                self._fail(502, "Upstream tunnel refused: %s" % exc)
                return
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            _relay(self.connection, upstream)
            return

        # Absolute-form request (plain HTTP through a proxy)
        u = urlsplit(target)
        if not u.hostname:
            self._fail(400, "Proxy needs an absolute URL")
            return
        try:
            upstream = socks5_connect(u.hostname, u.port or 80, port)
        except Exception as exc:
            self._fail(502, "Upstream tunnel refused: %s" % exc)
            return

        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        kept = [h for h in headers if not h.lower().startswith("proxy-")]
        req = "%s %s %s\r\n%s\r\n\r\n" % (method, path, version, "\r\n".join(kept))
        try:
            upstream.sendall(req.encode("latin-1"))
        except OSError:
            upstream.close()
            return
        _relay(self.connection, upstream)

    def _fail(self, code, msg):
        try:
            body = msg.encode()
            self.wfile.write(
                ("HTTP/1.1 %d Proxy Error\r\nContent-Length: %d\r\n"
                 "Connection: close\r\n\r\n" % (code, len(body))).encode()
            )
            self.wfile.write(body)
            self.wfile.flush()
        except OSError:
            pass

    def log_message(self, *_a, **_k):
        pass


class _BridgeServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class HttpBridge:
    """Local HTTP CONNECT proxy -> SSH SOCKS5 tunnel."""

    def __init__(self, listen_port, socks_port):
        self.listen_port = listen_port
        self.socks_port = socks_port
        self.server = None
        self.thread = None

    def start(self):
        self.server = _BridgeServer(("127.0.0.1", self.listen_port), _BridgeHandler)
        self.server.socks_port = self.socks_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None


# --------------------------------------------------------------------------- #
# tunnel + session manager
# --------------------------------------------------------------------------- #

class Session:
    """Owns the SSH tunnel, the HTTP bridge, the watchdog and syncplay."""

    def __init__(self, cfg, log, on_state):
        self.cfg = cfg
        self.log = log
        self.on_state = on_state
        self.ssh = None
        self.bridge = None
        self.player = None
        self.watchdog = None
        self.active_runtime = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()

    # -- ssh ------------------------------------------------------------- #

    def ssh_base(self, batch=True):
        cfg = self.cfg
        cmd = [
            "ssh",
            "-p", str(cfg["host_ssh_port"]),
            "-o", "ServerAliveInterval=5",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=8",
        ]
        if batch:
            cmd += ["-o", "BatchMode=yes"]
        return cmd + ["%s@%s" % (cfg["host_user"], cfg["host_ip"])]

    def begin(self):
        """Reset the stop latch so a session can be started again."""
        self._stopping.clear()
        self.player = None
        self.active_runtime = None

    def open_tunnel(self):
        """Bring up SSH SOCKS5 + the HTTP bridge. Returns (ok, message)."""
        with self._lock:
            if self.tunnel_alive():
                return True, "Tunnel already up"

            cfg = self.cfg
            self._stopping.clear()
            free_port(cfg["socks_port"])
            free_port(cfg["http_port"])
            time.sleep(0.4)

            cmd = self.ssh_base()
            cmd = cmd[:1] + ["-N", "-D", "127.0.0.1:%d" % cfg["socks_port"]] + cmd[1:]
            self.log("Dialling %s@%s — SOCKS5 on :%d"
                     % (cfg["host_user"], cfg["host_ip"], cfg["socks_port"]))

            self.ssh = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )

            deadline = time.time() + 15
            while time.time() < deadline:
                if self.ssh.poll() is not None:
                    err = (self.ssh.stderr.read() or "").strip()
                    self.ssh = None
                    return False, self._explain_ssh(err)
                if port_open(cfg["socks_port"]):
                    break
                time.sleep(0.3)
            else:
                self.close_tunnel()
                return False, "SSH connected but never opened the SOCKS port."

            self.bridge = HttpBridge(cfg["http_port"], cfg["socks_port"])
            try:
                self.bridge.start()
            except OSError as exc:
                self.close_tunnel()
                return False, "Port %d is busy — change it in Advanced. (%s)" % (cfg["http_port"], exc)

            self.log("Tunnel up. HTTP bridge on :%d for mpv/FFmpeg." % cfg["http_port"])
            return True, "Tunnel up"

    def _explain_ssh(self, err):
        low = err.lower()
        if "permission denied" in low or "publickey" in low:
            return ("SSH refused the key — this machine isn't authorised on the host yet. "
                    "(ssh-copy-id %s@%s)" % (self.cfg["host_user"], self.cfg["host_ip"]))
        if "connection refused" in low:
            return "Nothing is listening on port %d — is sshd running on the host?" % self.cfg["host_ssh_port"]
        if "timed out" in low or "no route" in low:
            return "Host unreachable. Check Tailscale is up on both machines."
        if "address already in use" in low:
            return "Port %d is already taken by something else." % self.cfg["socks_port"]
        return err or "SSH exited immediately."

    def tunnel_alive(self):
        return self.ssh is not None and self.ssh.poll() is None

    def close_tunnel(self):
        if self.bridge:
            self.bridge.stop()
            self.bridge = None
        if self.ssh and self.ssh.poll() is None:
            try:
                os.killpg(os.getpgid(self.ssh.pid), signal.SIGTERM)
            except Exception:
                self.ssh.terminate()
            try:
                self.ssh.wait(timeout=5)
            except Exception:
                pass
        self.ssh = None

    # -- probes ---------------------------------------------------------- #

    def curl_ip(self, mode):
        """mode: 'direct' | 'socks' | 'bridge'. Returns (ip|None, detail)."""
        for url in IP_ECHOS:
            cmd = ["curl", "-s", "--max-time", "12"]
            if mode == "socks":
                cmd += ["--socks5-hostname", "127.0.0.1:%d" % self.cfg["socks_port"]]
            elif mode == "bridge":
                cmd += ["-x", "http://127.0.0.1:%d" % self.cfg["http_port"]]
            else:
                cmd += ["--noproxy", "*"]
            env = dict(os.environ)
            for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
                env.pop(k, None)
            rc, out, err = run(cmd + [url], timeout=15, env=env)
            ip = out.strip().splitlines()[0].strip() if out.strip() else ""
            if rc == 0 and re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip or ""):
                return ip, url
        return None, err or "all echo services failed"

    def host_public_ip(self):
        """Ask the remote machine what its own public IP is — ground truth."""
        for url in IP_ECHOS:
            rc, out, err = run(self.ssh_base() + ["curl -s --max-time 10 " + shlex.quote(url)], timeout=20)
            ip = out.strip().splitlines()[0].strip() if out.strip() else ""
            if rc == 0 and re.fullmatch(r"[0-9a-fA-F:.]{7,45}", ip or ""):
                return ip, url
        return None, "could not read the host's own IP over SSH"

    # -- watchdog -------------------------------------------------------- #

    def start_watchdog(self):
        self.watchdog = threading.Thread(target=self._watch, daemon=True)
        self.watchdog.start()

    def _watch(self):
        fails = 0
        interval = max(3, int(self.cfg["check_interval"]))
        limit = max(1, int(self.cfg["max_fails"]))
        while not self._stopping.wait(interval):
            if not self.tunnel_alive():
                fails += 1
                self.log("Watchdog: ssh process is gone (%d/%d)" % (fails, limit))
            else:
                rc, _, _ = run(
                    ["curl", "-s", "-o", "/dev/null", "--max-time", "8",
                     "--socks5-hostname", "127.0.0.1:%d" % self.cfg["socks_port"],
                     "http://connectivitycheck.gstatic.com/generate_204"],
                    timeout=12,
                )
                if rc != 0:
                    fails += 1
                    self.log("Watchdog: no traffic through the tunnel (%d/%d)" % (fails, limit))
                else:
                    fails = 0
            if fails >= limit:
                self.log("Tunnel lost. Stopping playback so nothing exits from this machine.")
                GLib.idle_add(self.on_state, "lost")
                self.stop_all(reason="lost")
                return

    # -- launching ------------------------------------------------------- #

    def _wrap(self, rt, inner):
        """Build the argv that runs a bash snippet in the chosen environment."""
        if rt.kind == "native":
            return host_prefix() + ["bash", "-lc", inner]
        if in_container() and os.environ.get("CONTAINER_ID") == rt.name:
            return ["bash", "-lc", inner]          # already inside the target
        return host_prefix() + ["distrobox", "enter", rt.name, "--", "bash", "-lc", inner]

    def write_mpv_wrapper(self, rt, proxied=True):
        """Write ~/.local/bin/mpv-proxied pointing at the HTTP bridge.

        $HOME is shared between the host and every distrobox, so one file at one
        path serves whichever environment ends up running it. Only the mpv
        invocation inside differs — a Flatpak needs launching through flatpak.
        """
        cfg = self.cfg
        if rt.kind == "native" and rt.mpv_flatpak:
            base = "flatpak run --branch=stable %s" % rt.mpv_flatpak
        else:
            base = "mpv"

        extra = cfg["mpv_extra"].strip()
        proxy_lines = ""
        if proxied:
            proxy_lines = (
                "    --http-proxy=http://127.0.0.1:%d \\\n"
                "    --ytdl-raw-options=proxy=socks5h://127.0.0.1:%d \\\n"
                % (cfg["http_port"], cfg["socks_port"])
            )
        body = (
            "#!/bin/bash\n"
            "# generated by syncplay-tunnel — rewritten on every launch\n"
            "exec %s \\\n%s%s    \"$@\"\n"
            % (base, proxy_lines, ("    %s \\\n" % extra) if extra else "")
        )

        target = Path.home() / ".local/bin/mpv-proxied"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body)
            target.chmod(0o755)
        except OSError as exc:
            self.log("Could not write the mpv wrapper: %s" % exc)
            return False
        self.log("mpv wrapper written to %s (calls: %s%s)"
                 % (target, base, "" if proxied else ", no proxy"))
        return True

    def syncplay_command(self, rt, proxied=True):
        cfg = self.cfg
        http = "http://127.0.0.1:%d" % cfg["http_port"]
        socks = "socks5h://127.0.0.1:%d" % cfg["socks_port"]
        player = "$HOME/.local/bin/mpv-proxied"

        opts = ["--player-path=%s" % player]
        if cfg["syncplay_server"].strip():
            opts += ["-a", shlex.quote(cfg["syncplay_server"].strip())]
        if cfg["syncplay_room"].strip():
            opts += ["-r", shlex.quote(cfg["syncplay_room"].strip())]
        if cfg["syncplay_user"].strip():
            opts += ["-n", shlex.quote(cfg["syncplay_user"].strip())]
        # The URL goes last, as Syncplay's positional "file". That argument is
        # also what stops it forcing its setup dialog open.
        url = cfg["play_url"].strip()
        if url:
            opts += [shlex.quote(url)]

        if rt.kind == "native" and rt.syncplay_flatpak:
            if not proxied:
                return "exec flatpak run %s %s" % (rt.syncplay_flatpak, " ".join(opts))
            envs = " ".join(
                "--env=%s=%s" % (k, v)
                for k, v in (("http_proxy", http), ("https_proxy", http),
                             ("HTTP_PROXY", http), ("HTTPS_PROXY", http),
                             ("ALL_PROXY", socks), ("all_proxy", socks),
                             ("no_proxy", "localhost,127.0.0.1"))
            )
            return "exec flatpak run %s %s %s" % (envs, rt.syncplay_flatpak, " ".join(opts))

        exports = ""
        if proxied:
            exports = (
                "export http_proxy={h} https_proxy={h} HTTP_PROXY={h} HTTPS_PROXY={h} "
                "ALL_PROXY={s} all_proxy={s} no_proxy=localhost,127.0.0.1; "
            ).format(h=http, s=socks)
        return exports + "exec syncplay " + " ".join(opts)

    def launch_player(self, rt, proxied=True):
        inner = self.syncplay_command(rt, proxied=proxied)
        cmd = self._wrap(rt, inner)
        self.active_runtime = rt

        if rt.kind == "native":
            self.log("Starting Syncplay on this system%s."
                     % ("" if proxied else " with no proxy — this machine is the exit point"))
            if rt.syncplay_flatpak:
                self.log("Flatpak sandbox note: if the custom player path is refused, run "
                         "flatpak override --user --filesystem=home %s" % rt.syncplay_flatpak)
        else:
            self.log("Starting Syncplay in distrobox '%s'." % rt.name)

        self.player = subprocess.Popen(cmd, start_new_session=True, stdin=subprocess.DEVNULL)
        threading.Thread(target=self._await_player, daemon=True).start()

    def _await_player(self):
        rc = self.player.wait()
        if self._stopping.is_set():
            return
        self.log("Syncplay closed (exit %s). Tearing the tunnel down." % rc)
        self.stop_all(reason="player-exit")

    # -- teardown -------------------------------------------------------- #

    def stop_all(self, reason="user"):
        if self._stopping.is_set():
            return
        self._stopping.set()

        # The player was started in its own session, so this takes mpv with it.
        if self.player and self.player.poll() is None:
            try:
                os.killpg(os.getpgid(self.player.pid), signal.SIGTERM)
            except Exception:
                self.player.terminate()

        rt = self.active_runtime
        # -x matches the executable name exactly, so this can never match
        # syncplay-tunnel itself.
        run(["pkill", "-x", "mpv"], timeout=10)

        if rt is not None and rt.kind == "distrobox" and self.cfg["stop_container_on_drop"]:
            if which("distrobox") or in_container():
                run(host_prefix() + ["distrobox", "stop", "--yes", rt.name], timeout=60)

        self.close_tunnel()

        if reason == "lost":
            notify(APP_NAME, "Tunnel to the host dropped. Playback stopped.", urgent=True)
            self.log("Session ended: tunnel lost.")
        else:
            self.log("Session ended.")
        GLib.idle_add(self.on_state, "idle")


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

CSS = b"""
.pill { padding: 4px 12px; border-radius: 999px; font-weight: bold; }
.pill-idle { background: alpha(currentColor, 0.12); }
.pill-work { background: alpha(#e5a50a, 0.30); }
.pill-ok   { background: alpha(#2ec27e, 0.35); }
.pill-bad  { background: alpha(#e01b24, 0.35); }
.mono { font-family: monospace; font-size: 0.9em; }
.dim { opacity: 0.65; }
.result-pass { color: #2ec27e; font-weight: bold; }
.result-fail { color: #e01b24; font-weight: bold; }
.result-warn { color: #e5a50a; font-weight: bold; }
.section-title { font-weight: bold; }
"""


class Row:
    """One line in the verification results list."""

    def __init__(self, listbox, text):
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.set_margin_top(4)
        self.box.set_margin_bottom(4)
        self.box.set_margin_start(8)
        self.box.set_margin_end(8)
        self.mark = Gtk.Label(label="…")
        self.mark.set_size_request(20, -1)
        self.label = Gtk.Label(label=text, xalign=0)
        self.label.set_wrap(True)
        self.label.set_hexpand(True)
        self.label.set_max_width_chars(50)
        self.box.append(self.mark)
        self.box.append(self.label)
        row = Gtk.ListBoxRow()
        row.set_child(self.box)
        row.set_activatable(False)
        listbox.append(row)

    def set(self, state, text=None):
        marks = {"pass": "✓", "fail": "✗", "warn": "!", "busy": "…"}
        classes = {"pass": "result-pass", "fail": "result-fail", "warn": "result-warn", "busy": "dim"}
        self.mark.set_text(marks.get(state, "·"))
        for c in ("result-pass", "result-fail", "result-warn", "dim"):
            self.mark.remove_css_class(c)
        self.mark.add_css_class(classes.get(state, "dim"))
        if text is not None:
            self.label.set_text(text)


class Window(Gtk.ApplicationWindow):
    def __init__(self, app, cfg, autolaunch=False):
        super().__init__(application=app, title=APP_NAME)
        self.cfg = cfg
        self.set_default_size(720, 820)
        self.session = Session(cfg, self.log, self.on_state)
        self.verified = False
        self.busy = False

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.peers = []
        self.tailscale_self = None
        self.host_count_timer = None

        header = Gtk.HeaderBar()
        self.header = header
        self.pill = Gtk.Label(label="Not connected")
        self.pill.add_css_class("pill")
        self.pill.add_css_class("pill-idle")
        header.pack_end(self.pill)
        self.set_titlebar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(outer)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.append(scroller)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for side in ("top", "bottom", "start", "end"):
            getattr(page, "set_margin_" + side)(16)
        scroller.set_child(page)

        page.append(self._build_roles())
        page.append(self._build_what())
        page.append(self._build_where())
        page.append(self._build_verify())
        page.append(self._build_advanced())
        page.append(self._build_log())

        outer.append(self._build_actions())

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        header.set_title_widget(switcher)
        self.on_role_changed()

        self._env_banner()
        threading.Thread(target=self._scan_worker, daemon=True).start()
        self.refresh_peers()
        if autolaunch:
            GLib.timeout_add(600, self._auto)

    # -- sections -------------------------------------------------------- #

    def _frame(self, title):
        frame = Gtk.Frame()
        lbl = Gtk.Label(label=title)
        lbl.add_css_class("section-title")
        frame.set_label_widget(lbl)
        grid = Gtk.Grid(column_spacing=10, row_spacing=8)
        grid.set_hexpand(True)
        for side in ("top", "bottom", "start", "end"):
            getattr(grid, "set_margin_" + side)(12)
        frame.set_child(grid)
        return frame, grid

    def _entry(self, grid, row, label, key, placeholder="", width=1):
        lab = Gtk.Label(label=label, xalign=0)
        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_text(str(self.cfg[key]))
        if placeholder:
            entry.set_placeholder_text(placeholder)
        grid.attach(lab, 0, row, 1, 1)
        grid.attach(entry, 1, row, width, 1)
        setattr(self, "e_" + key, entry)
        return entry

    def _spin(self, grid, row, label, key, lo, hi):
        lab = Gtk.Label(label=label, xalign=0)
        spin = Gtk.SpinButton.new_with_range(lo, hi, 1)
        spin.set_value(int(self.cfg[key]))
        spin.set_hexpand(True)
        grid.attach(lab, 0, row, 1, 1)
        grid.attach(spin, 1, row, 1, 1)
        setattr(self, "s_" + key, spin)
        return spin

    def _switch(self, grid, row, label, key, col=0):
        lab = Gtk.Label(label=label, xalign=0)
        sw = Gtk.Switch()
        sw.set_active(bool(self.cfg[key]))
        sw.set_halign(Gtk.Align.START)
        grid.attach(lab, col, row, 1, 1)
        grid.attach(sw, col + 1, row, 1, 1)
        setattr(self, "w_" + key, sw)
        return sw

    def _build_roles(self):
        """Two modes: this machine is the exit point, or it dials one."""
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.add_titled(self._build_client_page(), "client", "Client")
        self.stack.add_titled(self._build_host_page(), "host", "Host")
        self.stack.set_visible_child_name(
            "host" if self.cfg["role"] == "host" else "client")
        self.stack.connect("notify::visible-child-name", self.on_role_changed)
        return self.stack

    # -- client side ----------------------------------------------------- #

    def _build_client_page(self):
        frame, grid = self._frame("Pick a host to route through")

        self.peer_list = Gtk.ListBox()
        self.peer_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.peer_list.add_css_class("boxed-list")
        self.peer_list.set_hexpand(True)
        self.peer_list.connect("row-selected", self.on_peer_selected)

        holder = Gtk.ScrolledWindow()
        holder.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        holder.set_min_content_height(120)
        holder.set_max_content_height(220)
        holder.set_hexpand(True)
        holder.set_child(self.peer_list)
        grid.attach(holder, 0, 0, 3, 1)

        refresh = Gtk.Button(label="Refresh list")
        refresh.connect("clicked", lambda _b: self.refresh_peers())
        grid.attach(refresh, 0, 1, 1, 1)

        keybtn = Gtk.Button(label="Set up SSH key…")
        keybtn.connect("clicked", lambda _b: self.open_key_dialog())
        grid.attach(keybtn, 1, 1, 1, 1)

        self._entry(grid, 2, "Host address", "host_ip", "100.x.x.x, or a hostname")
        self._entry(grid, 3, "SSH user on the host", "host_user", "account name on the exit machine")

        note = Gtk.Label(
            label="Everything Syncplay and mpv fetch will leave from the host you pick here. "
                  "The tunnel dials outward, so nothing needs forwarding at either end.",
            xalign=0)
        note.set_wrap(True)
        note.set_hexpand(True)
        note.set_max_width_chars(60)
        note.add_css_class("dim")
        grid.attach(note, 0, 4, 3, 1)
        return frame

    def refresh_peers(self):
        threading.Thread(target=self._peers_worker, daemon=True).start()

    def _peers_worker(self):
        me, peers = tailscale_status()

        def apply():
            self.tailscale_self = me
            self.peers = peers
            child = self.peer_list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.peer_list.remove(child)
                child = nxt

            if not peers:
                row = Gtk.ListBoxRow()
                lbl = Gtk.Label(
                    label="No Tailscale peers found. Type the address below instead.",
                    xalign=0)
                lbl.set_margin_top(8); lbl.set_margin_bottom(8)
                lbl.set_margin_start(10); lbl.set_margin_end(10)
                lbl.add_css_class("dim")
                row.set_child(lbl)
                row.set_selectable(False)
                self.peer_list.append(row)
            else:
                current = self.e_host_ip.get_text().strip()
                for p in peers:
                    row = Gtk.ListBoxRow()
                    lbl = Gtk.Label(label=p.label(), xalign=0)
                    lbl.set_margin_top(8); lbl.set_margin_bottom(8)
                    lbl.set_margin_start(10); lbl.set_margin_end(10)
                    if not p.online:
                        lbl.add_css_class("dim")
                    row.set_child(lbl)
                    row.peer = p
                    self.peer_list.append(row)
                    if p.ip == current:
                        self.peer_list.select_row(row)

            if me:
                self.host_self_label.set_text("%s  ·  %s" % (me.name, me.ip))
                self.cfg["client_ip"] = me.ip
            self.refresh_host_page()
            return False

        GLib.idle_add(apply)

    def on_peer_selected(self, _list, row):
        peer = getattr(row, "peer", None) if row is not None else None
        if peer is None:
            return
        self.e_host_ip.set_text(peer.ip)
        if not peer.online:
            self.log("%s is offline right now — the route check will fail until it wakes." % peer.name)

    # -- host side ------------------------------------------------------- #

    def _build_host_page(self):
        frame, grid = self._frame("This machine is the exit point")

        self.host_self_label = Gtk.Label(label="checking…", xalign=0)
        self.host_self_label.set_hexpand(True)
        grid.attach(Gtk.Label(label="Tailscale name", xalign=0), 0, 0, 1, 1)
        grid.attach(self.host_self_label, 1, 0, 2, 1)

        self.host_sshd_label = Gtk.Label(label="checking…", xalign=0)
        self.host_sshd_label.set_hexpand(True)
        grid.attach(Gtk.Label(label="SSH server", xalign=0), 0, 1, 1, 1)
        grid.attach(self.host_sshd_label, 1, 1, 2, 1)

        self.host_keys_label = Gtk.Label(label="checking…", xalign=0)
        self.host_keys_label.set_hexpand(True)
        grid.attach(Gtk.Label(label="Keys installed", xalign=0), 0, 2, 1, 1)
        grid.attach(self.host_keys_label, 1, 2, 2, 1)

        self.host_conn_label = Gtk.Label(label="checking…", xalign=0)
        self.host_conn_label.set_hexpand(True)
        self.host_conn_label.set_wrap(True)
        self.host_conn_label.set_max_width_chars(50)
        grid.attach(Gtk.Label(label="Connected now", xalign=0), 0, 3, 1, 1)
        grid.attach(self.host_conn_label, 1, 3, 2, 1)

        self.host_share = Gtk.Entry()
        self.host_share.set_editable(False)
        self.host_share.set_hexpand(True)
        grid.attach(Gtk.Label(label="Give this to the client", xalign=0), 0, 4, 1, 1)
        grid.attach(self.host_share, 1, 4, 1, 1)
        copy = Gtk.Button(label="Copy")
        copy.connect("clicked", self.on_copy_share)
        grid.attach(copy, 2, 4, 1, 1)

        recheck = Gtk.Button(label="Re-check")
        recheck.connect("clicked", lambda _b: self.refresh_peers())
        grid.attach(recheck, 0, 5, 1, 1)

        self.btn_restrict = Gtk.Button(label="Restrict existing keys")
        self.btn_restrict.connect("clicked", self.on_restrict_keys)
        self.btn_restrict.set_visible(False)
        grid.attach(self.btn_restrict, 1, 5, 2, 1)

        note = Gtk.Label(
            label="Traffic already leaves from here, so no tunnel is opened in this mode. "
                  "Start watching launches Syncplay directly.",
            xalign=0)
        note.set_wrap(True)
        note.set_hexpand(True)
        note.set_max_width_chars(60)
        note.add_css_class("dim")
        grid.attach(note, 0, 6, 3, 1)
        return frame

    def _count_worker(self):
        """Who is on our sshd right now. Runs off the main loop: ss is a fork."""
        port = int(self.cfg["host_ssh_port"] or 22)
        addrs = ssh_clients(port)
        names = {p.ip: p.name for p in self.peers}

        def apply():
            if not addrs:
                self.host_conn_label.set_text("nobody connected yet")
                if self.cfg["role"] == "host":
                    self.set_pill("Host — 0 connected", "ok")
                return False
            shown = []
            for a in addrs:
                name = names.get(a)
                tail = "" if is_tailscale_addr(a) else " (not Tailscale)"
                shown.append(("%s (%s)%s" % (name, a, tail)) if name else (a + tail))
            self.host_conn_label.set_text(
                "%d connected — %s" % (len(addrs), ", ".join(shown)))
            if self.cfg["role"] == "host":
                self.set_pill("Host — %d connected" % len(addrs), "ok")
            return False

        GLib.idle_add(apply)

    def _count_tick(self):
        """Keep polling only while the host page is the one on screen."""
        if self.cfg["role"] != "host":
            self.host_count_timer = None
            return False
        threading.Thread(target=self._count_worker, daemon=True).start()
        return True

    def refresh_host_page(self):
        user = getpass.getuser()
        me = getattr(self, "tailscale_self", None)
        addr = me.ip if me else socket.gethostname()

        port = int(self.cfg["host_ssh_port"] or 22)
        listening = port_open(port, timeout=1.5)
        self.host_sshd_label.set_text(
            "listening on port %d" % port if listening
            else "nothing on port %d — start sshd to accept clients" % port)

        keys = Path.home() / ".ssh/authorized_keys"
        try:
            lines = [l for l in keys.read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
            count = len(lines)
            # No options field means that key is a full shell login here.
            loose = len([l for l in lines
                         if l.split()[0].startswith(("ssh-", "ecdsa-", "sk-"))])
            text = "%d client key%s authorised" % (count, "" if count == 1 else "s")
            if loose:
                text += " — %d unrestricted (full shell)" % loose
            self.host_keys_label.set_text(text)
            self.btn_restrict.set_visible(bool(loose))
        except OSError:
            self.host_keys_label.set_text("no authorized_keys yet — no client can connect")
            self.btn_restrict.set_visible(False)

        self.host_share.set_text("%s@%s" % (user, addr))
        if me is None:
            self.host_self_label.set_text("Tailscale not reporting — %s" % socket.gethostname())

    def on_restrict_keys(self, _btn=None):
        """Retrofit keys enrolled before options were being set.

        Confirmed first: this changes what other people's machines are allowed
        to do on this one, and getting it wrong locks them out.
        """
        dlg = Gtk.Window(title="Restrict authorised keys", transient_for=self, modal=True)
        dlg.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(16)
        dlg.set_child(box)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Limit every authorised key to port forwarding</b>")
        box.append(head)

        why = Gtk.Label(
            label="Each key in ~/.ssh/authorized_keys with no options set is a full "
                  "shell login on this machine for whoever holds the matching private "
                  "key.\n\nThis prefixes those lines with "
                  "'" + KEY_OPTIONS + "': no pty, no agent or X11 forwarding, port "
                  "forwarding still allowed, so the tunnel and the route check keep "
                  "working. Nothing is removed and the old file is kept as "
                  "authorized_keys.bak.",
            xalign=0)
        why.set_wrap(True)
        why.set_max_width_chars(54)
        box.append(why)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        confirm = Gtk.Button(label="Restrict them")
        confirm.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        cancel.connect("clicked", lambda *_a: dlg.close())

        def go(*_a):
            dlg.close()
            changed, total = restrict_local_keys(log=self.log)
            if changed:
                notify(APP_NAME, "%d of %d authorised keys restricted." % (changed, total))
            self.refresh_host_page()

        confirm.connect("clicked", go)
        dlg.present()

    def on_copy_share(self, btn):
        text = self.host_share.get_text()
        try:
            btn.get_clipboard().set(text)
        except Exception:
            try:
                from gi.repository import Gdk as _Gdk
                _Gdk.Display.get_default().get_clipboard().set(text)
            except Exception:
                self.log("Could not reach the clipboard. The line is: %s" % text)
                return
        self.log("Copied: %s" % text)

    def on_role_changed(self, *_a):
        role = self.stack.get_visible_child_name() or "client"
        self.cfg["role"] = role
        host_mode = role == "host"
        self.frame_verify.set_visible(not host_mode)
        self.btn_launch.set_label("Start watching")
        if host_mode:
            self.set_pill("Host — no tunnel", "ok")
            self.log("Host mode: traffic already exits here, so no tunnel will be opened.")
            self.refresh_host_page()
            self._count_tick()
            if getattr(self, "host_count_timer", None) is None:
                self.host_count_timer = GLib.timeout_add_seconds(5, self._count_tick)
        else:
            self.set_pill("Not connected", "idle")
        return False

    def _build_where(self):
        frame, grid = self._frame("Where to play")

        self.runtimes = []

        # A dropdown over a plain Gtk.StringList. No SignalListItemFactory and
        # no expression: the default label factory is the part that renders the
        # same on every GTK build, which the nested list-in-a-scroller did not.
        self.env_drop = Gtk.DropDown()
        self.env_drop.set_model(Gtk.StringList.new(["scanning…"]))
        self.env_drop.set_hexpand(True)
        self.env_drop.connect("notify::selected", self.on_env_selected)
        grid.attach(self.env_drop, 0, 0, 3, 1)

        self.env_detail = Gtk.Label(label="", xalign=0)
        self.env_detail.set_wrap(True)
        self.env_detail.set_hexpand(True)
        self.env_detail.set_max_width_chars(56)
        grid.attach(self.env_detail, 0, 1, 2, 1)

        self.btn_install = Gtk.Button(label="Install missing")
        self.btn_install.connect("clicked", self.on_install_missing)
        self.btn_install.set_visible(False)
        grid.attach(self.btn_install, 2, 1, 1, 1)

        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", self.on_rescan)
        grid.attach(rescan, 0, 2, 1, 1)

        self._switch(grid, 2, "Start stopped containers while scanning",
                     "scan_stopped", col=1)

        self.where_note = Gtk.Label(
            label="Looking for Syncplay and mpv on this system and in every distrobox…",
            xalign=0,
        )
        self.where_note.set_wrap(True)
        self.where_note.set_hexpand(True)
        self.where_note.set_max_width_chars(60)
        self.where_note.add_css_class("dim")
        grid.attach(self.where_note, 0, 3, 3, 1)
        return frame

    def _build_what(self):
        frame, grid = self._frame("What to play")

        self._entry(grid, 0, "URL", "play_url",
                    "https://…  — leave blank to pick a file in Syncplay yourself",
                    width=2)

        note = Gtk.Label(
            label="A URL here is handed to Syncplay, which puts it on the shared "
                  "playlist — so it starts for everyone in the room, whoever typed it. "
                  "It also skips Syncplay's setup dialog, which only stays away while a "
                  "URL is set.",
            xalign=0)
        note.set_wrap(True)
        note.set_hexpand(True)
        note.set_max_width_chars(60)
        note.add_css_class("dim")
        grid.attach(note, 0, 1, 3, 1)
        return frame

    def on_rescan(self, _btn=None):
        self.collect()
        self.where_note.set_text("Scanning…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        found = scan_runtimes(allow_start=bool(self.cfg["scan_stopped"]), log=self.log)

        def apply():
            self.runtimes = found

            labels = [rt.label() for rt in found] or ["Nothing found to launch from"]
            self.env_drop.set_model(Gtk.StringList.new(labels))

            # keep the saved choice if it still exists, else take the best one
            want = self.cfg["runtime_kind"]
            if want == "distrobox" and self.cfg["container"]:
                want = "distrobox:" + self.cfg["container"]

            index = None
            for i, rt in enumerate(found):
                if rt.key == want:
                    index = i
                    break
                if index is None and rt.complete:
                    index = i
            # A fresh model starts unselected on some builds, so say it outright.
            self.env_drop.set_selected(index or 0)
            self.env_drop.set_sensitive(bool(found))
            self.on_env_selected()

            complete = [r for r in found if r.complete]
            if not found:
                self.where_note.set_text("No launch environment found.")
            elif complete:
                self.where_note.set_text(
                    "%d of %d environments have both Syncplay and mpv. Those are listed first."
                    % (len(complete), len(found)))
            else:
                self.where_note.set_text(
                    "Nothing has both Syncplay and mpv yet. Install them where you want to "
                    "watch, then press Rescan.")
            return False

        GLib.idle_add(apply)
        for r in found:
            self.log("Found %s" % r.label())

    def selected_runtime(self):
        index = self.env_drop.get_selected()
        if index is None or index == INVALID_SELECTION or index >= len(self.runtimes):
            return None
        return self.runtimes[index]

    def missing_in(self, rt):
        missing = []
        if not rt.has_syncplay:
            missing.append("syncplay")
        if not rt.has_mpv:
            missing.append("mpv")
        return missing

    def on_env_selected(self, *_a):
        rt = self.selected_runtime()
        if rt is None:
            self.env_detail.set_text("")
            self.btn_install.set_visible(False)
            return
        where = "this system" if rt.kind == "native" else "distrobox '%s'" % rt.name
        missing = self.missing_in(rt)
        for c in ("result-warn", "dim"):
            self.env_detail.remove_css_class(c)
        if rt.has_syncplay is None:
            self.env_detail.set_text("%s has not been scanned — it is stopped." % where)
            self.env_detail.add_css_class("dim")
            self.btn_install.set_visible(False)
            return
        if not missing:
            self.env_detail.set_text("Ready: %s" % rt.status_text())
            self.env_detail.add_css_class("dim")
            self.btn_install.set_visible(False)
            return
        self.env_detail.set_text("%s is missing from %s."
                                 % (" and ".join(missing), where))
        self.env_detail.add_css_class("result-warn")
        self.btn_install.set_label("Install " + " + ".join(missing))
        self.btn_install.set_visible(True)

    def on_install_missing(self, _btn=None):
        rt = self.selected_runtime()
        if rt is None:
            return
        missing = self.missing_in(rt)
        argv, how, _term = install_plan(rt, missing)
        if argv is None:
            self.log(how)
            self.env_detail.set_text(how)
            return
        self.btn_install.set_sensitive(False)
        self.log("Installing %s — %s." % (" and ".join(missing), how))

        def work():
            ok = stream_command(argv, self.log)
            self.log("Install finished." if ok else
                     "Install failed. The command above shows why.")

            def done():
                self.btn_install.set_sensitive(True)
                return False

            GLib.idle_add(done)
            if ok:
                self._scan_worker()

        threading.Thread(target=work, daemon=True).start()

    def _build_verify(self):
        frame, grid = self._frame("Route check")

        btn = Gtk.Button(label="Check the route")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self.on_test)
        self.btn_test = btn
        grid.attach(btn, 0, 0, 3, 1)

        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        self.results.set_hexpand(True)
        grid.attach(self.results, 0, 1, 3, 1)

        self.verdict = Gtk.Label(label="Not checked yet.", xalign=0)
        self.verdict.set_wrap(True)
        self.verdict.set_hexpand(True)
        self.verdict.set_max_width_chars(60)
        grid.attach(self.verdict, 0, 2, 3, 1)
        self.frame_verify = frame
        return frame

    def _build_advanced(self):
        exp = Gtk.Expander(label="Advanced")
        frame, grid = self._frame("")
        frame.set_label_widget(None)

        self._spin(grid, 0, "SOCKS5 port", "socks_port", 1, 65535)
        self._spin(grid, 1, "HTTP bridge port", "http_port", 1, 65535)
        self._spin(grid, 2, "SSH port", "host_ssh_port", 1, 65535)
        self._spin(grid, 3, "Watchdog interval (s)", "check_interval", 3, 300)
        self._spin(grid, 4, "Failures before stopping", "max_fails", 1, 20)
        self._entry(grid, 5, "Syncplay server", "syncplay_server", "syncplay.pl:8997")
        self._entry(grid, 6, "Room", "syncplay_room", "")
        self._entry(grid, 7, "Display name", "syncplay_user", "")
        self._entry(grid, 8, "Extra mpv flags", "mpv_extra", "--cache=yes --demuxer-max-bytes=200M")
        self._switch(grid, 9, "Require a verified route before launching", "require_verified")
        self._switch(grid, 10, "Stop the container when the tunnel drops", "stop_container_on_drop")
        self._switch(grid, 11, "Skip Syncplay's setup dialog", "skip_syncplay_dialog")
        self._switch(grid, 12, "Trust the domain of the URL being played", "trust_play_domain")

        exp.set_child(frame)
        return exp

    def _build_log(self):
        frame, grid = self._frame("Activity")

        sw = Gtk.ScrolledWindow()
        # Horizontal NEVER is what forces the view to take the frame's width and
        # wrap into it. With AUTOMATIC the scroller collapses to its minimum.
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(180)
        sw.set_min_content_width(360)
        sw.set_hexpand(True)
        sw.set_vexpand(False)

        self.logview = Gtk.TextView()
        self.logview.set_editable(False)
        self.logview.set_cursor_visible(False)
        self.logview.set_monospace(True)
        self.logview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.logview.set_hexpand(True)
        self.logview.set_left_margin(8)
        self.logview.set_right_margin(8)
        self.logview.set_top_margin(6)
        self.logview.set_bottom_margin(6)
        self.logbuf = self.logview.get_buffer()

        sw.set_child(self.logview)
        grid.attach(sw, 0, 0, 1, 1)
        return frame

    def _build_actions(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, "set_margin_" + side)(12)

        save = Gtk.Button(label="Save settings")
        save.connect("clicked", self.on_save)
        bar.append(save)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self.btn_stop = Gtk.Button(label="Stop session")
        self.btn_stop.connect("clicked", self.on_stop)
        self.btn_stop.set_sensitive(False)
        bar.append(self.btn_stop)

        self.btn_launch = Gtk.Button(label="Start watching")
        self.btn_launch.add_css_class("suggested-action")
        self.btn_launch.connect("clicked", self.on_launch)
        bar.append(self.btn_launch)
        return bar

    # -- plumbing -------------------------------------------------------- #

    def _env_banner(self):
        here = current_container()
        where = ("inside container '%s'" % here) if here else "on the host system"
        tools = []
        for t in ("ssh", "curl", "tailscale"):
            if not which(t):
                tools.append(t)
        self.log("Running %s." % where)
        if tools:
            self.log("Not found on PATH: %s" % ", ".join(tools))
        if in_container() and not which("distrobox-host-exec"):
            self.log("distrobox-host-exec is missing, so other containers can't be listed "
                     "from in here.")

    def collect(self):
        for key in DEFAULTS:
            w = getattr(self, "e_" + key, None)
            if w is not None:
                self.cfg[key] = w.get_text().strip()
                continue
            w = getattr(self, "s_" + key, None)
            if w is not None:
                self.cfg[key] = int(w.get_value())
                continue
            w = getattr(self, "w_" + key, None)
            if w is not None:
                self.cfg[key] = bool(w.get_active())

        rt = self.selected_runtime()
        if rt is not None:
            self.cfg["runtime_kind"] = rt.kind
            self.cfg["container"] = rt.name

    def log(self, text):
        line = "[%s] %s\n" % (stamp(), text)

        def write():
            self.logbuf.insert(self.logbuf.get_end_iter(), line)
            mark = self.logbuf.create_mark(None, self.logbuf.get_end_iter(), False)
            self.logview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
            return False

        GLib.idle_add(write)
        try:
            with LOG_FILE.open("a") as fh:
                fh.write("[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), text))
        except Exception:
            pass

    def set_pill(self, text, kind):
        for c in ("pill-idle", "pill-work", "pill-ok", "pill-bad"):
            self.pill.remove_css_class(c)
        self.pill.add_css_class("pill-" + kind)
        self.pill.set_text(text)

    def on_state(self, state):
        if state == "idle":
            self.set_pill("Host — no tunnel" if self.cfg["role"] == "host"
                          else "Not connected", "ok" if self.cfg["role"] == "host" else "idle")
            self.btn_stop.set_sensitive(False)
            self.btn_launch.set_sensitive(True)
            self.verified = False
        elif state == "lost":
            self.set_pill("Tunnel lost", "bad")
            self.verdict.set_text("The tunnel dropped mid-session. Everything was stopped.")
        elif state == "playing-host":
            self.set_pill("Playing locally", "ok")
            self.btn_stop.set_sensitive(True)
            self.btn_launch.set_sensitive(False)
        elif state == "playing":
            self.set_pill("Streaming through host", "ok")
            self.btn_stop.set_sensitive(True)
            self.btn_launch.set_sensitive(False)
        return False

    def set_busy(self, busy):
        self.busy = busy
        self.btn_test.set_sensitive(not busy)
        return False

    # -- actions --------------------------------------------------------- #

    def open_key_dialog(self, reason=None):
        """Ask for the host password once and install our public key.

        The password stays in a bytearray that gets zeroed as soon as
        ssh-copy-id is done. It is never written to the config, the log, the
        environment, or a command line.
        """
        self.collect()
        user = self.cfg["host_user"].strip()
        host = self.cfg["host_ip"].strip()
        if not host or not user:
            self.log("Fill in the host address and SSH user before setting up the key.")
            return False

        dlg = Gtk.Window(title="Authorise this machine", transient_for=self, modal=True)
        dlg.set_default_size(440, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(16)
        dlg.set_child(box)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>%s@%s doesn't trust this machine yet</b>" % (user, host))
        box.append(head)

        why = Gtk.Label(
            label=(reason or "SSH turned down the key.") +
                  "\n\nEnter the password for that account once. This copies your public key "
                  "over, and the host stops asking after that.",
            xalign=0)
        why.set_wrap(True)
        why.set_max_width_chars(52)
        box.append(why)

        pw = Gtk.PasswordEntry()
        pw.set_show_peek_icon(True)
        pw.set_property("placeholder-text", "password for %s@%s" % (user, host))
        pw.set_hexpand(True)
        box.append(pw)

        status = Gtk.Label(label="", xalign=0)
        status.set_wrap(True)
        status.set_max_width_chars(52)
        box.append(status)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        confirm = Gtk.Button(label="Copy key to host")
        confirm.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        def close(*_a):
            pw.set_text("")
            dlg.close()

        cancel.connect("clicked", close)

        def go(*_a):
            secret = bytearray(pw.get_text().encode())
            pw.set_text("")
            if not secret:
                status.set_text("Enter the password first.")
                return
            confirm.set_sensitive(False)
            cancel.set_sensitive(False)
            pw.set_sensitive(False)
            status.set_text("Copying the key over…")

            def work():
                try:
                    ok, msg = ssh_copy_id(user, host, int(self.cfg["host_ssh_port"]),
                                          secret, log=self.log)
                finally:
                    for i in range(len(secret)):
                        secret[i] = 0

                if ok:
                    rc, _, _ = run(self.session.ssh_base() + ["true"], timeout=25)
                    if rc != 0:
                        ok = False
                        msg = "The key copied, but SSH still won't log in without a password."
                    else:
                        # Narrow the key down now that it is proven to work: an
                        # unrestricted entry is a full shell on the host for
                        # anyone who ends up holding this private key.
                        pub = find_ssh_key()
                        if pub is not None:
                            restrict_authorized_key(self.session.ssh_base(), pub,
                                                    log=self.log)

                def done():
                    status.set_text(msg)
                    self.log(msg)
                    cancel.set_sensitive(True)
                    cancel.set_label("Close")
                    if ok:
                        confirm.set_label("Check the route")
                        confirm.set_sensitive(True)
                        confirm.disconnect_by_func(go)
                        confirm.connect("clicked", lambda *_x: (close(), self.on_test(None)))
                    else:
                        pw.set_sensitive(True)
                        confirm.set_sensitive(True)
                    return False

                GLib.idle_add(done)

            threading.Thread(target=work, daemon=True).start()

        confirm.connect("clicked", go)
        pw.connect("activate", go)
        dlg.present()
        return False

    def on_save(self, _btn):
        self.collect()
        try:
            self.cfg.save()
            self.log("Settings saved to %s" % CONFIG_FILE)
        except Exception as exc:
            self.log("Settings not saved: %s" % exc)

    def on_test(self, _btn):
        self.collect()
        if not self.cfg["host_ip"]:
            self.log("Enter the host IP first.")
            return
        self.set_busy(True)
        self.set_pill("Checking", "work")
        self.verified = False
        self.verdict.set_text("Checking…")

        child = self.results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt

        self.rows = {
            "peer": Row(self.results, "Host is reachable on Tailscale"),
            "ssh": Row(self.results, "SSH accepts our key"),
            "tunnel": Row(self.results, "SOCKS5 tunnel is listening"),
            "direct": Row(self.results, "This machine's own public IP"),
            "socks": Row(self.results, "Public IP seen through SOCKS5"),
            "bridge": Row(self.results, "Public IP seen through the mpv/FFmpeg path"),
            "host": Row(self.results, "Host's own public IP (asked over SSH)"),
        }
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _row(self, key, state, text=None):
        GLib.idle_add(lambda: (self.rows[key].set(state, text), False)[1])

    def _test_worker(self):
        s = self.session
        cfg = self.cfg

        # 1. peer reachable
        if which("tailscale"):
            rc, out, _ = run(["tailscale", "ping", "-c", "1", "--timeout", "5s", cfg["host_ip"]], timeout=15)
            if rc == 0:
                relay = "via relay" if "via DERP" in out else "direct"
                self._row("peer", "pass", "Host answers on Tailscale (%s)" % relay)
            else:
                self._row("peer", "warn", "Tailscale ping failed — trying SSH anyway")
        else:
            self._row("peer", "warn", "tailscale not installed here; skipped")

        # 2. ssh
        rc, _, err = run(s.ssh_base() + ["true"], timeout=25)
        if rc != 0:
            reason = s._explain_ssh(err)
            self._row("ssh", "fail", "SSH failed — %s" % reason)
            for k in ("tunnel", "direct", "socks", "bridge", "host"):
                self._row(k, "fail", None)
            if "ssh-copy-id" in reason:
                GLib.idle_add(self.open_key_dialog, "SSH turned down the key.")
                return self._finish(False, "The host hasn't got this machine's key yet.")
            return self._finish(False, "SSH could not reach the host, so nothing else was tested.")
        self._row("ssh", "pass", "SSH accepts our key")

        # 3. tunnel
        ok, msg = s.open_tunnel()
        if not ok:
            self._row("tunnel", "fail", msg)
            return self._finish(False, msg)
        self._row("tunnel", "pass", "SOCKS5 on :%d, HTTP bridge on :%d" % (cfg["socks_port"], cfg["http_port"]))

        if True:
            direct, _ = s.curl_ip("direct")
            self._row("direct", "pass" if direct else "warn",
                      "This machine: %s" % (direct or "unknown"))

            socks_ip, _ = s.curl_ip("socks")
            if not socks_ip:
                self._row("socks", "fail", "Nothing came back through the SOCKS5 tunnel")
                return self._finish(False, "The tunnel is open but no traffic gets through it.")
            self._row("socks", "pass", "Through SOCKS5: %s" % socks_ip)

            bridge_ip, _ = s.curl_ip("bridge")
            if not bridge_ip:
                self._row("bridge", "fail", "The HTTP bridge did not answer — mpv would leak")
                return self._finish(False, "The mpv/FFmpeg path is not working, so playback would go out from here.")
            self._row("bridge", "pass", "Through the mpv path: %s" % bridge_ip)

            host_ip, _ = s.host_public_ip()
            if not host_ip:
                self._row("host", "warn", "Could not read the host's own IP")
            else:
                self._row("host", "pass", "Host reports: %s" % host_ip)

            # verdict
            if direct and socks_ip == direct:
                return self._finish(False, "Leak: traffic through the tunnel still shows this machine's IP (%s)." % direct)
            if bridge_ip != socks_ip:
                return self._finish(False, "The two paths exit differently (%s vs %s). Playback would not match the tunnel."
                                    % (socks_ip, bridge_ip))
            if host_ip and socks_ip != host_ip:
                return self._finish(False, "Traffic exits from %s, but the host says its address is %s. That is not the host."
                                    % (socks_ip, host_ip))
            if not host_ip:
                return self._finish(True, "Traffic exits from %s, which is not this machine — but the host's own IP could "
                                          "not be confirmed." % socks_ip, strong=False)
        return self._finish(True, "Confirmed: everything exits from the host at %s. This machine's %s is not exposed."
                            % (host_ip, direct or "address"))

    def _finish(self, ok, message, strong=True):
        def apply():
            self.verified = ok and strong
            self.verdict.set_text(message)
            self.set_pill("Route verified" if ok else "Not verified", "ok" if ok else "bad")
            self.set_busy(False)
            return False

        GLib.idle_add(apply)
        self.log(message)
        if not ok:
            self.session.close_tunnel()
        return None

    def on_launch(self, _btn):
        self.collect()
        host_mode = self.cfg["role"] == "host"
        if not host_mode and self.cfg["require_verified"] and not self.verified:
            self.verdict.set_text("Check the route first — or turn the requirement off in Advanced.")
            self.log("Launch blocked: the route has not been verified in this session.")
            return
        rt = self.selected_runtime()
        if rt is None:
            self.log("Pick an environment under Where to play first.")
            return
        if not rt.complete:
            missing = []
            if not rt.has_syncplay:
                missing.append("Syncplay")
            if not rt.has_mpv:
                missing.append("mpv")
            where = "this system" if rt.kind == "native" else rt.name
            note = "%s is missing from %s." % (" and ".join(missing) or "Nothing", where)
            self.verdict.set_text(note)
            self.log(note + " Use the install button under Where to play, or pick "
                            "another environment.")
            return

        self.btn_launch.set_sensitive(False)
        threading.Thread(target=self._launch_worker, args=(rt, host_mode),
                         daemon=True).start()

    def _prepare_syncplay(self, rt):
        """Make Syncplay start playing on its own, if that was asked for.

        $HOME is shared with every distrobox, so one config file covers all of
        them — the same reason the mpv wrapper is written once.
        """
        url = self.cfg["play_url"].strip()
        if not self.cfg["skip_syncplay_dialog"]:
            return
        if not url:
            self.log("No URL set, so Syncplay will show its setup dialog — it forces "
                     "the dialog whenever no file is given, whatever the config says.")
            return
        prepare_syncplay_ini(url, bool(self.cfg["trust_play_domain"]), log=self.log)
        self.log("Syncplay will open %s and put it on the shared playlist for the room."
                 % url)
        if not self.cfg["trust_play_domain"]:
            self.log("Whoever else is in the room still has to confirm that domain once.")
        self.log("Note: Syncplay saves the player path it was given, so a later plain "
                 "'syncplay' run will also use the proxied mpv wrapper.")

    def _launch_worker(self, rt, host_mode=False):
        s = self.session
        s.begin()
        if not host_mode:
            ok, msg = s.open_tunnel()
            if not ok:
                self.log(msg)
                GLib.idle_add(self.on_state, "idle")
                return
        if not s.write_mpv_wrapper(rt, proxied=not host_mode):
            GLib.idle_add(self.on_state, "idle")
            return
        self._prepare_syncplay(rt)
        if not host_mode:
            s.start_watchdog()
        s.launch_player(rt, proxied=not host_mode)
        GLib.idle_add(self.on_state, "playing-host" if host_mode else "playing")
        if not host_mode:
            notify(APP_NAME, "Connected. Syncplay is starting.")

    def on_stop(self, _btn):
        threading.Thread(target=lambda: self.session.stop_all("user"), daemon=True).start()

    def _auto(self):
        """--launch: wait for the scan, check the route, then start if it passed.

        Every wait happens off the main loop so the window stays responsive.
        """
        def drive():
            deadline = time.time() + 25
            while not self.runtimes and time.time() < deadline:
                time.sleep(0.2)
            GLib.idle_add(lambda: (self.on_test(None), False)[1])
            time.sleep(0.6)
            while self.busy:
                time.sleep(0.3)
            if self.verified:
                GLib.idle_add(lambda: (self.on_launch(None), False)[1])

        threading.Thread(target=drive, daemon=True).start()
        return False


class App(Gtk.Application):
    def __init__(self, autolaunch=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.autolaunch = autolaunch
        self.win = None

    def do_activate(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS)
        except TypeError:
            provider.load_from_data(CSS.decode(), -1)
        display = Gdk.Display.get_default()
        if display is not None:
            try:
                Gtk.StyleContext.add_provider_for_display(
                    display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            except Exception:
                pass

        cfg = Config()
        self.win = Window(self, cfg, autolaunch=self.autolaunch)
        self.win.connect("close-request", self.on_close)
        self.win.present()

    def on_close(self, _win):
        threading.Thread(target=lambda: self.win.session.stop_all("user"), daemon=True).start()
        time.sleep(0.2)
        return False


def main():
    autolaunch = "--launch" in sys.argv
    app = App(autolaunch=autolaunch)
    sys.exit(app.run([]))


if __name__ == "__main__":
    main()
