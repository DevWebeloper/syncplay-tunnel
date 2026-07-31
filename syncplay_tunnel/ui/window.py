"""The main window: a sidebar over Watch, Route, Where, Setup and Activity."""
import getpass
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from .. import gtk_setup  # noqa: F401  (must precede gi.repository)
from gi.repository import Adw, GLib, Gtk

from ..constants import (APP_NAME, CONFIG_FILE, DATA_DIR, DEFAULTS, KEY_OPTIONS,
                         LOG_FILE)
from ..library import Series
from ..playlist import SyncplayPush
from ..runtimes import (current_container, install_plan, scan_runtimes,
                        start_container, stream_command)
from ..session import Session
from ..sshkeys import (ensure_ssh_key, find_ssh_key, restrict_authorized_key,
                       restrict_local_keys, ssh_copy_id)
from ..store import Cache, History
from ..syncplay_ini import prepare_syncplay_ini
from ..tailscale import tailscale_status
from ..util import (in_container, is_tailscale_addr, notify, port_open, run,
                    ssh_clients, stamp, which)
from .browse import BrowseWindow
from .widgets import (Row, block_scroll_steal, clear_list, list_row,
                      scrolled_list)

VIEWS = [
    ("watch", "Watch", "media-playback-start-symbolic"),
    ("route", "Route", "network-transmit-receive-symbolic"),
    ("where", "Where", "computer-symbolic"),
    ("setup", "Setup", "preferences-system-symbolic"),
    ("activity", "Activity", "text-x-generic-symbolic"),
]


class Window(Adw.ApplicationWindow):
    def __init__(self, app, cfg, autolaunch=False):
        super().__init__(application=app, title=APP_NAME)
        self.cfg = cfg
        self.set_default_size(940, 720)
        self.session = Session(cfg, self.log, self.on_state)
        self.verified = False
        self.busy = False

        DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.peers = []
        self.tailscale_self = None
        self.host_count_timer = None
        # Episodes queued by the browser. The first one launches with Syncplay
        # as its positional file; the rest are pushed to the room afterwards.
        self.queue = []
        self.cache = Cache()
        self.history = History()
        self._seed_history()
        self._save_timer = None

        self.toaster = Adw.ToastOverlay()
        self.set_content(self.toaster)

        self.views = Adw.ViewStack()
        self.views.set_vexpand(True)

        split = Adw.NavigationSplitView()
        split.set_min_sidebar_width(180)
        split.set_max_sidebar_width(240)
        # Content first: the sidebar selects its first row as it is built, which
        # fires on_nav_selected, which needs the views and the page to exist.
        content = self._build_content()
        split.set_sidebar(self._build_sidebar())
        split.set_content(content)
        self.toaster.set_child(split)

        self.on_role_changed()
        self._env_banner()
        threading.Thread(target=self._scan_worker, args=(True,), daemon=True).start()
        self.refresh_peers()
        self.refresh_history()
        if autolaunch:
            GLib.timeout_add(600, self._auto)

    # -- shell ------------------------------------------------------------ #

    def _build_sidebar(self):
        self.nav = Gtk.ListBox()
        self.nav.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.nav.add_css_class("navigation-sidebar")
        for name, title, icon in VIEWS:
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            box.set_margin_top(10)
            box.set_margin_bottom(10)
            box.set_margin_start(6)
            box.set_margin_end(6)
            box.append(Gtk.Image.new_from_icon_name(icon))
            box.append(Gtk.Label(label=title, xalign=0))
            row.set_child(box)
            row.view_name = name
            self.nav.append(row)
        self.nav.connect("row-selected", self.on_nav_selected)
        self.nav.select_row(self.nav.get_row_at_index(0))

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.nav)

        bar = Adw.ToolbarView()
        bar.add_top_bar(Adw.HeaderBar())
        bar.set_content(scroller)
        return Adw.NavigationPage(child=bar, title=APP_NAME)

    def _build_content(self):
        self.views.add_titled(self._build_watch(), "watch", "Watch")
        self.views.add_titled(self._build_route(), "route", "Route")
        self.views.add_titled(self._build_where(), "where", "Where")
        self.views.add_titled(self._build_setup(), "setup", "Setup")
        self.views.add_titled(self._build_log(), "activity", "Activity")

        header = Adw.HeaderBar()
        self.pill = Gtk.Label(label="Not connected")
        self.pill.add_css_class("pill")
        self.pill.add_css_class("pill-idle")
        header.pack_end(self.pill)

        bar = Adw.ToolbarView()
        bar.add_top_bar(header)
        bar.set_content(self.views)
        # Start and Stop belong to no single view -- they are the point of the
        # whole window, so they sit under all of them.
        bar.add_bottom_bar(self._build_actions())
        self.content_page = Adw.NavigationPage(child=bar, title="Watch")
        return self.content_page

    def on_nav_selected(self, _list, row):
        name = getattr(row, "view_name", None) if row is not None else None
        if not name:
            return
        self.views.set_visible_child_name(name)
        for key, title, _icon in VIEWS:
            if key == name:
                self.content_page.set_title(title)
                break

    def show_view(self, name):
        for i, (key, _t, _i) in enumerate(VIEWS):
            if key == name:
                self.nav.select_row(self.nav.get_row_at_index(i))
                return

    def toast(self, text):
        self.toaster.add_toast(Adw.Toast(title=text, timeout=3))

    # -- setting rows ----------------------------------------------------- #
    #
    # collect() finds every bound widget by the e_/s_/w_ prefix on this object,
    # so these keep storing under exactly the names it expects. AdwEntryRow
    # implements GtkEditable and AdwSpinRow/AdwSwitchRow carry value/active, so
    # the reading side needed no changes at all.

    def _entry(self, group, label, key, hint="", password=False):
        row = Adw.PasswordEntryRow(title=label) if password else Adw.EntryRow(title=label)
        row.set_text(str(self.cfg[key]))
        if hint:
            # AdwEntryRow has no placeholder and no subtitle, and the hints are
            # worth keeping, so they live in the tooltip and in the group
            # description above.
            row.set_tooltip_text(hint)
        row.connect("changed", self._on_setting_changed)
        group.add(row)
        setattr(self, "e_" + key, row)
        return row

    def _spin(self, group, label, key, lo, hi, subtitle=""):
        row = Adw.SpinRow.new_with_range(lo, hi, 1)
        row.set_title(label)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_value(int(self.cfg[key]))
        block_scroll_steal(row)
        row.connect("notify::value", self._on_setting_changed)
        group.add(row)
        setattr(self, "s_" + key, row)
        return row

    def _switch(self, group, label, key, subtitle=""):
        row = Adw.SwitchRow(title=label)
        if subtitle:
            row.set_subtitle(subtitle)
        row.set_active(bool(self.cfg[key]))
        row.connect("notify::active", self._on_setting_changed)
        group.add(row)
        setattr(self, "w_" + key, row)
        return row

    def _on_setting_changed(self, *_a):
        """Save shortly after the last edit, so nothing is lost to a stray close."""
        if self._save_timer is not None:
            GLib.source_remove(self._save_timer)
        self._save_timer = GLib.timeout_add(900, self._save_now)

    def _save_now(self):
        self._save_timer = None
        self.collect()
        self.cfg.save()
        return False

    def _view(self, spacing=18):
        """The shape every view shares: a clamped, scrolling column of groups."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(18)
        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(box)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(clamp)
        return scroller, box

    @staticmethod
    def _note(text):
        label = Gtk.Label(label=text, xalign=0)
        label.set_wrap(True)
        label.set_hexpand(True)
        label.add_css_class("dim")
        return label

    def _build_route(self):
        """How traffic leaves, and proof that it does."""
        view, box = self._view()

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.add_titled(self._build_client_page(), "client", "Client")
        self.stack.add_titled(self._build_host_page(), "host", "Host")
        self.stack.set_visible_child_name(
            "host" if self.cfg["role"] == "host" else "client")
        self.stack.connect("notify::visible-child-name", self.on_role_changed)

        switcher = Gtk.StackSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_halign(Gtk.Align.CENTER)
        box.append(switcher)
        box.append(self.stack)
        box.append(self._build_verify())
        return view

    # -- client side ----------------------------------------------------- #

    def _build_client_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        group = Adw.PreferencesGroup(
            title="Pick a host to route through",
            description="Everything Syncplay and mpv fetch will leave from the host you "
                        "pick here. The tunnel dials outward, so nothing needs "
                        "forwarding at either end.")

        self.peer_list = Gtk.ListBox()
        self.peer_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.peer_list.add_css_class("boxed-list")
        self.peer_list.set_hexpand(True)
        self.peer_list.connect("row-selected", self.on_peer_selected)

        holder = Gtk.ScrolledWindow()
        holder.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        holder.set_min_content_height(120)
        holder.set_max_content_height(220)
        holder.set_hexpand(True)
        holder.set_child(self.peer_list)
        group.add(holder)
        page.append(group)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        refresh = Gtk.Button(label="Refresh list")
        refresh.connect("clicked", lambda _b: self.refresh_peers())
        buttons.append(refresh)
        keybtn = Gtk.Button(label="Set up SSH key…")
        keybtn.connect("clicked", lambda _b: self.open_key_dialog())
        buttons.append(keybtn)
        page.append(buttons)

        fields = Adw.PreferencesGroup()
        self._entry(fields, "Host address", "host_ip", "100.x.x.x, or a hostname")
        self._entry(fields, "SSH user on the host", "host_user",
                    "account name on the exit machine")
        page.append(fields)
        return page

    def refresh_peers(self):
        threading.Thread(target=self._peers_worker, daemon=True).start()

    def _peers_worker(self):
        me, peers = tailscale_status()

        def apply():
            self.tailscale_self = me
            self.peers = peers
            child = self.peer_list.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                self.peer_list.remove(child)
                child = nxt

            if not peers:
                row = Gtk.ListBoxRow()
                lbl = Gtk.Label(
                    label="No Tailscale peers found. Type the address below instead.",
                    xalign=0)
                lbl.set_margin_top(8); lbl.set_margin_bottom(8)
                lbl.set_margin_start(10); lbl.set_margin_end(10)
                lbl.add_css_class("dim")
                row.set_child(lbl)
                row.set_selectable(False)
                self.peer_list.append(row)
            else:
                current = self.e_host_ip.get_text().strip()
                for p in peers:
                    row = Gtk.ListBoxRow()
                    lbl = Gtk.Label(label=p.label(), xalign=0)
                    lbl.set_margin_top(8); lbl.set_margin_bottom(8)
                    lbl.set_margin_start(10); lbl.set_margin_end(10)
                    if not p.online:
                        lbl.add_css_class("dim")
                    row.set_child(lbl)
                    row.peer = p
                    self.peer_list.append(row)
                    if p.ip == current:
                        self.peer_list.select_row(row)

            if me:
                self.host_self_label.set_text("%s  ·  %s" % (me.name, me.ip))
                self.cfg["client_ip"] = me.ip
            self.refresh_host_page()
            return False

        GLib.idle_add(apply)

    def on_peer_selected(self, _list, row):
        peer = getattr(row, "peer", None) if row is not None else None
        if peer is None:
            return
        self.e_host_ip.set_text(peer.ip)
        if not peer.online:
            self.log("%s is offline right now — the route check will fail until it wakes." % peer.name)

    # -- host side ------------------------------------------------------- #

    def _build_host_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        group = Adw.PreferencesGroup(
            title="This machine is the exit point",
            description="Traffic already leaves from here, so no tunnel is opened in "
                        "this mode. Start watching launches Syncplay directly.")

        # These stay GtkLabels in a suffix rather than becoming row subtitles,
        # because the workers that fill them call set_text() on each by name.
        def status_row(title, wrap=False):
            label = Gtk.Label(label="checking…", xalign=1)
            label.add_css_class("dim")
            if wrap:
                label.set_wrap(True)
                label.set_max_width_chars(40)
            row = Adw.ActionRow(title=title)
            row.add_suffix(label)
            group.add(row)
            return label

        self.host_self_label = status_row("Tailscale name")
        self.host_sshd_label = status_row("SSH server")
        self.host_keys_label = status_row("Keys installed")
        self.host_conn_label = status_row("Connected now", wrap=True)

        share_row = Adw.ActionRow(title="Give this to the client")
        self.host_share = Gtk.Entry()
        self.host_share.set_editable(False)
        self.host_share.set_hexpand(True)
        self.host_share.set_valign(Gtk.Align.CENTER)
        share_row.add_suffix(self.host_share)
        copy = Gtk.Button(icon_name="edit-copy-symbolic")
        copy.set_tooltip_text("Copy")
        copy.set_valign(Gtk.Align.CENTER)
        copy.connect("clicked", self.on_copy_share)
        share_row.add_suffix(copy)
        group.add(share_row)
        page.append(group)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        recheck = Gtk.Button(label="Re-check")
        recheck.connect("clicked", lambda _b: self.refresh_peers())
        buttons.append(recheck)
        self.btn_restrict = Gtk.Button(label="Restrict existing keys")
        self.btn_restrict.add_css_class("destructive-action")
        self.btn_restrict.connect("clicked", self.on_restrict_keys)
        self.btn_restrict.set_visible(False)
        buttons.append(self.btn_restrict)
        page.append(buttons)
        return page

    def _count_worker(self):
        """Who is on our sshd right now. Runs off the main loop: ss is a fork."""
        port = int(self.cfg["host_ssh_port"] or 22)
        addrs = ssh_clients(port)
        names = {p.ip: p.name for p in self.peers}

        def apply():
            if not addrs:
                self.host_conn_label.set_text("nobody connected yet")
                if self.cfg["role"] == "host":
                    self.set_pill("Host — 0 connected", "ok")
                return False
            shown = []
            for a in addrs:
                name = names.get(a)
                tail = "" if is_tailscale_addr(a) else " (not Tailscale)"
                shown.append(("%s (%s)%s" % (name, a, tail)) if name else (a + tail))
            self.host_conn_label.set_text(
                "%d connected — %s" % (len(addrs), ", ".join(shown)))
            if self.cfg["role"] == "host":
                self.set_pill("Host — %d connected" % len(addrs), "ok")
            return False

        GLib.idle_add(apply)

    def _count_tick(self):
        """Keep polling only while the host page is the one on screen."""
        if self.cfg["role"] != "host":
            self.host_count_timer = None
            return False
        threading.Thread(target=self._count_worker, daemon=True).start()
        return True

    def refresh_host_page(self):
        user = getpass.getuser()
        me = getattr(self, "tailscale_self", None)
        addr = me.ip if me else socket.gethostname()

        port = int(self.cfg["host_ssh_port"] or 22)
        listening = port_open(port, timeout=1.5)
        self.host_sshd_label.set_text(
            "listening on port %d" % port if listening
            else "nothing on port %d — start sshd to accept clients" % port)

        keys = Path.home() / ".ssh/authorized_keys"
        try:
            lines = [l for l in keys.read_text().splitlines()
                     if l.strip() and not l.startswith("#")]
            count = len(lines)
            # No options field means that key is a full shell login here.
            loose = len([l for l in lines
                         if l.split()[0].startswith(("ssh-", "ecdsa-", "sk-"))])
            text = "%d client key%s authorised" % (count, "" if count == 1 else "s")
            if loose:
                text += " — %d unrestricted (full shell)" % loose
            self.host_keys_label.set_text(text)
            self.btn_restrict.set_visible(bool(loose))
        except OSError:
            self.host_keys_label.set_text("no authorized_keys yet — no client can connect")
            self.btn_restrict.set_visible(False)

        self.host_share.set_text("%s@%s" % (user, addr))
        if me is None:
            self.host_self_label.set_text("Tailscale not reporting — %s" % socket.gethostname())

    def on_restrict_keys(self, _btn=None):
        """Retrofit keys enrolled before options were being set.

        Confirmed first: this changes what other people's machines are allowed
        to do on this one, and getting it wrong locks them out.
        """
        dlg = Gtk.Window(title="Restrict authorised keys", transient_for=self, modal=True)
        dlg.set_default_size(460, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(16)
        dlg.set_child(box)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>Limit every authorised key to port forwarding</b>")
        box.append(head)

        why = Gtk.Label(
            label="Each key in ~/.ssh/authorized_keys with no options set is a full "
                  "shell login on this machine for whoever holds the matching private "
                  "key.\n\nThis prefixes those lines with "
                  "'" + KEY_OPTIONS + "': no pty, no agent or X11 forwarding, port "
                  "forwarding still allowed, so the tunnel and the route check keep "
                  "working. Nothing is removed and the old file is kept as "
                  "authorized_keys.bak.",
            xalign=0)
        why.set_wrap(True)
        why.set_max_width_chars(54)
        box.append(why)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        confirm = Gtk.Button(label="Restrict them")
        confirm.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        cancel.connect("clicked", lambda *_a: dlg.close())

        def go(*_a):
            dlg.close()
            changed, total = restrict_local_keys(log=self.log)
            if changed:
                notify(APP_NAME, "%d of %d authorised keys restricted." % (changed, total))
            self.refresh_host_page()

        confirm.connect("clicked", go)
        dlg.present()

    def on_copy_share(self, btn):
        text = self.host_share.get_text()
        try:
            btn.get_clipboard().set(text)
        except Exception:
            try:
                from gi.repository import Gdk as _Gdk
                _Gdk.Display.get_default().get_clipboard().set(text)
            except Exception:
                self.log("Could not reach the clipboard. The line is: %s" % text)
                return
        self.log("Copied: %s" % text)

    def on_role_changed(self, *_a):
        role = self.stack.get_visible_child_name() or "client"
        self.cfg["role"] = role
        # No widget carries the role, so collect() never sees it and nothing
        # else would schedule the save that makes it survive a restart.
        self._on_setting_changed()
        host_mode = role == "host"
        self.frame_verify.set_visible(not host_mode)
        self.btn_launch.set_label("Start watching")
        if host_mode:
            self.set_pill("Host — no tunnel", "ok")
            self.log("Host mode: traffic already exits here, so no tunnel will be opened.")
            self.refresh_host_page()
            self._count_tick()
            if getattr(self, "host_count_timer", None) is None:
                self.host_count_timer = GLib.timeout_add_seconds(5, self._count_tick)
        else:
            self.set_pill("Not connected", "idle")
        return False

    def _build_where(self):
        view, box = self._view()

        self.runtimes = []

        group = Adw.PreferencesGroup(title="Where to play")

        # Same widget as the host picker: a plain ListBox of rows inside a
        # scroller, each row carrying its runtime. Nothing here depends on a
        # list factory or an expression, so it draws the same on every build.
        self.env_list = Gtk.ListBox()
        self.env_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.env_list.add_css_class("boxed-list")
        self.env_list.set_hexpand(True)
        self.env_list.connect("row-selected", self.on_env_selected)

        env_holder = Gtk.ScrolledWindow()
        env_holder.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        env_holder.set_min_content_height(140)
        env_holder.set_max_content_height(280)
        env_holder.set_hexpand(True)
        env_holder.set_child(self.env_list)
        group.add(env_holder)
        box.append(group)
        self._set_env_rows([])

        self.env_detail = Gtk.Label(label="", xalign=0)
        self.env_detail.set_wrap(True)
        self.env_detail.set_hexpand(True)
        box.append(self.env_detail)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        rescan = Gtk.Button(label="Rescan")
        rescan.connect("clicked", self.on_rescan)
        buttons.append(rescan)
        self.btn_install = Gtk.Button(label="Install missing")
        self.btn_install.add_css_class("suggested-action")
        self.btn_install.connect("clicked", self.on_install_missing)
        self.btn_install.set_visible(False)
        buttons.append(self.btn_install)
        box.append(buttons)

        options = Adw.PreferencesGroup()
        self._switch(options, "Start the environment I used last", "autostart_container",
                     "Brings that container up when the app opens, so it is ready "
                     "instead of showing up stopped.")
        self._switch(options, "Start stopped containers while scanning", "scan_stopped",
                     "Applies to every other container too, which means starting all "
                     "of them just to look inside.")
        box.append(options)

        self.where_note = self._note(
            "Looking for Syncplay and mpv on this system and in every distrobox…")
        box.append(self.where_note)
        return view

    def _set_env_rows(self, found, placeholder="Scanning…", select=None):
        """Refill the environment list. `select` is an index into `found`."""
        child = self.env_list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.env_list.remove(child)
            child = nxt

        if not found:
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=placeholder, xalign=0)
            lbl.set_margin_top(8); lbl.set_margin_bottom(8)
            lbl.set_margin_start(10); lbl.set_margin_end(10)
            lbl.add_css_class("dim")
            row.set_child(lbl)
            row.set_selectable(False)
            self.env_list.append(row)
            return

        chosen = None
        for i, rt in enumerate(found):
            row = Gtk.ListBoxRow()
            lbl = Gtk.Label(label=rt.label(), xalign=0)
            lbl.set_wrap(True)
            lbl.set_max_width_chars(52)
            lbl.set_margin_top(8); lbl.set_margin_bottom(8)
            lbl.set_margin_start(10); lbl.set_margin_end(10)
            if not rt.complete:
                lbl.add_css_class("dim")
            row.set_child(lbl)
            row.runtime = rt
            self.env_list.append(row)
            if i == select:
                chosen = row
        self.env_list.select_row(chosen or self.env_list.get_row_at_index(0))

    def _build_watch(self):
        view, box = self._view()

        self.history_group = Adw.PreferencesGroup(title="Continue watching")
        browse_btn = Gtk.Button(label="Browse…")
        browse_btn.add_css_class("suggested-action")
        browse_btn.connect("clicked", self.on_browse)
        self.history_group.set_header_suffix(browse_btn)

        self.history_list = Gtk.ListBox()
        self.history_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.history_list.add_css_class("boxed-list")
        holder = Gtk.ScrolledWindow()
        holder.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        holder.set_min_content_height(120)
        holder.set_max_content_height(300)
        holder.set_child(self.history_list)
        self.history_group.add(holder)
        box.append(self.history_group)

        playing = Adw.PreferencesGroup(
            title="Playing",
            description="A URL here is handed to Syncplay, which puts it on the shared "
                        "playlist — so it starts for everyone in the room, whoever set "
                        "it. It also skips Syncplay's setup dialog, which only stays "
                        "away while a URL is set.")
        self._entry(playing, "URL", "play_url",
                    "https://…  — leave blank to pick a file in Syncplay yourself")
        box.append(playing)

        self.queue_note = Gtk.Label(label="", xalign=0)
        self.queue_note.set_wrap(True)
        self.queue_note.set_hexpand(True)
        self.queue_note.set_visible(False)
        box.append(self.queue_note)
        return view

    def refresh_history(self):
        """Redraw Continue watching from the store."""
        if not hasattr(self, "history_list"):
            return
        clear_list(self.history_list)
        entries = self.history.entries()
        if not entries:
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            label = self._note("Nothing watched yet. Browse… finds a series and puts "
                               "its episodes on the shared playlist.")
            label.set_margin_top(12)
            label.set_margin_bottom(12)
            label.set_margin_start(12)
            label.set_margin_end(12)
            row.set_child(label)
            self.history_list.append(row)
            return

        for entry in entries:
            season = int(entry.get("season") or 0)
            episode = int(entry.get("episode") or 0)
            row = Adw.ActionRow(title=entry.get("name") or entry.get("id"))
            if season and episode:
                # The stored episode is the last one queued, so the interesting
                # one is the next.
                row.set_subtitle("watched S%02dE%02d  ·  next S%02dE%02d"
                                 % (season, episode, season, episode + 1))
            resume = Gtk.Button(label="Resume")
            resume.set_valign(Gtk.Align.CENTER)
            resume.connect("clicked", self.on_resume_entry, entry)
            row.add_suffix(resume)
            forget = Gtk.Button(icon_name="user-trash-symbolic")
            forget.set_tooltip_text("Remove from this list")
            forget.set_valign(Gtk.Align.CENTER)
            forget.add_css_class("flat")
            forget.connect("clicked", self.on_forget_entry, entry)
            row.add_suffix(forget)
            row.set_activatable_widget(resume)
            self.history_list.append(row)

    def on_resume_entry(self, _btn, entry):
        self.collect()
        win = BrowseWindow(self)
        win.present()
        win.open_series(Series(entry.get("id", ""), entry.get("name") or "",
                               entry.get("year") or ""),
                        season=int(entry.get("season") or 0),
                        after=int(entry.get("episode") or 0))

    def on_forget_entry(self, _btn, entry):
        self.history.forget(entry.get("id", ""))
        self.refresh_history()
        self.toast("Removed %s" % (entry.get("name") or "series"))

    def on_browse(self, _btn):
        self.collect()
        BrowseWindow(self).present()

    def adopt_queue(self, urls):
        """Take the browser's episode list.

        The first URL rides the existing path — Syncplay's positional file, which
        it puts on the shared playlist itself. The rest wait for _launch_worker,
        because a playlist set before anyone is in the room is discarded.
        """
        self.queue = list(urls)
        if not self.queue:
            return
        self.e_play_url.set_text(self.queue[0])
        self.cfg["play_url"] = self.queue[0]
        self.cfg["last_play_url"] = self.queue[0]
        if len(self.queue) > 1:
            self.queue_note.set_text("%d episodes queued. They go on the shared playlist "
                                     "once Syncplay is in the room." % len(self.queue))
            self.queue_note.set_visible(True)
        else:
            self.queue_note.set_visible(False)

    def on_rescan(self, _btn=None):
        self.collect()
        self.where_note.set_text("Scanning…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self, autostart=False):
        # Bring the remembered container up before scanning, so it is probed as
        # a running one and comes back selected and ready rather than "stopped,
        # not scanned" — which is what it looked like every launch otherwise.
        if (autostart and self.cfg["autostart_container"]
                and self.cfg["runtime_kind"] == "distrobox" and self.cfg["container"]):
            name = str(self.cfg["container"]).strip()
            if name and name != current_container():
                self.log("Starting %s, the environment you used last…" % name)
                if start_container(name, log=self.log):
                    self.log("%s is up." % name)

        found = scan_runtimes(allow_start=bool(self.cfg["scan_stopped"]), log=self.log)

        def apply():
            self.runtimes = found

            # keep the saved choice if it still exists, else take the best one
            want = self.cfg["runtime_kind"]
            if want == "distrobox" and self.cfg["container"]:
                want = "distrobox:" + self.cfg["container"]

            index = None
            for i, rt in enumerate(found):
                if rt.key == want:
                    index = i
                    break
                if index is None and rt.complete:
                    index = i

            self._set_env_rows(found, "Nothing found to launch from", index or 0)
            self.on_env_selected()

            complete = [r for r in found if r.complete]
            if not found:
                self.where_note.set_text("No launch environment found.")
            elif complete:
                self.where_note.set_text(
                    "%d of %d environments have both Syncplay and mpv. Those are listed first."
                    % (len(complete), len(found)))
            else:
                self.where_note.set_text(
                    "Nothing has both Syncplay and mpv yet. Install them where you want to "
                    "watch, then press Rescan.")
            return False

        GLib.idle_add(apply)
        for r in found:
            self.log("Found %s" % r.label())

    def selected_runtime(self):
        row = self.env_list.get_selected_row()
        return getattr(row, "runtime", None) if row is not None else None

    def missing_in(self, rt):
        missing = []
        if not rt.has_syncplay:
            missing.append("syncplay")
        if not rt.has_mpv:
            missing.append("mpv")
        return missing

    def on_env_selected(self, *_a):
        rt = self.selected_runtime()
        if rt is None:
            self.env_detail.set_text("")
            self.btn_install.set_visible(False)
            return
        where = "this system" if rt.kind == "native" else "distrobox '%s'" % rt.name
        missing = self.missing_in(rt)
        for c in ("result-warn", "dim"):
            self.env_detail.remove_css_class(c)
        if rt.has_syncplay is None:
            self.env_detail.set_text("%s has not been scanned — it is stopped." % where)
            self.env_detail.add_css_class("dim")
            self.btn_install.set_visible(False)
            return
        if not missing:
            self.env_detail.set_text("Ready: %s" % rt.status_text())
            self.env_detail.add_css_class("dim")
            self.btn_install.set_visible(False)
            return
        self.env_detail.set_text("%s is missing from %s."
                                 % (" and ".join(missing), where))
        self.env_detail.add_css_class("result-warn")
        self.btn_install.set_label("Install " + " + ".join(missing))
        self.btn_install.set_visible(True)

    def on_install_missing(self, _btn=None):
        rt = self.selected_runtime()
        if rt is None:
            return
        missing = self.missing_in(rt)
        argv, how, _term = install_plan(rt, missing)
        if argv is None:
            self.log(how)
            self.env_detail.set_text(how)
            return
        self.btn_install.set_sensitive(False)
        self.log("Installing %s — %s." % (" and ".join(missing), how))

        def work():
            ok = stream_command(argv, self.log)
            self.log("Install finished." if ok else
                     "Install failed. The command above shows why.")

            def done():
                self.btn_install.set_sensitive(True)
                return False

            GLib.idle_add(done)
            if ok:
                self._scan_worker()

        threading.Thread(target=work, daemon=True).start()

    def _build_verify(self):
        group = Adw.PreferencesGroup(title="Route check")

        btn = Gtk.Button(label="Check the route")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self.on_test)
        self.btn_test = btn
        group.set_header_suffix(btn)

        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results.add_css_class("boxed-list")
        self.results.set_hexpand(True)
        group.add(self.results)

        self.verdict = Gtk.Label(label="Not checked yet.", xalign=0)
        self.verdict.set_wrap(True)
        self.verdict.set_hexpand(True)
        self.verdict.set_margin_top(10)
        group.add(self.verdict)
        # Kept under the old name: on_role_changed hides the whole thing in host
        # mode, where there is nothing to route through.
        self.frame_verify = group
        return group

    def _build_setup(self):
        page = Adw.PreferencesPage()

        conn = Adw.PreferencesGroup(
            title="Connection",
            description="Ports used on this machine only. Change them if something "
                        "else already listens there.")
        self._spin(conn, "SOCKS5 port", "socks_port", 1, 65535,
                   "Used by yt-dlp and ALL_PROXY.")
        self._spin(conn, "HTTP bridge port", "http_port", 1, 65535,
                   "Used by mpv and FFmpeg, which only speak HTTP CONNECT.")
        self._spin(conn, "SSH port", "host_ssh_port", 1, 65535,
                   "The port sshd listens on at the other end.")
        page.add(conn)

        sync = Adw.PreferencesGroup(
            title="Syncplay",
            description="Leave blank to use Syncplay's own saved settings. The server "
                        "and room are required to queue more than one episode.")
        self._entry(sync, "Server", "syncplay_server", "syncplay.pl:8997")
        self._entry(sync, "Room", "syncplay_room", "the room you both join")
        self._entry(sync, "Display name", "syncplay_user", "the name the other side sees")
        page.add(sync)

        lib = Adw.PreferencesGroup(
            title="Library",
            description="The Real-Debrid key is stored in config.json, which is written "
                        "owner-only, and it grants full access to that account. It is "
                        "kept out of the activity log.")
        self._entry(lib, "Real-Debrid API key", "rd_api_key",
                    "from real-debrid.com/apitoken", password=True)
        self._entry(lib, "Torrentio options", "torrentio_opts",
                    "pipe-joined, e.g. sort=qualitysize|qualityfilter=cam,scr")
        self._entry(lib, "Preferred quality", "preferred_quality",
                    "matched exactly first, e.g. 1080p")
        page.add(lib)

        play = Adw.PreferencesGroup(title="Playback")
        self._entry(play, "Extra mpv flags", "mpv_extra",
                    "--cache=yes --demuxer-max-bytes=200M")
        self._switch(play, "Skip Syncplay's setup dialog", "skip_syncplay_dialog",
                     "Writes forceguiprompt = False into Syncplay's own config. Only "
                     "takes effect while a URL is set.")
        self._switch(play, "Trust the domain of what is played", "trust_play_domain",
                     "Adds each hostname to Syncplay's trusted domains, so switching "
                     "episode never stops for a confirmation.")
        page.add(play)

        safety = Adw.PreferencesGroup(
            title="Safety",
            description="What happens when the route cannot be trusted.")
        self._switch(safety, "Require a verified route before launching",
                     "require_verified",
                     "Leave this on. It is what stops playback going out from this "
                     "machine, and it also gates resolving debrid links.")
        self._switch(safety, "Stop the container when the tunnel drops",
                     "stop_container_on_drop",
                     "Shuts the distrobox down too, so nothing is left running.")
        self._spin(safety, "Watchdog interval", "check_interval", 3, 300,
                   "Seconds between tests that traffic still goes through the tunnel.")
        self._spin(safety, "Failures before stopping", "max_fails", 1, 20,
                   "Consecutive failures tolerated before everything is killed.")
        page.add(safety)

        upkeep = Adw.PreferencesGroup(
            title="Stored data",
            description="Everything lives in %s." % DATA_DIR)
        self.cache_row = Adw.ActionRow(title="Cached lookups")
        clear_cache = Gtk.Button(label="Clear")
        clear_cache.set_valign(Gtk.Align.CENTER)
        clear_cache.connect("clicked", self.on_clear_cache)
        self.cache_row.add_suffix(clear_cache)
        upkeep.add(self.cache_row)

        self.history_row = Adw.ActionRow(title="Watch history")
        clear_hist = Gtk.Button(label="Clear")
        clear_hist.set_valign(Gtk.Align.CENTER)
        clear_hist.add_css_class("destructive-action")
        clear_hist.connect("clicked", self.on_clear_history)
        self.history_row.add_suffix(clear_hist)
        upkeep.add(self.history_row)
        page.add(upkeep)

        self._refresh_upkeep()
        return page

    def _refresh_upkeep(self):
        if not hasattr(self, "cache_row"):
            return
        entries = self.cache.count()
        self.cache_row.set_subtitle(
            "%d saved answer%s — searches for a week, episode lists for a day, "
            "sources for six hours" % (entries, "" if entries == 1 else "s"))
        seen = len(self.history.entries())
        self.history_row.set_subtitle(
            "%d series remembered" % seen if seen else "nothing remembered yet")

    def on_clear_cache(self, _btn):
        self.cache.clear()
        self._refresh_upkeep()
        self.toast("Cached lookups cleared")

    def on_clear_history(self, _btn):
        self.history.clear()
        self.refresh_history()
        self._refresh_upkeep()
        self.toast("Watch history cleared")

    def _build_log(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        sw = Gtk.ScrolledWindow()
        # Horizontal NEVER is what forces the view to take the width available
        # and wrap into it. With AUTOMATIC the scroller collapses to its minimum.
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_hexpand(True)
        sw.set_vexpand(True)

        self.logview = Gtk.TextView()
        self.logview.set_editable(False)
        self.logview.set_cursor_visible(False)
        self.logview.set_monospace(True)
        self.logview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.logview.set_hexpand(True)
        self.logview.set_left_margin(12)
        self.logview.set_right_margin(12)
        self.logview.set_top_margin(10)
        self.logview.set_bottom_margin(10)
        self.logbuf = self.logview.get_buffer()

        sw.set_child(self.logview)
        box.append(sw)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, "set_margin_" + side)(12)
        hint = self._note("Also written to %s" % LOG_FILE)
        hint.set_hexpand(True)
        bar.append(hint)
        copy = Gtk.Button(label="Copy")
        copy.connect("clicked", self.on_copy_log)
        bar.append(copy)
        clear = Gtk.Button(label="Clear")
        clear.connect("clicked", self.on_clear_log)
        bar.append(clear)
        box.append(bar)
        return box

    def on_copy_log(self, btn):
        start, end = self.logbuf.get_bounds()
        text = self.logbuf.get_text(start, end, False)
        try:
            btn.get_clipboard().set(text)
            self.toast("Activity log copied")
        except Exception:
            self.toast("Could not reach the clipboard")

    def on_clear_log(self, _btn):
        self.logbuf.set_text("")
        self.toast("Cleared on screen — %s still has the full record" % LOG_FILE.name)

    def _build_actions(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(bar, "set_margin_" + side)(12)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self.btn_stop = Gtk.Button(label="Stop session")
        self.btn_stop.connect("clicked", self.on_stop)
        self.btn_stop.set_sensitive(False)
        bar.append(self.btn_stop)

        self.btn_launch = Gtk.Button(label="Start watching")
        self.btn_launch.add_css_class("suggested-action")
        self.btn_launch.connect("clicked", self.on_launch)
        bar.append(self.btn_launch)
        return bar

    # -- plumbing -------------------------------------------------------- #

    def _env_banner(self):
        here = current_container()
        where = ("inside container '%s'" % here) if here else "on the host system"
        tools = []
        for t in ("ssh", "curl", "tailscale"):
            if not which(t):
                tools.append(t)
        self.log("Running %s." % where)
        if tools:
            self.log("Not found on PATH: %s" % ", ".join(tools))
        if in_container() and not which("distrobox-host-exec"):
            self.log("distrobox-host-exec is missing, so other containers can't be listed "
                     "from in here.")

    def _seed_history(self):
        """Carry the old single-slot bookmark into the history list once.

        Before there was a history there was one remembered series. Somebody
        upgrading should still see it under Continue watching rather than an
        empty panel.
        """
        if self.history.entries():
            return
        sid = str(self.cfg["library_series_id"] or "").strip()
        if not sid:
            return
        self.history.remember(sid, str(self.cfg["library_series_name"] or sid), "",
                              self.cfg["library_season"], self.cfg["library_episode"])

    def collect(self):
        for key in DEFAULTS:
            w = getattr(self, "e_" + key, None)
            if w is not None:
                self.cfg[key] = w.get_text().strip()
                continue
            w = getattr(self, "s_" + key, None)
            if w is not None:
                self.cfg[key] = int(w.get_value())
                continue
            w = getattr(self, "w_" + key, None)
            if w is not None:
                self.cfg[key] = bool(w.get_active())

        rt = self.selected_runtime()
        if rt is not None:
            self.cfg["runtime_kind"] = rt.kind
            self.cfg["container"] = rt.name

    def log(self, text):
        line = "[%s] %s\n" % (stamp(), text)

        def write():
            self.logbuf.insert(self.logbuf.get_end_iter(), line)
            mark = self.logbuf.create_mark(None, self.logbuf.get_end_iter(), False)
            self.logview.scroll_to_mark(mark, 0.0, True, 0.0, 1.0)
            return False

        GLib.idle_add(write)
        try:
            with LOG_FILE.open("a") as fh:
                fh.write("[%s] %s\n" % (datetime.now().isoformat(timespec="seconds"), text))
        except Exception:
            pass

    def set_pill(self, text, kind):
        for c in ("pill-idle", "pill-work", "pill-ok", "pill-bad"):
            self.pill.remove_css_class(c)
        self.pill.add_css_class("pill-" + kind)
        self.pill.set_text(text)

    def on_state(self, state):
        if state == "idle":
            self.set_pill("Host — no tunnel" if self.cfg["role"] == "host"
                          else "Not connected", "ok" if self.cfg["role"] == "host" else "idle")
            self.btn_stop.set_sensitive(False)
            self.btn_launch.set_sensitive(True)
            self.verified = False
        elif state == "lost":
            self.set_pill("Tunnel lost", "bad")
            self.verdict.set_text("The tunnel dropped mid-session. Everything was stopped.")
        elif state == "playing-host":
            self.set_pill("Playing locally", "ok")
            self.btn_stop.set_sensitive(True)
            self.btn_launch.set_sensitive(False)
        elif state == "playing":
            self.set_pill("Streaming through host", "ok")
            self.btn_stop.set_sensitive(True)
            self.btn_launch.set_sensitive(False)
        return False

    def set_busy(self, busy):
        self.busy = busy
        self.btn_test.set_sensitive(not busy)
        return False

    # -- actions --------------------------------------------------------- #

    def open_key_dialog(self, reason=None):
        """Ask for the host password once and install our public key.

        The password stays in a bytearray that gets zeroed as soon as
        ssh-copy-id is done. It is never written to the config, the log, the
        environment, or a command line.
        """
        self.collect()
        user = self.cfg["host_user"].strip()
        host = self.cfg["host_ip"].strip()
        if not host or not user:
            self.log("Fill in the host address and SSH user before setting up the key.")
            return False

        dlg = Gtk.Window(title="Authorise this machine", transient_for=self, modal=True)
        dlg.set_default_size(440, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(16)
        dlg.set_child(box)

        head = Gtk.Label(xalign=0)
        head.set_markup("<b>%s@%s doesn't trust this machine yet</b>" % (user, host))
        box.append(head)

        why = Gtk.Label(
            label=(reason or "SSH turned down the key.") +
                  "\n\nEnter the password for that account once. This copies your public key "
                  "over, and the host stops asking after that.",
            xalign=0)
        why.set_wrap(True)
        why.set_max_width_chars(52)
        box.append(why)

        pw = Gtk.PasswordEntry()
        pw.set_show_peek_icon(True)
        pw.set_property("placeholder-text", "password for %s@%s" % (user, host))
        pw.set_hexpand(True)
        box.append(pw)

        status = Gtk.Label(label="", xalign=0)
        status.set_wrap(True)
        status.set_max_width_chars(52)
        box.append(status)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        confirm = Gtk.Button(label="Copy key to host")
        confirm.add_css_class("suggested-action")
        buttons.append(cancel)
        buttons.append(confirm)
        box.append(buttons)

        def close(*_a):
            pw.set_text("")
            dlg.close()

        cancel.connect("clicked", close)

        def go(*_a):
            secret = bytearray(pw.get_text().encode())
            pw.set_text("")
            if not secret:
                status.set_text("Enter the password first.")
                return
            confirm.set_sensitive(False)
            cancel.set_sensitive(False)
            pw.set_sensitive(False)
            status.set_text("Copying the key over…")

            def work():
                try:
                    ok, msg = ssh_copy_id(user, host, int(self.cfg["host_ssh_port"]),
                                          secret, log=self.log)
                finally:
                    for i in range(len(secret)):
                        secret[i] = 0

                if ok:
                    rc, _, _ = run(self.session.ssh_base() + ["true"], timeout=25)
                    if rc != 0:
                        ok = False
                        msg = "The key copied, but SSH still won't log in without a password."
                    else:
                        # Narrow the key down now that it is proven to work: an
                        # unrestricted entry is a full shell on the host for
                        # anyone who ends up holding this private key.
                        pub = find_ssh_key()
                        if pub is not None:
                            restrict_authorized_key(self.session.ssh_base(), pub,
                                                    log=self.log)

                def done():
                    status.set_text(msg)
                    self.log(msg)
                    cancel.set_sensitive(True)
                    cancel.set_label("Close")
                    if ok:
                        confirm.set_label("Check the route")
                        confirm.set_sensitive(True)
                        confirm.disconnect_by_func(go)
                        confirm.connect("clicked", lambda *_x: (close(), self.on_test(None)))
                    else:
                        pw.set_sensitive(True)
                        confirm.set_sensitive(True)
                    return False

                GLib.idle_add(done)

            threading.Thread(target=work, daemon=True).start()

        confirm.connect("clicked", go)
        pw.connect("activate", go)
        dlg.present()
        return False

    def on_save(self, _btn=None):
        """Kept for the explicit path; editing a setting also saves on its own."""
        self.collect()
        try:
            self.cfg.save()
            self.log("Settings saved to %s" % CONFIG_FILE)
        except Exception as exc:
            self.log("Settings not saved: %s" % exc)

    def on_test(self, _btn):
        self.collect()
        if not self.cfg["host_ip"]:
            self.log("Enter the host IP first.")
            return
        self.set_busy(True)
        self.set_pill("Checking", "work")
        self.verified = False
        self.verdict.set_text("Checking…")

        child = self.results.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.results.remove(child)
            child = nxt

        self.rows = {
            "peer": Row(self.results, "Host is reachable on Tailscale"),
            "ssh": Row(self.results, "SSH accepts our key"),
            "tunnel": Row(self.results, "SOCKS5 tunnel is listening"),
            "direct": Row(self.results, "This machine's own public IP"),
            "socks": Row(self.results, "Public IP seen through SOCKS5"),
            "bridge": Row(self.results, "Public IP seen through the mpv/FFmpeg path"),
            "host": Row(self.results, "Host's own public IP (asked over SSH)"),
        }
        threading.Thread(target=self._test_worker, daemon=True).start()

    def _row(self, key, state, text=None):
        GLib.idle_add(lambda: (self.rows[key].set(state, text), False)[1])

    def _test_worker(self):
        s = self.session
        cfg = self.cfg

        # 1. peer reachable
        if which("tailscale"):
            rc, out, _ = run(["tailscale", "ping", "-c", "1", "--timeout", "5s", cfg["host_ip"]], timeout=15)
            if rc == 0:
                relay = "via relay" if "via DERP" in out else "direct"
                self._row("peer", "pass", "Host answers on Tailscale (%s)" % relay)
            else:
                self._row("peer", "warn", "Tailscale ping failed — trying SSH anyway")
        else:
            self._row("peer", "warn", "tailscale not installed here; skipped")

        # 2. ssh
        rc, _, err = run(s.ssh_base() + ["true"], timeout=25)
        if rc != 0:
            reason = s._explain_ssh(err)
            self._row("ssh", "fail", "SSH failed — %s" % reason)
            for k in ("tunnel", "direct", "socks", "bridge", "host"):
                self._row(k, "fail", None)
            if "ssh-copy-id" in reason:
                GLib.idle_add(self.open_key_dialog, "SSH turned down the key.")
                return self._finish(False, "The host hasn't got this machine's key yet.")
            return self._finish(False, "SSH could not reach the host, so nothing else was tested.")
        self._row("ssh", "pass", "SSH accepts our key")

        # 3. tunnel
        ok, msg = s.open_tunnel()
        if not ok:
            self._row("tunnel", "fail", msg)
            return self._finish(False, msg)
        self._row("tunnel", "pass", "SOCKS5 on :%d, HTTP bridge on :%d" % (cfg["socks_port"], cfg["http_port"]))

        if True:
            direct, _ = s.curl_ip("direct")
            self._row("direct", "pass" if direct else "warn",
                      "This machine: %s" % (direct or "unknown"))

            socks_ip, _ = s.curl_ip("socks")
            if not socks_ip:
                self._row("socks", "fail", "Nothing came back through the SOCKS5 tunnel")
                return self._finish(False, "The tunnel is open but no traffic gets through it.")
            self._row("socks", "pass", "Through SOCKS5: %s" % socks_ip)

            bridge_ip, _ = s.curl_ip("bridge")
            if not bridge_ip:
                self._row("bridge", "fail", "The HTTP bridge did not answer — mpv would leak")
                return self._finish(False, "The mpv/FFmpeg path is not working, so playback would go out from here.")
            self._row("bridge", "pass", "Through the mpv path: %s" % bridge_ip)

            host_ip, _ = s.host_public_ip()
            if not host_ip:
                self._row("host", "warn", "Could not read the host's own IP")
            else:
                self._row("host", "pass", "Host reports: %s" % host_ip)

            # verdict
            if direct and socks_ip == direct:
                return self._finish(False, "Leak: traffic through the tunnel still shows this machine's IP (%s)." % direct)
            if bridge_ip != socks_ip:
                return self._finish(False, "The two paths exit differently (%s vs %s). Playback would not match the tunnel."
                                    % (socks_ip, bridge_ip))
            if host_ip and socks_ip != host_ip:
                return self._finish(False, "Traffic exits from %s, but the host says its address is %s. That is not the host."
                                    % (socks_ip, host_ip))
            if not host_ip:
                return self._finish(True, "Traffic exits from %s, which is not this machine — but the host's own IP could "
                                          "not be confirmed." % socks_ip, strong=False)
        return self._finish(True, "Confirmed: everything exits from the host at %s. This machine's %s is not exposed."
                            % (host_ip, direct or "address"))

    def _finish(self, ok, message, strong=True):
        def apply():
            self.verified = ok and strong
            self.verdict.set_text(message)
            self.set_pill("Route verified" if ok else "Not verified", "ok" if ok else "bad")
            self.set_busy(False)
            return False

        GLib.idle_add(apply)
        self.log(message)
        if not ok:
            self.session.close_tunnel()
        return None

    def on_launch(self, _btn):
        self.collect()
        host_mode = self.cfg["role"] == "host"
        if not host_mode and self.cfg["require_verified"] and not self.verified:
            self.verdict.set_text("Check the route first — or turn the requirement off in Advanced.")
            self.log("Launch blocked: the route has not been verified in this session.")
            return
        rt = self.selected_runtime()
        if rt is None:
            self.log("Pick an environment under Where to play first.")
            return
        if not rt.complete:
            missing = []
            if not rt.has_syncplay:
                missing.append("Syncplay")
            if not rt.has_mpv:
                missing.append("mpv")
            where = "this system" if rt.kind == "native" else rt.name
            note = "%s is missing from %s." % (" and ".join(missing) or "Nothing", where)
            self.verdict.set_text(note)
            self.log(note + " Use the install button under Where to play, or pick "
                            "another environment.")
            return

        self.btn_launch.set_sensitive(False)
        threading.Thread(target=self._launch_worker, args=(rt, host_mode),
                         daemon=True).start()

    def _prepare_syncplay(self, rt):
        """Make Syncplay start playing on its own, if that was asked for.

        $HOME is shared with every distrobox, so one config file covers all of
        them — the same reason the mpv wrapper is written once.
        """
        url = self.cfg["play_url"].strip()
        if not self.cfg["skip_syncplay_dialog"]:
            return
        if not url:
            # Syncplay forces the dialog whenever it is handed no file
            # (ConfigurationGetter "forceGuiPrompt == True or not file"). The
            # only documented escape is --no-gui, which drops it to a console
            # interface and loses the playlist entirely. So the nearest thing to
            # "always skip" is to hand it the last thing that was played.
            url = str(self.cfg["last_play_url"] or "").strip()
            if url:
                self.cfg["play_url"] = url
                GLib.idle_add(lambda: (self.e_play_url.set_text(url), False)[1])
                self.log("No URL set, so the last one is being reused to keep "
                         "Syncplay's setup dialog away: %s" % url)
            else:
                self.log("No URL set and none remembered, so Syncplay will show its "
                         "setup dialog — it forces the dialog whenever no file is "
                         "given, and the only way round that loses its playlist.")
                return
        # Every queued episode's domain is trusted up front. A season can span
        # more than one debrid host, and an untrusted one interrupts playback
        # with a confirmation halfway through.
        self.cfg["last_play_url"] = url
        prepare_syncplay_ini(self.queue or [url], bool(self.cfg["trust_play_domain"]),
                             log=self.log)
        self.log("Syncplay will open %s and put it on the shared playlist for the room."
                 % url)
        if not self.cfg["trust_play_domain"]:
            self.log("Whoever else is in the room still has to confirm that domain once.")
        self.log("Note: Syncplay saves the player path it was given, so a later plain "
                 "'syncplay' run will also use the proxied mpv wrapper.")

    def _launch_worker(self, rt, host_mode=False):
        s = self.session
        s.begin()
        if not host_mode:
            ok, msg = s.open_tunnel()
            if not ok:
                self.log(msg)
                GLib.idle_add(self.on_state, "idle")
                return
        if not s.write_mpv_wrapper(rt, proxied=not host_mode):
            GLib.idle_add(self.on_state, "idle")
            return
        self._prepare_syncplay(rt)
        if not host_mode:
            s.start_watchdog()
        s.launch_player(rt, proxied=not host_mode)
        GLib.idle_add(self.on_state, "playing-host" if host_mode else "playing")
        if not host_mode:
            notify(APP_NAME, "Connected. Syncplay is starting.")
        if len(self.queue) > 1:
            self._push_queue()

    def _push_queue(self):
        """Put the queued episodes on the room's shared playlist.

        Deliberately after the player is up: the server drops a room, and its
        playlist with it, the moment the last watcher leaves, so a queue pushed
        before Syncplay has joined would be queued into nothing.
        """
        server = str(self.cfg["syncplay_server"] or "").strip()
        room = str(self.cfg["syncplay_room"] or "").strip()
        if not server or not room:
            self.log("Queued %d episodes, but the Syncplay server and room have to be "
                     "set in Advanced for the rest of them to be sent. Only the first "
                     "one will play." % len(self.queue))
            return
        user = str(self.cfg["syncplay_user"] or "").strip() or "tunnel"
        pusher = SyncplayPush(server, room, "%s-queue" % user, log=self.log)
        ok, msg = pusher.push(self.queue)
        self.log(msg)
        if not ok:
            self.log("The first episode still plays — only the rest of the queue was "
                     "lost. Everything is on the shared playlist once it works.")

    def on_stop(self, _btn):
        threading.Thread(target=lambda: self.session.stop_all("user"), daemon=True).start()

    def _auto(self):
        """--launch: wait for the scan, check the route, then start if it passed.

        Every wait happens off the main loop so the window stays responsive.
        """
        def drive():
            deadline = time.time() + 25
            while not self.runtimes and time.time() < deadline:
                time.sleep(0.2)
            GLib.idle_add(lambda: (self.on_test(None), False)[1])
            time.sleep(0.6)
            while self.busy:
                time.sleep(0.3)
            if self.verified:
                GLib.idle_add(lambda: (self.on_launch(None), False)[1])

        threading.Thread(target=drive, daemon=True).start()
        return False
