"""Every fixed value in one place: paths, limits, endpoints and defaults.

Nothing here imports from the rest of the package, so anything may import it.
"""
import getpass
import ipaddress
import os
from pathlib import Path

APP_ID = "io.github.DevWebeloper.SyncplayTunnel"
APP_NAME = "Syncplay Tunnel"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "syncplay-tunnel"
CONFIG_FILE = CONFIG_DIR / "config.json"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "syncplay-tunnel"
LOG_FILE = DATA_DIR / "session.log"
CACHE_FILE = DATA_DIR / "cache.json"
HISTORY_FILE = DATA_DIR / "history.json"

# How long a cached answer stays good. Sources are shortest because what the
# debrid service already holds changes, and new releases appear.
CACHE_TTL = {
    "search": 7 * 86400,
    "episodes": 24 * 3600,
    "sources": 6 * 3600,
}
CACHE_MAX_ENTRIES = 400
HISTORY_MAX = 30

# Statuses worth trying again. The source addon sits behind Cloudflare, which
# answers 52x when it cannot reach the addon itself — seen in the wild as a run
# of 522s while the service was overloaded. None of those mean "no such thing".
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
RETRY_BACKOFF = (2, 5, 10)

# Stand-in for the debrid key inside anything written to disk. Torrentio puts
# the key in its URL path, so a cached source list would otherwise leak it into
# a file that exists purely for speed.
KEY_TOKEN = "{KEY}"

# Public-IP echo services, tried in order. Plain HTTP variants are kept as a
# fallback because some SOCKS paths choke on TLS through odd MTUs.
IP_ECHOS = [
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
]

# Stremio's own addons, spoken directly. Cinemeta is the metadata catalogue the
# Stremio app itself uses and needs no key; Torrentio is the source finder, and
# it takes the debrid key inside its URL path — which is why nothing from these
# URLs is ever logged raw.
CINEMETA = "https://v3-cinemeta.strem.io"
RD_API = "https://api.real-debrid.com/rest/1.0"
TORRENTIO = "https://torrentio.strem.fun"

# syncplay/utils.py playlistIsValid() rejects anything past these, silently, on
# the server. Better to say so here than to have the push vanish.
PLAYLIST_MAX_ITEMS = 250
PLAYLIST_MAX_CHARACTERS = 10000

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
    # The last URL actually launched. Syncplay forces its setup dialog whenever
    # it is given no file at all, so this is what a blank URL falls back to.
    "last_play_url": "",
    "skip_syncplay_dialog": True,
    "trust_play_domain": True,
    "mpv_extra": "",
    "require_verified": True,
    "stop_container_on_drop": True,
    # Library browser. The key is the same one already handed to Torrentio by
    # the Stremio addon, and it lands in config.json, which is written 0600.
    "rd_api_key": "",
    "torrentio_opts": "sort=qualitysize",
    # Start the container you last launched from when the app opens, so it is
    # ready instead of showing up stopped.
    "autostart_container": True,
    "preferred_quality": "1080p",
    # Where the last session got to, so reopening the browser lands on the next
    # episode instead of the search box. Set programmatically, no widget.
    "library_series_id": "",
    "library_series_name": "",
    "library_season": 0,
    "library_episode": 0,
}

TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")

# Options written onto an authorised key: no shell, forwarding only.
KEY_OPTIONS = "restrict,port-forwarding"

SYNCPLAY_SECTION = "client_settings"
SYNCPLAY_DEFAULT_PORT = 8999
SYNCPLAY_PROTOCOL_VERSION = "1.7.5"

FLATPAK_IDS = {"syncplay": "pl.syncplay.Syncplay", "mpv": "io.mpv.Mpv"}

PROBE = "command -v syncplay >/dev/null 2>&1 && echo HAVE_SP; " \
        "command -v mpv >/dev/null 2>&1 && echo HAVE_MPV"
