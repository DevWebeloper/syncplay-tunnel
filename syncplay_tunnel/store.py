"""The three JSON files the app keeps: settings, cached lookups, watch history.

All three are written atomically through a sibling .tmp and left owner-only,
because two of them hold things worth protecting.
"""
import copy
import json
import threading
import time
from pathlib import Path

from .constants import (CACHE_FILE, CACHE_MAX_ENTRIES, CACHE_TTL, CONFIG_DIR,
                        CONFIG_FILE, DEFAULTS, HISTORY_FILE, HISTORY_MAX)


class Config(dict):
    """The settings, as a plain dict seeded from DEFAULTS.

    The path is an argument rather than a module global so a caller can point it
    somewhere else — which is the only way to redirect it now that each module
    holds its own reference to the constants.
    """

    def __init__(self, path=None):
        super().__init__(DEFAULTS)
        self.path = Path(path) if path else CONFIG_FILE
        self.load()

    def load(self):
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text())
                for k, v in data.items():
                    if k in DEFAULTS:
                        self[k] = v
        except Exception:
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(dict(self), indent=2) + "\n")
        tmp.replace(self.path)
        self.path.chmod(0o600)


class JsonStore:
    """Shared plumbing for the two small JSON files beside the config.

    Same atomic write as Config -- write a sibling .tmp, then replace -- so a
    crash mid-write cannot leave a half-parsed file behind, and the same 0600
    mode, because both of these can end up holding things worth protecting.
    """

    def __init__(self, path, empty):
        self.path = Path(path)
        self._empty = empty
        self._lock = threading.Lock()
        self.data = copy.deepcopy(empty)
        self.load()

    def load(self):
        try:
            if self.path.exists():
                loaded = json.loads(self.path.read_text())
                if isinstance(loaded, type(self._empty)):
                    self.data = loaded
        except Exception:
            # A corrupt cache or history is not worth a failed start; the file
            # is rewritten on the next save.
            self.data = copy.deepcopy(self._empty)

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data) + "\n")
            tmp.replace(self.path)
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self):
        with self._lock:
            self.data = copy.deepcopy(self._empty)
        self.save()


class Cache(JsonStore):
    """Answers from Cinemeta and Torrentio, kept for a while.

    Those services change slowly, and re-fetching a series you opened a minute
    ago is pure waiting. Resolved debrid links are deliberately NOT stored --
    they expire, and a stale one fails in the middle of an episode.
    """

    def __init__(self, path=None):
        super().__init__(path or CACHE_FILE, {})

    def get(self, namespace, key, now=None):
        ttl = CACHE_TTL.get(namespace, 3600)
        now = time.time() if now is None else now
        with self._lock:
            entry = self.data.get("%s:%s" % (namespace, key))
            if not isinstance(entry, dict):
                return None
            if now - entry.get("at", 0) > ttl:
                return None
            return entry.get("data")

    def put(self, namespace, key, value, now=None):
        now = time.time() if now is None else now
        with self._lock:
            self.data["%s:%s" % (namespace, key)] = {"at": now, "data": value}
            if len(self.data) > CACHE_MAX_ENTRIES:
                # Oldest first, so a long browsing session cannot grow the file
                # without bound.
                for stale in sorted(self.data,
                                    key=lambda k: self.data[k].get("at", 0)
                                    )[:len(self.data) - CACHE_MAX_ENTRIES]:
                    self.data.pop(stale, None)
        self.save()

    def count(self):
        with self._lock:
            return len(self.data)


class History(JsonStore):
    """What was watched, most recent first."""

    def __init__(self, path=None):
        super().__init__(path or HISTORY_FILE, [])

    def remember(self, series_id, name, year="", season=0, episode=0, now=None):
        """Record a series, moving it to the front and folding in a re-watch."""
        if not series_id:
            return
        now = time.time() if now is None else now
        with self._lock:
            kept = [e for e in self.data if e.get("id") != series_id]
            kept.insert(0, {"id": series_id, "name": name, "year": year,
                            "season": int(season or 0), "episode": int(episode or 0),
                            "at": now})
            self.data = kept[:HISTORY_MAX]
        self.save()

    def forget(self, series_id):
        with self._lock:
            self.data = [e for e in self.data if e.get("id") != series_id]
        self.save()

    def entries(self):
        with self._lock:
            return [dict(e) for e in self.data if e.get("id")]

    def merge(self, incoming):
        """Fold another machine's history into this one. Returns entries added.

        Both ends may have watched something since the last sync, so neither
        copy wins outright: entries are matched on series id and the newer
        timestamp keeps its season and episode.
        """
        added = 0
        with self._lock:
            mine = {e.get("id"): e for e in self.data if e.get("id")}
            for entry in incoming or []:
                sid = entry.get("id")
                if not sid:
                    continue
                current = mine.get(sid)
                if current is None:
                    mine[sid] = dict(entry)
                    added += 1
                elif float(entry.get("at") or 0) > float(current.get("at") or 0):
                    mine[sid] = dict(entry)
            merged = sorted(mine.values(),
                            key=lambda e: float(e.get("at") or 0), reverse=True)
            self.data = merged[:HISTORY_MAX]
        self.save()
        return added
