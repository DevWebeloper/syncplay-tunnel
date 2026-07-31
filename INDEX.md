# Index

What lives where, so a change can go straight to the right file instead of
reading the whole application. Line counts are a rough guide to size, not a
promise — check the file.

| File | Lines | What it holds |
|---|---|---|
| `syncplay_tunnel/constants.py` | 117 | Every fixed value in one place: paths, limits, endpoints and defaults. |
| `syncplay_tunnel/gtk_setup.py` | 22 | Pin the GTK and libadwaita versions before anything imports them. |
| `syncplay_tunnel/library.py` | 468 | Stremio's addons and the debrid account, spoken directly. |
| `syncplay_tunnel/playlist.py` | 173 | Setting the room's shared playlist over Syncplay's own protocol. |
| `syncplay_tunnel/proxy.py` | 203 | A local HTTP CONNECT proxy that forwards over the SSH SOCKS5 tunnel. |
| `syncplay_tunnel/runtimes.py` | 356 | Where Syncplay can be launched from: this system, or any distrobox. |
| `syncplay_tunnel/session.py` | 345 | The SSH tunnel, the bridge, the watchdog and the player. |
| `syncplay_tunnel/sshkeys.py` | 273 | Enrolling this machine's key on the host, and narrowing what it may do. |
| `syncplay_tunnel/store.py` | 155 | The three JSON files the app keeps: settings, cached lookups, watch history. |
| `syncplay_tunnel/syncplay_ini.py` | 103 | Editing Syncplay's own configuration file. |
| `syncplay_tunnel/tailscale.py` | 51 | Reading the Tailscale peer list. |
| `syncplay_tunnel/ui/app.py` | 53 | Application object and entry point. |
| `syncplay_tunnel/ui/browse.py` | 562 | The episode browser: search, episodes, the chosen sources, one source list. |
| `syncplay_tunnel/ui/widgets.py` | 189 | Shared widget helpers and the stylesheet. |
| `syncplay_tunnel/ui/window.py` | 1562 | The main window: a sidebar over Watch, Route, Where, Setup and Activity. |
| `syncplay_tunnel/util.py` | 239 | Small shared helpers: running commands, fetching over curl, ports. |

## Two things to know before editing

**Modules import names directly** (`from .util import run`), so rebinding
`syncplay_tunnel.run` does not reach them. Patch the module where the name is
bound — `syncplay_tunnel.util.run`, `syncplay_tunnel.runtimes.host_run`, and so
on. The test suites do exactly this.

**`gtk_setup` must be imported before `gi.repository`** in anything touching
GTK, because `gi.require_version` has to run first. Every UI module and
`session.py` already do.

## Symbols

### `syncplay_tunnel/constants.py`

**Constants:** `APP_ID`, `APP_NAME`, `CONFIG_DIR`, `CONFIG_FILE`, `DATA_DIR`, `LOG_FILE`, `CACHE_FILE`, `HISTORY_FILE`, `CACHE_TTL`, `CACHE_MAX_ENTRIES`, `HISTORY_MAX`, `TRANSIENT_HTTP`, `RETRY_BACKOFF`, `KEY_TOKEN`, `IP_ECHOS`, `CINEMETA`, `RD_API`, `TORRENTIO`, `PLAYLIST_MAX_ITEMS`, `PLAYLIST_MAX_CHARACTERS`, `DEFAULTS`, `TAILSCALE_NET`, `KEY_OPTIONS`, `SYNCPLAY_SECTION`, `SYNCPLAY_DEFAULT_PORT`, `SYNCPLAY_PROTOCOL_VERSION`, `FLATPAK_IDS`, `PROBE`

### `syncplay_tunnel/library.py`

**Constants:** `_SEEDERS_RE`, `_SIZE_RE`, `_PROVIDER_RE`, `_EPISODE_PATTERNS`

**Classes:** `Series`, `Episode`, `Source`

**Functions:** `parse_source()`, `rd_auth_file()`, `rd_call()`, `file_is_episode()`, `rd_fallback_sources()`, `guess_quality()`, `human_size()`, `cinemeta_search()`, `cinemeta_episodes()`, `torrentio_url()`, `torrentio_sources()`, `pick_source()`

### `syncplay_tunnel/playlist.py`

**Classes:** `SyncplayPush`

### `syncplay_tunnel/proxy.py`

**Classes:** `_BridgeHandler`, `_BridgeServer`, `HttpBridge`

**Functions:** `socks5_connect()`

### `syncplay_tunnel/runtimes.py`

**Constants:** `ANSI`, `PROBE`, `FLATPAK_IDS`

**Classes:** `Runtime`

**Functions:** `flatpak_apps()`, `probe_native()`, `host_prefix()`, `host_run()`, `host_has()`, `current_container()`, `container_manager()`, `list_distroboxes()`, `probe_container()`, `start_container()`, `scan_runtimes()`, `install_plan()`, `stream_command()`

### `syncplay_tunnel/session.py`

**Classes:** `Session`

### `syncplay_tunnel/sshkeys.py`

**Constants:** `KEY_CANDIDATES`, `KEY_OPTIONS`, `_RESTRICT_AWK`, `PROMPT_PW`, `PROMPT_YN`, `DENIED`

**Functions:** `find_ssh_key()`, `ensure_ssh_key()`, `restrict_authorized_key()`, `key_line_is_open()`, `restrict_local_keys()`, `ssh_copy_id()`

### `syncplay_tunnel/store.py`

**Classes:** `Config`, `JsonStore`, `Cache`, `History`

### `syncplay_tunnel/syncplay_ini.py`

**Constants:** `SYNCPLAY_SECTION`

**Functions:** `syncplay_ini_path()`, `prepare_syncplay_ini()`

### `syncplay_tunnel/tailscale.py`

**Classes:** `Peer`

**Functions:** `tailscale_status()`

### `syncplay_tunnel/ui/app.py`

**Classes:** `App`

**Functions:** `main()`

### `syncplay_tunnel/ui/browse.py`

**Classes:** `BrowseWindow`

### `syncplay_tunnel/ui/widgets.py`

**Constants:** `CSS`

**Classes:** `Row`

**Functions:** `block_scroll_steal()`, `clear_list()`, `list_row()`, `check_row()`, `toggle_row()`, `checked_rows()`, `scrolled_list()`

### `syncplay_tunnel/ui/window.py`

**Constants:** `VIEWS`

**Classes:** `Window`

### `syncplay_tunnel/util.py`

**Functions:** `which()`, `in_container()`, `stamp()`, `run()`, `redact()`, `curl_json()`, `curl_final_url()`, `notify()`, `port_open()`, `ssh_clients()`, `is_tailscale_addr()`, `free_port()`

