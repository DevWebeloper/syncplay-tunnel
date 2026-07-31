"""Where Syncplay can be launched from: this system, or any distrobox.

Also the per-environment installer, since what is missing and how to install it
both depend on which environment was picked.
"""
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .constants import FLATPAK_IDS, PROBE
from .util import run, which, in_container

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


def wrap_in_runtime(rt, inner):
    """argv that runs a bash snippet inside the chosen environment."""
    if rt is None or rt.kind == "native":
        return host_prefix() + ["bash", "-lc", inner]
    if in_container() and os.environ.get("CONTAINER_ID") == rt.name:
        return ["bash", "-lc", inner]          # already inside the target
    return host_prefix() + ["distrobox", "enter", rt.name, "--", "bash", "-lc", inner]


def start_container(name, log=None):
    """Bring one container up. Returns True when it is running afterwards.

    `podman start` is used rather than `distrobox enter`, because it returns as
    soon as the container is up instead of holding a shell open, and it is a
    no-op when the container is already running.
    """
    if not name:
        return False
    mgr = container_manager()
    if mgr is None:
        if log:
            log("No podman or docker reachable, so %s cannot be started." % name)
        return False
    rc, _, err = host_run([mgr, "start", name], timeout=120)
    if rc != 0 and log:
        log("Could not start %s: %s" % (name, err or "exit %s" % rc))
    return rc == 0


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
