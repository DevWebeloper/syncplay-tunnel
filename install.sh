#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# install.sh — put syncplay-tunnel on this machine.
#
# Works on: CachyOS/Arch, Fedora Silverblue (host), Ubuntu/Debian (container).
# Installs to ~/.local, so no root and nothing touches the immutable base image.
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$1"; }

# --- work out what we're on ------------------------------------------------
IN_CONTAINER=no
if [ -f /run/.containerenv ] || [ -f /.dockerenv ] || [ -n "${CONTAINER_ID:-}" ]; then
    IN_CONTAINER=yes
fi

if   command -v pacman  >/dev/null 2>&1; then FAMILY=arch
elif command -v apt-get >/dev/null 2>&1; then FAMILY=debian
elif command -v rpm-ostree >/dev/null 2>&1; then FAMILY=silverblue
elif command -v dnf     >/dev/null 2>&1; then FAMILY=fedora
else FAMILY=unknown
fi
say "Detected: $FAMILY (in container: $IN_CONTAINER)"

# --- dependency check ------------------------------------------------------
# Only what the app itself needs. Syncplay and mpv belong to whichever
# environment you end up watching in, and the app installs those for you from
# the "Where to play" section, where it knows which environment you picked.
check_deps() {
    missing=()
    python3 - <<'PY' 2>/dev/null || missing+=("python3-gobject + gtk4 + libadwaita")
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
PY
    for t in ssh curl; do command -v "$t" >/dev/null 2>&1 || missing+=("$t"); done
}

install_cmd() {
    case "$FAMILY" in
        arch)   echo "pacman -S --needed --noconfirm python-gobject gtk4 libadwaita openssh curl" ;;
        debian) echo "apt-get update && apt-get install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libgtk-4-1 openssh-client curl" ;;
        fedora) echo "dnf install -y python3-gobject gtk4 libadwaita openssh-clients curl" ;;
        *)      echo "" ;;
    esac
}

check_deps
if [ ${#missing[@]} -gt 0 ]; then
    warn "Missing: ${missing[*]}"
    cmd="$(install_cmd)"

    if [ "$FAMILY" = silverblue ]; then
        # Layering needs a reboot, so don't do it behind the user's back.
        echo "    Silverblue ships python3-gobject, gtk4 and libadwaita already. If this"
        echo "    check still failed, either run the app inside your container, or layer it:"
        echo "        sudo rpm-ostree install python3-gobject gtk4 libadwaita   # then reboot"
    elif [ -n "$cmd" ]; then
        echo "    sudo sh -c '$cmd'"
        read -rp "Install them now with sudo? [y/N] " ans
        if [[ "$ans" =~ ^[Yy]$ ]]; then
            if sudo sh -c "$cmd"; then
                check_deps
                if [ ${#missing[@]} -eq 0 ]; then
                    say "Dependencies are in place."
                else
                    warn "Still missing after the install: ${missing[*]}"
                fi
            else
                warn "The install command failed."
            fi
        fi
    else
        echo "    Install PyGObject with GTK4, plus ssh and curl."
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        read -rp "Continue installing anyway? [y/N] " ans
        [[ "$ans" =~ ^[Yy]$ ]] || exit 1
    fi
fi

# --- install ---------------------------------------------------------------
mkdir -p "$BIN_DIR" "$APP_DIR" "$ICON_DIR"

# The application is a package now, so it goes to its own directory under
# ~/.local/share and the launcher on PATH points at that rather than carrying
# the code itself.
PKG_DIR="$HOME/.local/share/syncplay-tunnel-app"
rm -rf "$PKG_DIR"
mkdir -p "$PKG_DIR"
cp -r "$SRC_DIR/syncplay_tunnel" "$PKG_DIR/"
find "$PKG_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
cat > "$BIN_DIR/syncplay-tunnel" <<LAUNCHER
#!/usr/bin/env python3
"""Launcher written by install.sh; the application lives in $PKG_DIR."""
import sys
sys.path.insert(0, "$PKG_DIR")
from syncplay_tunnel.ui.app import main
main()
LAUNCHER
chmod 755 "$BIN_DIR/syncplay-tunnel"
install -m 644 "$SRC_DIR/syncplay-tunnel.svg" "$ICON_DIR/syncplay-tunnel.svg"

sed "s|^Exec=syncplay-tunnel|Exec=$BIN_DIR/syncplay-tunnel|" \
    "$SRC_DIR/syncplay-tunnel.desktop" > "$APP_DIR/syncplay-tunnel.desktop"
chmod 644 "$APP_DIR/syncplay-tunnel.desktop"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APP_DIR" || true
command -v gtk-update-icon-cache  >/dev/null 2>&1 && \
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# Inside a container, also export the launcher to the host menu.
if [ "$IN_CONTAINER" = yes ] && command -v distrobox-export >/dev/null 2>&1; then
    say "Exporting the launcher to the host menu"
    distrobox-export --app syncplay-tunnel 2>/dev/null || \
        warn "distrobox-export failed — launch it from inside the container instead."
fi

case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not on your PATH. Add this to your shell rc:"
       echo '    export PATH="$HOME/.local/bin:$PATH"' ;;
esac

say "Installed. Launch it from your app menu, or run: syncplay-tunnel"
say "First run: fill in the host IP, press \"Check the route\", then \"Start watching\"."
