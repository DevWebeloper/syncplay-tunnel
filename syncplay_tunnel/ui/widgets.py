"""Shared widget helpers and the stylesheet.

The list helpers all hang their payload on the row as a plain Python attribute,
so every list in the app is read the same way.
"""
from .. import gtk_setup  # noqa: F401  (must precede gi.repository)
from gi.repository import Gtk

CSS = b"""
.pill { padding: 4px 12px; border-radius: 999px; font-weight: bold; }
.pill-idle { background: alpha(currentColor, 0.12); }
.pill-work { background: alpha(#e5a50a, 0.30); }
.pill-ok   { background: alpha(#2ec27e, 0.35); }
.pill-bad  { background: alpha(#e01b24, 0.35); }
.mono { font-family: monospace; font-size: 0.9em; }
.dim { opacity: 0.65; }
.result-pass { color: #2ec27e; font-weight: bold; }
.result-fail { color: #e01b24; font-weight: bold; }
.result-warn { color: #e5a50a; font-weight: bold; }
.section-title { font-weight: bold; }
"""


class Row:
    """One line in the verification results list."""

    def __init__(self, listbox, text):
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.set_margin_top(4)
        self.box.set_margin_bottom(4)
        self.box.set_margin_start(8)
        self.box.set_margin_end(8)
        self.mark = Gtk.Label(label="…")
        self.mark.set_size_request(20, -1)
        self.label = Gtk.Label(label=text, xalign=0)
        self.label.set_wrap(True)
        self.label.set_hexpand(True)
        self.label.set_max_width_chars(50)
        self.box.append(self.mark)
        self.box.append(self.label)
        row = Gtk.ListBoxRow()
        row.set_child(self.box)
        row.set_activatable(False)
        listbox.append(row)

    def set(self, state, text=None):
        marks = {"pass": "✓", "fail": "✗", "warn": "!", "busy": "…"}
        classes = {"pass": "result-pass", "fail": "result-fail", "warn": "result-warn", "busy": "dim"}
        self.mark.set_text(marks.get(state, "·"))
        for c in ("result-pass", "result-fail", "result-warn", "dim"):
            self.mark.remove_css_class(c)
        self.mark.add_css_class(classes.get(state, "dim"))
        if text is not None:
            self.label.set_text(text)


def block_scroll_steal(widget):
    """Stop a spin control eating scroll meant for the page underneath it.

    GtkScrolledWindow scrolls from a bubble-phase controller, and GtkSpinButton
    has a bubble-phase scroll controller of its own. Bubble runs deepest-first,
    so the spin button always won: scrolling past the port fields changed the
    ports instead of moving the page.

    A capture-phase controller here runs before the spin button ever sees the
    event, because capture goes top-down. Swallowing it there also stops the
    scroller's own bubble handler, though, which would freeze the page instead
    — so the delta is handed to the enclosing scroller by hand. Attaching this
    to an AdwSpinRow covers the spin button nested inside it for the same
    top-down reason.
    """
    ctrl = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
    ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

    def on_scroll(_ctrl, _dx, dy):
        scroller = widget.get_ancestor(Gtk.ScrolledWindow)
        if scroller is not None:
            adj = scroller.get_vadjustment()
            # dy already arrives in scroll units, so one step per unit is what
            # the scroller would have done itself. Multiplying it makes the page
            # jump several times faster over a spin row than anywhere else.
            step = adj.get_step_increment() or 50
            top = max(adj.get_lower(), adj.get_upper() - adj.get_page_size())
            adj.set_value(min(max(adj.get_value() + dy * step, adj.get_lower()), top))
        return True

    ctrl.connect("scroll", on_scroll)
    widget.add_controller(ctrl)
    return widget


def clear_list(listbox):
    """Empty a ListBox. GTK4 has no remove_all, so walk the siblings."""
    child = listbox.get_first_child()
    while child:
        nxt = child.get_next_sibling()
        listbox.remove(child)
        child = nxt


def list_row(text, dim=False, payload=None, attr="item"):
    """One padded row, with its object hung on as a plain Python attribute.

    Same shape the environment and host pickers use, so every list in the app
    reads and behaves the same way.
    """
    row = Gtk.ListBoxRow()
    lbl = Gtk.Label(label=text, xalign=0)
    lbl.set_wrap(True)
    lbl.set_max_width_chars(64)
    lbl.set_margin_top(8)
    lbl.set_margin_bottom(8)
    lbl.set_margin_start(10)
    lbl.set_margin_end(10)
    if dim:
        lbl.add_css_class("dim")
    row.set_child(lbl)
    if payload is not None:
        setattr(row, attr, payload)
    return row


def check_row(text, payload=None, attr="item"):
    """A row with a real tick box, toggled by clicking anywhere on it.

    A plain multi-select list needed ctrl-click to add a second episode, which
    is not what clicking a list of episodes should mean. This is a checklist:
    click to tick, click again to untick.
    """
    row = Gtk.ListBoxRow()
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    box.set_margin_top(8)
    box.set_margin_bottom(8)
    box.set_margin_start(10)
    box.set_margin_end(10)

    check = Gtk.CheckButton()
    check.set_valign(Gtk.Align.CENTER)
    box.append(check)

    label = Gtk.Label(label=text, xalign=0)
    label.set_wrap(True)
    label.set_max_width_chars(60)
    label.set_hexpand(True)
    box.append(label)

    row.set_child(box)
    row.check = check
    # Clicking the box itself is handled by the box; this covers the rest of
    # the row, so the whole line is a target.
    row.set_activatable(True)
    if payload is not None:
        setattr(row, attr, payload)
    return row


def toggle_row(_listbox, row):
    """Flip a check row. Wired to row-activated."""
    check = getattr(row, "check", None)
    if check is not None:
        check.set_active(not check.get_active())


def checked_rows(listbox):
    """Every ticked row, in order."""
    out = []
    child = listbox.get_first_child()
    while child:
        check = getattr(child, "check", None)
        if check is not None and check.get_active():
            out.append(child)
        child = child.get_next_sibling()
    return out


def scrolled_list(mode=Gtk.SelectionMode.SINGLE, min_height=150, max_height=320):
    """A ListBox in a vertical-only scroller. Returns (scroller, listbox)."""
    lb = Gtk.ListBox()
    lb.set_selection_mode(mode)
    lb.add_css_class("boxed-list")
    lb.set_hexpand(True)
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    sw.set_min_content_height(min_height)
    sw.set_max_content_height(max_height)
    sw.set_hexpand(True)
    sw.set_vexpand(True)
    sw.set_child(lb)
    return sw, lb
