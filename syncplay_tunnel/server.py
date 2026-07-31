"""Running the Syncplay server on whichever machine was chosen to host it.

Both people need a server to meet on. The public one at syncplay.pl serves a
certificate whose issuer is not in either machine's trust store, which is what
started this, so the pair run their own on one of the two laptops. That machine
is reachable on the tailnet, and nothing needs forwarding.

The server is bound to the Tailscale address only, deliberately: on 0.0.0.0 it
would also be listening on whatever cafe wifi the laptop is on.
"""
import os
import re
import subprocess

from .runtimes import wrap_in_runtime
from .util import port_open, run


def parse_server(value, default_port=8999):
    """Split "host:port" into its parts, tolerating a bare host."""
    host, _, port = str(value or "").strip().rpartition(":")
    if not host:
        host, port = port, ""
    return host, (int(port) if port.isdigit() else default_port)


def make_salt():
    """A stable random salt, so room passwords survive a restart."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(alphabet[b % len(alphabet)] for b in os.urandom(10))


def server_running(host, port, timeout=1.5):
    """True when something is already listening where the server should be."""
    if not host:
        return False
    return port_open(int(port), host=host, timeout=timeout)


def server_command(host, port, salt):
    """The command line, as a bash snippet.

    --interface-ipv4 is only honoured alongside --ipv4-only; without it the
    server also opens an IPv6 socket and ends up listening everywhere, which
    defeats the point of naming an interface at all.
    """
    return ("exec syncplay-server --ipv4-only --interface-ipv4 %s --port %d --salt %s"
            % (host, int(port), salt))


def stop_server(rt, log=None):
    """Stop a server this app started. Safe when none is running."""
    # -x matches the command name exactly. A distrobox shares the host PID
    # namespace, so a loose -f pattern here can reach out and kill things on the
    # host that merely mention the name.
    rc, _, _ = run(wrap_in_runtime(rt, "pkill -x syncplay-server"), timeout=15)
    if log and rc == 0:
        log("Stopped the Syncplay server.")
    return rc == 0


def ensure_server(cfg, rt, self_ip="", log=None):
    """Start the Syncplay server if this machine is the one hosting it.

    Returns (running, message). Doing nothing and reporting the server already
    up is a success -- this runs on every app start.
    """
    def say(msg):
        if log:
            log(msg)

    if not cfg["run_syncplay_server"]:
        return False, "This machine is not the Syncplay server."

    host, port = parse_server(cfg["syncplay_server"])
    # Prefer the address the tailnet knows us by; a stale host from the config
    # would bind nothing.
    if self_ip:
        host = self_ip
    if not host:
        return False, ("No address to bind the Syncplay server to — Tailscale has "
                       "not reported one yet.")

    if server_running(host, port):
        return True, "Syncplay server already listening on %s:%d." % (host, port)

    salt = str(cfg["syncplay_server_salt"] or "").strip()
    if not salt:
        salt = make_salt()
        cfg["syncplay_server_salt"] = salt

    say("Starting the Syncplay server on %s:%d…" % (host, port))
    try:
        subprocess.Popen(wrap_in_runtime(rt, server_command(host, port, salt)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as exc:
        return False, "Could not start the Syncplay server: %s" % exc

    # It binds quickly, but not instantly.
    for _ in range(20):
        if server_running(host, port, timeout=0.5):
            return True, "Syncplay server listening on %s:%d." % (host, port)
    return False, ("The Syncplay server did not come up on %s:%d. Is syncplay-server "
                   "installed in the environment under Where to play?" % (host, port))
