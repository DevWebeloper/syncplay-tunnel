"""Reading the Tailscale peer list."""
import json

from .util import run, which


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
