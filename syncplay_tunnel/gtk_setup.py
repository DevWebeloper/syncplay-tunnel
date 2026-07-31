"""Pin the GTK and libadwaita versions before anything imports them.

gi.require_version has to run before the first `from gi.repository import`, so
every module that touches GTK imports this one first. It also turns a missing
libadwaita into a readable line rather than a traceback.
"""
import gi

gi.require_version("Gtk", "4.0")
try:
    gi.require_version("Adw", "1")
    from gi.repository import Adw  # noqa: F401
except (ValueError, ImportError) as exc:  # pragma: no cover - depends on host
    import sys

    sys.stderr.write(
        "Syncplay Tunnel needs libadwaita (%s).\n"
        "  Arch:   sudo pacman -S --needed libadwaita\n"
        "  Debian: sudo apt install -y gir1.2-adw-1\n"
        "  Fedora: sudo dnf install -y libadwaita\n" % exc
    )
    raise SystemExit(1)
