"""Small shared helpers: running commands, fetching over curl, ports.

Every network call in the app goes through curl, which keeps the package free of
pip dependencies and makes the route an explicit argument rather than something
picked up from the environment.
"""
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .constants import RETRY_BACKOFF, TAILSCALE_NET, TRANSIENT_HTTP


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


def redact(text, *secrets):
    """Blank out anything that must never reach the log or the UI.

    The debrid key travels inside the Torrentio URL path, so any message that
    might carry one of those URLs goes through here first.
    """
    out = str(text)
    for secret in secrets:
        secret = (secret or "").strip()
        # A short or empty value would match everywhere and redact the message
        # itself, which hides real errors.
        if len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


def _scrubbed_env():
    """os.environ without proxy variables, so a route is never picked up by
    accident. Ambient proxy settings would silently change which IP a request
    leaves from, which is the one thing this app exists to control."""
    env = dict(os.environ)
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    return env


def _curl_json_once(url, socks_port, timeout):
    """One attempt. Returns (data, error, worth_retrying)."""
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout),
           "-H", "Accept: application/json",
           # The status is needed to tell "the service is broken" apart from
           # "the service answered something we cannot read".
           "-w", "\n%{http_code}"]
    if socks_port:
        cmd += ["--socks5-hostname", "127.0.0.1:%d" % socks_port]
    else:
        cmd += ["--noproxy", "*"]
    rc, out, err = run(cmd + [url], timeout=timeout + 5, env=_scrubbed_env())
    if rc != 0:
        # 28 is curl's own timeout; everything at this level is worth another go.
        return None, (err or "curl exited %s" % rc), True

    body, _, code = out.rpartition("\n")
    code = code.strip()
    if code.isdigit() and not code.startswith("2"):
        return None, "HTTP %s" % code, int(code) in TRANSIENT_HTTP
    if not body.strip():
        return None, "empty reply", True
    try:
        return json.loads(body), "", False
    except ValueError:
        # A gateway that is failing hands back an HTML error page, so say what
        # actually came back rather than blaming the JSON.
        head = body.strip()[:60].replace("\n", " ")
        return None, "HTTP %s but the reply was not JSON: %s…" % (code or "?", head), True


def curl_json(url, socks_port=None, timeout=20, attempts=3):
    """Fetch JSON with curl. Returns (data, error) — data is None on failure.

    Every other fetch in this app shells out to curl, which keeps the zero-pip
    promise and, more usefully, makes the route an explicit argument: pass a
    port to go through the tunnel, pass nothing to go direct.

    Retried, because the source addon sits behind a gateway that intermittently
    cannot reach it and answers 522 or an HTML error page. One of those should
    not end a whole queue.
    """
    err = "no attempt made"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
        data, err, retry = _curl_json_once(url, socks_port, timeout)
        if data is not None:
            return data, ""
        if not retry:
            break
    return None, err


def curl_final_url(url, socks_port=None, timeout=180, attempts=3):
    """Follow redirects and report where they land. Returns (url, error).

    Asks for one byte rather than issuing a HEAD: some CDNs answer HEAD with
    405, and every one of them handles a range request because seeking needs
    it. Without the range this would download the whole episode.

    The timeout is deliberately generous. Torrentio's resolver has to reach the
    debrid service and have it hand back a link, and measured against real
    sources that takes anywhere from a couple of seconds to about ninety, even
    for a torrent the service already has cached.
    """
    cmd = ["curl", "-s", "-L", "-o", "/dev/null", "-r", "0-0",
           "--max-time", str(timeout), "-w", "%{url_effective}\t%{http_code}"]
    if socks_port:
        cmd += ["--socks5-hostname", "127.0.0.1:%d" % socks_port]
    else:
        cmd += ["--noproxy", "*"]

    err = "no attempt made"
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(RETRY_BACKOFF[min(attempt - 1, len(RETRY_BACKOFF) - 1)])
        rc, out, cerr = run(cmd + [url], timeout=timeout + 5, env=_scrubbed_env())
        if rc != 0:
            err = cerr or "curl exited %s" % rc
            continue
        final, _, code = out.strip().partition("\t")
        code = code.strip()
        if code.startswith("2"):
            return (final, "") if final else (None, "no redirect target")
        err = "HTTP %s" % (code or "?")
        if code.isdigit() and int(code) not in TRANSIENT_HTTP:
            break
    return None, err


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
