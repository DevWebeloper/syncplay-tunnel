#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-appimage.sh — bundle syncplay-tunnel into a single portable file.
#
# Run this INSIDE the sync-ubuntu container. Building on the oldest glibc you
# own is what makes the result run on CachyOS and Silverblue too; building on
# Arch produces an AppImage that Fedora will refuse to start.
#
#   distrobox enter sync-ubuntu -- bash build-appimage.sh
#
# Needs network access for the first run (it downloads linuxdeploy).
# ---------------------------------------------------------------------------
set -euo pipefail

APP=syncplay-tunnel
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$SRC_DIR/build"
APPDIR="$BUILD/AppDir"
TOOLS="$BUILD/tools"
ARCH="$(uname -m)"

say()  { printf '\033[1;36m==>\033[0m %s\n' "$1"; }
die()  { printf '\033[1;31mxx\033[0m %s\n' "$1" >&2; exit 1; }

command -v apt-get >/dev/null 2>&1 || \
    die "This is meant to run in the Debian/Ubuntu container. Use install.sh on Arch/Fedora."

# --- build dependencies ----------------------------------------------------
say "Installing build dependencies"
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends \
    python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 libgtk-4-1 \
    openssh-client curl wget file desktop-file-utils \
    librsvg2-common libcairo2 >/dev/null

rm -rf "$BUILD"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" \
         "$APPDIR/usr/share/icons/hicolor/scalable/apps" "$TOOLS"

# --- app payload -----------------------------------------------------------
say "Staging the application"
install -m 755 "$SRC_DIR/syncplay-tunnel.py" "$APPDIR/usr/bin/$APP"
install -m 644 "$SRC_DIR/syncplay-tunnel.svg" \
    "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP.svg"
install -m 644 "$SRC_DIR/syncplay-tunnel.desktop" \
    "$APPDIR/usr/share/applications/$APP.desktop"
cp "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP.svg" "$APPDIR/$APP.svg"
cp "$APPDIR/usr/share/applications/$APP.desktop" "$APPDIR/$APP.desktop"

# --- bundle the interpreter and PyGObject ---------------------------------
PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
say "Bundling Python $PYVER and PyGObject"
mkdir -p "$APPDIR/usr/lib/python$PYVER" "$APPDIR/usr/lib/$ARCH-linux-gnu"
cp -a "/usr/bin/python$PYVER" "$APPDIR/usr/bin/"
cp -a "/usr/lib/python$PYVER/." "$APPDIR/usr/lib/python$PYVER/"

# gi lives in dist-packages; carry it and its compiled extensions along
for site in $(python3 -c 'import site,sys; print("\n".join(site.getsitepackages()))'); do
    [ -d "$site/gi" ] && cp -a "$site/gi" "$APPDIR/usr/lib/python$PYVER/" && break
done
[ -d "$APPDIR/usr/lib/python$PYVER/gi" ] || die "Could not find the gi package to bundle."

# GObject introspection typelibs — GTK is useless without these
mkdir -p "$APPDIR/usr/lib/$ARCH-linux-gnu/girepository-1.0"
cp -a /usr/lib/"$ARCH"-linux-gnu/girepository-1.0/. \
      "$APPDIR/usr/lib/$ARCH-linux-gnu/girepository-1.0/" 2>/dev/null || true

# --- entry point -----------------------------------------------------------
cat > "$APPDIR/AppRun" << 'APPRUN'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
PYVER="$(ls "$HERE/usr/lib" | grep -m1 '^python3\.')"
export PATH="$HERE/usr/bin:$PATH"
export LD_LIBRARY_PATH="$HERE/usr/lib:$HERE/usr/lib/$(uname -m)-linux-gnu:${LD_LIBRARY_PATH:-}"
export GI_TYPELIB_PATH="$HERE/usr/lib/$(uname -m)-linux-gnu/girepository-1.0:${GI_TYPELIB_PATH:-}"
export PYTHONHOME="$HERE/usr"
export PYTHONPATH="$HERE/usr/lib/$PYVER:$HERE/usr/lib/$PYVER/lib-dynload:${PYTHONPATH:-}"
export XDG_DATA_DIRS="$HERE/usr/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
export GDK_PIXBUF_MODULE_FILE="$HERE/usr/lib/gdk-pixbuf-2.0/loaders.cache"
exec "$HERE/usr/bin/$PYVER" "$HERE/usr/bin/syncplay-tunnel" "$@"
APPRUN
chmod +x "$APPDIR/AppRun"

# --- linuxdeploy -----------------------------------------------------------
say "Fetching linuxdeploy"
cd "$TOOLS"
base="https://github.com/linuxdeploy/linuxdeploy/releases/download/continuous"
gtkp="https://raw.githubusercontent.com/linuxdeploy/linuxdeploy-plugin-gtk/master/linuxdeploy-plugin-gtk.sh"
[ -f linuxdeploy ] || wget -q -O linuxdeploy "$base/linuxdeploy-$ARCH.AppImage"
[ -f linuxdeploy-plugin-gtk.sh ] || wget -q -O linuxdeploy-plugin-gtk.sh "$gtkp"
chmod +x linuxdeploy linuxdeploy-plugin-gtk.sh

export DEPLOY_GTK_VERSION=4
export OUTPUT="$SRC_DIR/SyncplayTunnel-$ARCH.AppImage"
export PATH="$TOOLS:$PATH"

say "Building the AppImage (this takes a few minutes)"
cd "$BUILD"
./tools/linuxdeploy --appimage-extract-and-run \
    --appdir "$APPDIR" \
    --plugin gtk \
    --desktop-file "$APPDIR/usr/share/applications/$APP.desktop" \
    --icon-file "$APPDIR/usr/share/icons/hicolor/scalable/apps/$APP.svg" \
    --output appimage

say "Done: $OUTPUT"
say "Copy it to the other machine, chmod +x it, and run it. No install needed."
