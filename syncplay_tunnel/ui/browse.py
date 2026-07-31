"""The episode browser: search, episodes, the chosen sources, one source list."""
import threading

from .. import gtk_setup  # noqa: F401  (must precede gi.repository)
from gi.repository import Adw, GLib, Gtk

from ..library import (Series, cinemeta_episodes, cinemeta_search, pick_source,
                       rd_fallback_sources, torrentio_is_down,
                       torrentio_sources)
from ..util import curl_final_url, redact
from .widgets import (check_row, checked_rows, clear_list, list_row,
                      scrolled_list, toggle_row)

class BrowseWindow(Adw.Window):
    """Pick a series and episodes, and put them on the shared playlist.

    Four stages in a stack rather than one tall column: search, episodes, the
    chosen sources, and the full source list for one episode.
    """

    def __init__(self, parent):
        super().__init__(title="Browse", transient_for=parent, modal=False)
        self.main = parent
        self.cfg = parent.cfg
        self.set_default_size(820, 760)

        self.series = []
        self.episodes = []
        self.season = 0
        self.picks = []        # [{"episode":…, "source":…, "sources":[…]}, …]
        self.editing = None    # index into picks while the sources page is up
        self.busy = False
        # Where Resume asked us to land: the season, and the episode already
        # watched, so selection falls on the one after it.
        self.resume_season = 0
        self.resume_after = 0
        # Set by the Refresh control: bypass the cache for the next lookup, so
        # a stale source list is never a dead end.
        self.refresh = False

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, "set_margin_" + side)(16)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_vexpand(True)
        self.stack.add_named(self._page_search(), "search")
        self.stack.add_named(self._page_episodes(), "episodes")
        self.stack.add_named(self._page_review(), "review")
        self.stack.add_named(self._page_sources(), "sources")
        box.append(self.stack)

        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_wrap(True)
        self.status.set_max_width_chars(76)
        self.status.add_css_class("dim")
        box.append(self.status)

        self.toaster = Adw.ToastOverlay()
        self.toaster.set_child(box)
        bar = Adw.ToolbarView()
        bar.add_top_bar(Adw.HeaderBar())
        bar.set_content(self.toaster)
        self.set_content(bar)

        self.stack.set_visible_child_name("search")
        self._fill_recent()

    # -- pages ------------------------------------------------------------ #

    def _page_search(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.search_entry = Gtk.Entry()
        self.search_entry.set_hexpand(True)
        self.search_entry.set_placeholder_text("Series name…")
        self.search_entry.connect("activate", self.on_search)
        bar.append(self.search_entry)
        btn = Gtk.Button(label="Search")
        btn.add_css_class("suggested-action")
        btn.connect("clicked", self.on_search)
        bar.append(btn)
        page.append(bar)

        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Search again, ignoring anything remembered")
        refresh.connect("clicked", self.on_search_fresh)
        bar.append(refresh)

        sw, self.series_list = scrolled_list()
        self.series_list.connect("row-activated", self.on_series_chosen)
        page.append(sw)

        self.recent_label = Gtk.Label(label="Recently watched", xalign=0)
        self.recent_label.add_css_class("section-title")
        page.append(self.recent_label)

        sw2, self.recent_list = scrolled_list(min_height=90, max_height=200)
        self.recent_list.connect("row-activated", self.on_recent_chosen)
        page.append(sw2)

        hint = Gtk.Label(
            label="Metadata comes from Cinemeta, the same catalogue Stremio uses, and "
                  "answers are remembered for a week. Nothing here touches your debrid "
                  "account — that starts when you look for sources.",
            xalign=0)
        hint.set_wrap(True)
        hint.set_max_width_chars(76)
        hint.add_css_class("dim")
        page.append(hint)
        return page

    def _fill_recent(self):
        clear_list(self.recent_list)
        entries = self.main.history.entries()
        self.recent_label.set_visible(bool(entries))
        self.recent_list.get_parent().set_visible(bool(entries))
        for entry in entries:
            season = int(entry.get("season") or 0)
            episode = int(entry.get("episode") or 0)
            text = entry.get("name") or entry.get("id")
            if season and episode:
                text += "  ·  next S%02dE%02d" % (season, episode + 1)
            self.recent_list.append(list_row(text, payload=entry, attr="entry"))

    def on_recent_chosen(self, _list, row):
        entry = getattr(row, "entry", None)
        if entry is None:
            return
        self.open_series(Series(entry.get("id", ""), entry.get("name") or "",
                                entry.get("year") or ""),
                         season=int(entry.get("season") or 0),
                         after=int(entry.get("episode") or 0))

    def _page_episodes(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.series_label = Gtk.Label(label="", xalign=0)
        self.series_label.add_css_class("section-title")
        page.append(self.series_label)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        split.set_vexpand(True)
        sw_s, self.season_list = scrolled_list(min_height=220)
        sw_s.set_size_request(150, -1)
        sw_s.set_hexpand(False)
        self.season_list.connect("row-selected", self.on_season_selected)
        split.append(sw_s)

        sw_e, self.episode_list = scrolled_list(Gtk.SelectionMode.NONE,
                                                min_height=220, max_height=420)
        self.episode_list.connect("row-activated", toggle_row)
        split.append(sw_e)
        page.append(split)

        hint = Gtk.Label(label="Click an episode to tick it, click again to untick. "
                               "Every ticked one gets queued.",
                         xalign=0)
        hint.set_wrap(True)
        hint.add_css_class("dim")
        page.append(hint)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda _b: self.stack.set_visible_child_name("search"))
        bar.append(back)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        self.btn_find = Gtk.Button(label="Find sources")
        self.btn_find.add_css_class("suggested-action")
        self.btn_find.connect("clicked", self.on_find_sources)
        bar.append(self.btn_find)
        page.append(bar)
        return page

    def _page_review(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        lbl = Gtk.Label(label="These are the picks. Change any of them before adding.",
                        xalign=0)
        lbl.set_wrap(True)
        lbl.add_css_class("dim")
        page.append(lbl)

        sw, self.review_list = scrolled_list(Gtk.SelectionMode.NONE,
                                             min_height=260, max_height=440)
        page.append(sw)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda _b: self.stack.set_visible_child_name("episodes"))
        bar.append(back)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)
        self.btn_add = Gtk.Button(label="Add to playlist")
        self.btn_add.add_css_class("suggested-action")
        self.btn_add.connect("clicked", self.on_add)
        bar.append(self.btn_add)
        page.append(bar)
        return page

    def _page_sources(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)

        self.sources_label = Gtk.Label(label="", xalign=0)
        self.sources_label.add_css_class("section-title")
        page.append(self.sources_label)

        sw, self.source_list = scrolled_list(min_height=300, max_height=460)
        self.source_list.connect("row-activated", self.on_source_chosen)
        page.append(sw)

        hint = Gtk.Label(
            label="“ready” means the file is already on the debrid server and starts "
                  "at once. “needs download” means picking it makes the server fetch "
                  "the torrent first, which can take a while.",
            xalign=0)
        hint.set_wrap(True)
        hint.set_max_width_chars(76)
        hint.add_css_class("dim")
        page.append(hint)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        back = Gtk.Button(label="Back")
        back.connect("clicked", lambda _b: self.stack.set_visible_child_name("review"))
        bar.append(back)
        page.append(bar)
        return page

    # -- helpers ---------------------------------------------------------- #

    def say(self, text):
        def apply():
            self.status.set_text(text)
            return False
        GLib.idle_add(apply)

    def set_busy(self, busy):
        self.busy = busy
        for btn in (self.btn_find, self.btn_add):
            btn.set_sensitive(not busy)

    def _key(self):
        return str(self.cfg["rd_api_key"] or "").strip()

    def _log(self, text):
        """Main-window log, with the debrid key removed on the way out."""
        self.main.log(redact(text, self._key()))

    def _socks(self):
        """The tunnel's port when it is actually up, else None for a direct route."""
        if self.main.session.tunnel_alive():
            return int(self.cfg["socks_port"])
        return None

    # -- search ----------------------------------------------------------- #

    def on_search(self, _w):
        query = self.search_entry.get_text().strip()
        if not query:
            self.say("Type a series name first.")
            return
        self.say("Searching…")
        threading.Thread(target=self._search_worker, args=(query,), daemon=True).start()

    def on_search_fresh(self, _btn):
        """Search past whatever was remembered."""
        self.refresh = True
        self.on_search(None)

    def _search_worker(self, query):
        found, err = cinemeta_search(query, socks_port=self._socks(),
                                     cache=self.main.cache, refresh=self.refresh)

        def apply():
            clear_list(self.series_list)
            self.series = found
            if err:
                self.status.set_text("Search failed: %s" % err)
            elif not found:
                self.status.set_text("Nothing matched “%s”." % query)
            else:
                self.status.set_text("%d result%s." % (len(found), "" if len(found) == 1 else "s"))
            for s in found:
                self.series_list.append(list_row(s.label(), payload=s, attr="series"))
            return False

        GLib.idle_add(apply)

    def on_series_chosen(self, _list, row):
        series = getattr(row, "series", None)
        if series is not None:
            self.open_series(series)

    def open_series(self, series, season=0, after=0):
        """Show a series. `season`/`after` land on the episode following one
        already watched, which is what Resume means."""
        self.chosen_series = series
        self.resume_season = int(season or 0)
        self.resume_after = int(after or 0)
        self.series_label.set_text(series.label())
        clear_list(self.season_list)
        clear_list(self.episode_list)
        self.stack.set_visible_child_name("episodes")
        self.say("Loading episodes…")
        threading.Thread(target=self._episodes_worker, args=(series,), daemon=True).start()

    def _episodes_worker(self, series):
        eps, err = cinemeta_episodes(series.id, socks_port=self._socks(),
                                     cache=self.main.cache, refresh=self.refresh)

        def apply():
            self.episodes = eps
            clear_list(self.season_list)
            if err or not eps:
                self.status.set_text("Could not load episodes: %s" % (err or "none listed"))
                return False
            seasons = sorted({e.season for e in eps})
            want = self.resume_season
            if want not in seasons:
                want = seasons[0]
            chosen = None
            for s in seasons:
                row = list_row("Season %d" % s, payload=s, attr="season")
                self.season_list.append(row)
                if s == want:
                    chosen = row
            self.season_list.select_row(chosen or self.season_list.get_row_at_index(0))
            self.status.set_text("%d episodes across %d seasons." % (len(eps), len(seasons)))
            return False

        GLib.idle_add(apply)

    def on_season_selected(self, _list, row):
        season = getattr(row, "season", None) if row is not None else None
        if season is None:
            return
        self.season = season
        clear_list(self.episode_list)
        resume_here = season == self.resume_season and self.resume_after > 0
        pick = None
        for ep in [e for e in self.episodes if e.season == season]:
            r = check_row(ep.label(), payload=ep, attr="episode")
            self.episode_list.append(r)
            # Land on the one after wherever the last session stopped.
            if resume_here and ep.number == self.resume_after + 1:
                pick = r
        if pick is not None:
            pick.check.set_active(True)

    # -- sources ---------------------------------------------------------- #

    def on_find_sources(self, _btn):
        rows = checked_rows(self.episode_list)
        chosen = [getattr(r, "episode", None) for r in rows]
        chosen = [e for e in chosen if e is not None]
        if not chosen:
            self.say("Tick at least one episode.")
            return
        if not self._key():
            self.say("No Real-Debrid key set — put one in Advanced first.")
            self._log("Browse: no debrid key configured, so no sources can be looked up.")
            return
        chosen.sort(key=lambda e: (e.season, e.number))
        self.set_busy(True)
        self.say("Looking up sources for %d episode%s…"
                 % (len(chosen), "" if len(chosen) == 1 else "s"))
        threading.Thread(target=self._sources_worker, args=(chosen,), daemon=True).start()

    def _sources_worker(self, episodes):
        socks = self._socks()
        picks = []
        shape_logged = False
        fell_back = False
        for i, ep in enumerate(episodes, 1):
            self.say("Looking up %s (%d of %d)…" % (ep.code(), i, len(episodes)))
            sources, err = [], ""
            series_name = getattr(self.chosen_series, "name", "")

            # A copy already on the debrid account needs no lookup and no
            # resolving, so it is both the fastest answer and the one that still
            # works when the addon is unreachable. Worth asking first.
            if self.cfg["prefer_rd_cache"]:
                sources, rderr = rd_fallback_sources(
                    self.cfg, ep, series_name, socks_port=socks, log=self._log)
                if sources:
                    fell_back = True
                elif rderr:
                    self._log("Real-Debrid lookup failed for %s: %s" % (ep.code(), rderr))

            if not sources:
                sources, err = torrentio_sources(self.cfg, ep.stream_id(), socks_port=socks,
                                                 cache=self.main.cache, refresh=self.refresh)
                if err:
                    self._log("Torrentio lookup failed for %s: %s" % (ep.code(), err))
            if not sources:
                # The source addon is unreachable or knows nothing. The episode
                # may still be sitting on the debrid account from last time, and
                # that copy needs no torrent index at all.
                self.say("Torrentio gave nothing for %s — checking what is already "
                         "on your debrid account…" % ep.code())
                sources, rderr = rd_fallback_sources(
                    self.cfg, ep, series_name, socks_port=socks, log=self._log)
                if rderr:
                    self._log("Real-Debrid fallback failed for %s: %s" % (ep.code(), rderr))
                elif sources:
                    fell_back = True
            if sources and not shape_logged:
                # The debrid key changes Torrentio's reply shape. Say which one
                # arrived rather than assuming it.
                shape_logged = True
                marked = sum(1 for s in sources if s.cached is True)
                self._log("Torrentio replied with %s links; %d of %d marked ready."
                          % ("resolvable" if sources[0].url else "info-hash only",
                             marked, len(sources)))
            picks.append({"episode": ep, "sources": sources,
                          "source": pick_source(sources, self.cfg["preferred_quality"])})

        def apply():
            self.picks = picks
            self._refresh_review()
            self.stack.set_visible_child_name("review")
            missing = sum(1 for p in picks if p["source"] is None)
            uncached = sum(1 for p in picks if p["source"] is not None
                           and p["source"].cached is False)
            bits = []
            if missing:
                bits.append("%d with no source at all" % missing)
            if uncached:
                bits.append("%d not on the debrid server yet" % uncached)
            summary = "; ".join(bits) if bits else \
                "Every episode has a source that is ready to play."
            if fell_back:
                summary += "  Some came straight from your debrid account."
            waiting = torrentio_is_down()
            if waiting:
                summary += ("  Torrentio is not answering; it will be tried again "
                            "in %ds." % int(waiting))
            self.status.set_text(summary)
            self.set_busy(False)
            return False

        GLib.idle_add(apply)

    def _refresh_review(self):
        clear_list(self.review_list)
        for i, pick in enumerate(self.picks):
            ep, src = pick["episode"], pick["source"]
            row = Gtk.ListBoxRow()
            row.set_activatable(False)
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            line.set_margin_top(8)
            line.set_margin_bottom(8)
            line.set_margin_start(10)
            line.set_margin_end(10)

            text = "%s\n%s" % (ep.label(), src.label() if src else "no source found")
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.set_wrap(True)
            lbl.set_hexpand(True)
            lbl.set_max_width_chars(60)
            if src is None:
                lbl.add_css_class("result-fail")
            elif src.cached is False:
                lbl.add_css_class("result-warn")
            line.append(lbl)

            btn = Gtk.Button(label="Change…")
            btn.set_valign(Gtk.Align.CENTER)
            btn.connect("clicked", self.on_change, i)
            btn.set_sensitive(bool(pick["sources"]))
            line.append(btn)

            row.set_child(line)
            self.review_list.append(row)

    def on_change(self, _btn, index):
        self.editing = index
        pick = self.picks[index]
        self.sources_label.set_text("%s — %d sources"
                                    % (pick["episode"].label(), len(pick["sources"])))
        clear_list(self.source_list)
        for src in pick["sources"]:
            row = list_row("%s\n%s" % (src.label(), src.filename or ""),
                           dim=(src.cached is False), payload=src, attr="source")
            self.source_list.append(row)
        self.stack.set_visible_child_name("sources")

    def on_source_chosen(self, _list, row):
        src = getattr(row, "source", None)
        if src is None or self.editing is None:
            return
        self.picks[self.editing]["source"] = src
        self.editing = None
        self._refresh_review()
        self.stack.set_visible_child_name("review")

    # -- handing the queue over ------------------------------------------- #

    def on_add(self, _btn):
        usable = [p for p in self.picks if p["source"] is not None]
        if not usable:
            self.say("Nothing to add — none of these episodes has a source.")
            return
        # In host mode there is no tunnel and none is wanted: this machine is
        # the exit point, so a link resolved straight from here is already
        # coming from the right address. Requiring a tunnel here is what stopped
        # the host queueing anything at all.
        if self.cfg["role"] != "host" and not self.main.session.tunnel_alive():
            if self.cfg["require_verified"]:
                self.say("The tunnel is not up. Run “Check the route” first so the "
                         "links are made from the host's address, not this machine's.")
                return
            self._log("Browse: resolving debrid links without the tunnel, so they are "
                      "tied to this machine's address rather than the host's.")
        self.set_busy(True)
        self.say("Resolving %d link%s…" % (len(usable), "" if len(usable) == 1 else "s"))
        threading.Thread(target=self._resolve_worker, args=(usable,), daemon=True).start()

    def _resolve_worker(self, picks):
        """Resolve each pick to its final link.

        Resolving here, once, is the point: the resolved link is what both
        watchers get, so the debrid server sees one link fetched from one
        address instead of each side resolving its own.
        """
        # Strictly one at a time. Resolving three at once was measured against
        # the real service and every one of them timed out past 180s, while the
        # same source resolved alone in about 80s — the resolver serialises per
        # account, so overlapping the requests only starves all of them.
        socks = self._socks()
        urls, failed = [], []
        for i, pick in enumerate(picks, 1):
            ep, src = pick["episode"], pick["source"]
            self.say("Resolving %s (%d of %d) — this takes up to a couple of minutes…"
                     % (ep.code(), i, len(picks)))
            if not src.url:
                failed.append("%s (no resolvable link — is the debrid key set?)" % ep.code())
                continue
            if src.direct:
                # Came straight from the debrid account, so it is already the
                # final link -- resolving it again would only cost a round trip.
                urls.append((ep, src.url))
                continue
            final, err = curl_final_url(src.url, socks_port=socks)
            if not final:
                failed.append("%s (%s)" % (ep.code(), redact(err, self._key())))
                continue
            urls.append((ep, final))

        def apply():
            self.set_busy(False)
            for note in failed:
                self._log("Could not resolve " + note)
            if not urls:
                self.status.set_text("Nothing resolved. See the activity log.")
                return False
            self.main.adopt_queue([u for _e, u in urls])
            last = urls[-1][0]
            self.main.history.remember(self.chosen_series.id, self.chosen_series.name,
                                       self.chosen_series.year, last.season, last.number)
            self.main.refresh_history()
            self.main.push_history()
            self.cfg["library_series_id"] = self.chosen_series.id
            self.cfg["library_series_name"] = self.chosen_series.name
            self.cfg["library_season"] = last.season
            self.cfg["library_episode"] = last.number
            self.cfg.save()
            self._log("Queued %d episode%s: %s."
                      % (len(urls), "" if len(urls) == 1 else "s",
                         ", ".join(e.code() for e, _u in urls)))
            if failed:
                self._log("%d episode%s could not be resolved and were left out."
                          % (len(failed), "" if len(failed) == 1 else "s"))
            self.close()
            return False

        GLib.idle_add(apply)
