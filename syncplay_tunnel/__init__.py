"""Syncplay Tunnel — route Syncplay and mpv through a remote machine over SSH.

Split across modules by concern; see INDEX.md for what lives where. Everything
public is re-exported here, so `import syncplay_tunnel as st` gives one flat
namespace for tests and for anyone poking at it from a shell.
"""
from .constants import *  # noqa: F401,F403
from .constants import (APP_ID, APP_NAME, CACHE_FILE, CACHE_MAX_ENTRIES,
                        CACHE_TTL, CINEMETA, CONFIG_DIR, CONFIG_FILE, DATA_DIR,
                        DEFAULTS, FLATPAK_IDS, HISTORY_FILE, HISTORY_MAX,
                        IP_ECHOS, KEY_OPTIONS, KEY_TOKEN, LOG_FILE,
                        PLAYLIST_MAX_CHARACTERS, PLAYLIST_MAX_ITEMS, PROBE,
                        RD_API, RETRY_BACKOFF, SYNCPLAY_DEFAULT_PORT,
                        SYNCPLAY_PROTOCOL_VERSION, SYNCPLAY_SECTION,
                        TAILSCALE_NET, TORRENTIO, TRANSIENT_HTTP)
from .library import (Episode, Series, Source, cinemeta_episodes,
                      cinemeta_search, file_is_episode, guess_quality,
                      human_size, parse_source, pick_source, rd_auth_file,
                      rd_call, rd_fallback_sources, torrentio_sources,
                      torrentio_url, _cache_state)
from .playlist import SyncplayPush
from .proxy import HttpBridge, socks5_connect
from .runtimes import (Runtime, container_manager, current_container,
                       host_has, host_prefix, host_run, install_plan,
                       list_distroboxes, probe_container, probe_native,
                       scan_runtimes, start_container, stream_command)
from .session import Session
from .sshkeys import (ensure_ssh_key, find_ssh_key, key_line_is_open,
                      restrict_authorized_key, restrict_local_keys, ssh_copy_id)
from .store import Cache, Config, History, JsonStore
from .syncplay_ini import prepare_syncplay_ini, syncplay_ini_path
from .tailscale import Peer, tailscale_status
from .util import (all_ssh_clients, curl_final_url, curl_json, free_port,
                   in_container, is_tailscale_addr, notify, port_open,
                   redact, run, ssh_clients, stamp, tailscale_ssh_clients,
                   which, _scrubbed_env, _curl_json_once)

from .ui.app import App, main
from .ui.browse import BrowseWindow
from .ui.widgets import (CSS, Row, block_scroll_steal, check_row, checked_rows,
                         clear_list, list_row, scrolled_list, toggle_row)
from .ui.window import VIEWS, Window

__all__ = [n for n in dir() if not n.startswith("_")]
