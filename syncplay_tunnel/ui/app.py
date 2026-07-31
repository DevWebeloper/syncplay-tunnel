"""Application object and entry point."""
import sys
import threading
import time

from .. import gtk_setup  # noqa: F401  (must precede gi.repository)
from gi.repository import Adw, Gdk, Gio, Gtk

from ..constants import APP_ID
from ..store import Config
from .widgets import CSS
from .window import Window

class App(Adw.Application):
    def __init__(self, autolaunch=False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.autolaunch = autolaunch
        self.win = None

    def do_activate(self):
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(CSS)
        except TypeError:
            provider.load_from_data(CSS.decode(), -1)
        display = Gdk.Display.get_default()
        if display is not None:
            try:
                Gtk.StyleContext.add_provider_for_display(
                    display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
            except Exception:
                pass

        cfg = Config()
        self.win = Window(self, cfg, autolaunch=self.autolaunch)
        self.win.connect("close-request", self.on_close)
        self.win.present()

    def on_close(self, _win):
        threading.Thread(target=lambda: self.win.session.stop_all("user"), daemon=True).start()
        time.sleep(0.2)
        return False


def main():
    autolaunch = "--launch" in sys.argv
    app = App(autolaunch=autolaunch)
    sys.exit(app.run([]))


if __name__ == "__main__":
    main()
