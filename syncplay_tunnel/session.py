"""The SSH tunnel, the bridge, the watchdog and the player.

Fail-closed: when the tunnel stops carrying traffic, playback is stopped rather
than left running from the wrong address.
"""
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import gtk_setup  # noqa: F401  (must precede gi.repository)
from gi.repository import GLib

from .constants import APP_NAME, IP_ECHOS
from .proxy import HttpBridge
from .runtimes import host_prefix, in_container
from .syncplay_ini import prepare_syncplay_ini
from .util import (_scrubbed_env, free_port, notify, port_open, run,
                    which)


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
            rc, out, err = run(cmd + [url], timeout=15, env=_scrubbed_env())
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
