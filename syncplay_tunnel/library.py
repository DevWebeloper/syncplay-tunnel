"""Stremio's addons and the debrid account, spoken directly.

Cinemeta is the metadata catalogue, Torrentio finds sources, and when Torrentio
cannot be reached the debrid account itself is asked what it already holds.

Torrentio takes the debrid key inside its URL path, so nothing built here is
ever logged or cached raw.
"""
import contextlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

from .constants import (CINEMETA, KEY_TOKEN, RD_API, TORRENTIO,
                        TORRENTIO_COOLDOWN)
from .util import _scrubbed_env, curl_json, redact, run

# The manual loop this replaces was: open Stremio, let the Torrentio addon put
# the episode on the debrid server, copy the link out, unrestrict it, paste it
# into Syncplay's playlist. Stremio does nothing there that this app cannot —
# Cinemeta is a plain JSON catalogue and Torrentio answers a plain JSON query.
#
# Torrentio takes the debrid key inside its URL path, so no URL built here is
# ever logged or shown raw; everything user-visible goes through redact().
# --------------------------------------------------------------------------- #


class Series:
    def __init__(self, imdb_id, name, year=""):
        self.id = imdb_id
        self.name = name
        self.year = year

    def label(self):
        return "%s  ·  %s" % (self.name, self.year) if self.year else self.name

    def to_dict(self):
        return {"id": self.id, "name": self.name, "year": self.year}

    @staticmethod
    def from_dict(d):
        return Series(d.get("id", ""), d.get("name", ""), d.get("year", ""))


class Episode:
    def __init__(self, series_id, season, number, name="", released=""):
        self.series_id = series_id
        self.season = season
        self.number = number
        self.name = name
        self.released = released

    def code(self):
        return "S%02dE%02d" % (self.season, self.number)

    def stream_id(self):
        """Stremio addressing: <imdb id>:<season>:<episode>."""
        return "%s:%d:%d" % (self.series_id, self.season, self.number)

    def label(self):
        return "%s  ·  %s" % (self.code(), self.name) if self.name else self.code()

    def to_dict(self):
        return {"series_id": self.series_id, "season": self.season,
                "number": self.number, "name": self.name, "released": self.released}

    @staticmethod
    def from_dict(d):
        return Episode(d.get("series_id", ""), int(d.get("season", 0)),
                       int(d.get("number", 0)), d.get("name", ""), d.get("released", ""))


class Source:
    """One Torrentio result, parsed out of its two display strings."""

    def __init__(self, quality="", size="", seeders=0, provider="",
                 filename="", cached=None, url="", infohash="", direct=False):
        self.quality = quality
        self.size = size
        self.seeders = seeders
        self.provider = provider
        self.filename = filename
        # True = already on the debrid server, False = picking it starts a
        # fresh download, None = Torrentio was queried without a debrid key.
        self.cached = cached
        self.url = url
        self.infohash = infohash
        # True when url is already the final link and needs no resolving --
        # what the debrid account hands back directly.
        self.direct = direct

    def state_text(self):
        if self.cached is True:
            return "ready"
        if self.cached is False:
            return "needs download"
        return "cache unknown"

    def label(self):
        bits = [self.quality or "?"]
        if self.size:
            bits.append(self.size)
        if self.seeders:
            bits.append("%d seeders" % self.seeders)
        if self.provider:
            bits.append(self.provider)
        bits.append(self.state_text())
        return "  ·  ".join(bits)

    def to_dict(self, key=""):
        """Serialise for the cache, with the debrid key swapped for a token."""
        url = self.url
        if key and len(key) >= 8:
            url = url.replace(key, KEY_TOKEN)
        return {"quality": self.quality, "size": self.size, "seeders": self.seeders,
                "provider": self.provider, "filename": self.filename,
                "cached": self.cached, "url": url, "infohash": self.infohash,
                "direct": self.direct}

    @staticmethod
    def from_dict(d, key=""):
        url = d.get("url", "")
        if key and KEY_TOKEN in url:
            url = url.replace(KEY_TOKEN, key)
        return Source(quality=d.get("quality", ""), size=d.get("size", ""),
                      seeders=int(d.get("seeders", 0)), provider=d.get("provider", ""),
                      filename=d.get("filename", ""), cached=d.get("cached"),
                      url=url, infohash=d.get("infohash", ""),
                      direct=bool(d.get("direct")))


_SEEDERS_RE = re.compile(r"👤\s*(\d+)")
_SIZE_RE = re.compile(r"💾\s*([\d.]+\s*[KMGT]i?B)")
_PROVIDER_RE = re.compile(r"⚙️\s*(\S+)")


def _cache_state(name):
    """Read Torrentio's debrid marker out of a stream's display name.

    '[RD+]' means the file is already on the debrid server; '[RD download]'
    means picking it starts one. No marker at all means the query carried no
    key, so nothing can be said either way.
    """
    low = (name or "").lower()
    if "[rd+]" in low:
        return True
    if "[rd" in low:
        return False
    return None


def parse_source(raw):
    """Turn one Torrentio stream object into a Source.

    Shapes seen in the wild: without a debrid key a stream carries infoHash and
    fileIdx, with one it also carries a resolver `url`. Both are accepted, and
    which one arrived is worth logging the first time.
    """
    name = raw.get("name") or ""
    title = raw.get("title") or ""
    name_lines = name.splitlines()
    title_lines = title.splitlines()
    hints = raw.get("behaviorHints") or {}

    seeders = _SEEDERS_RE.search(title)
    size = _SIZE_RE.search(title)
    provider = _PROVIDER_RE.search(title)

    return Source(
        quality=name_lines[1].strip() if len(name_lines) > 1 else "",
        size=size.group(1).strip() if size else "",
        seeders=int(seeders.group(1)) if seeders else 0,
        provider=provider.group(1).strip() if provider else "",
        filename=hints.get("filename") or (title_lines[1].strip() if len(title_lines) > 1 else ""),
        cached=_cache_state(name),
        url=raw.get("url") or "",
        infohash=raw.get("infoHash") or "",
    )


@contextlib.contextmanager
def rd_auth_file(key):
    """A 0600 curl config carrying the debrid token.

    The token goes in a file rather than a -H argument because a command line is
    readable by every other process on the machine through ps.
    """
    fd, path = tempfile.mkstemp(prefix="syncplay-tunnel-", suffix=".conf")
    os.close(fd)
    try:
        os.chmod(path, 0o600)
        Path(path).write_text('header = "Authorization: Bearer %s"\n' % key)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def rd_call(cfg, path, socks_port=None, post=None, timeout=30):
    """One Real-Debrid API call. Returns (data, error)."""
    key = str(cfg["rd_api_key"] or "").strip()
    if not key:
        return None, "no Real-Debrid key set"
    with rd_auth_file(key) as auth:
        cmd = ["curl", "-s", "-L", "--max-time", str(timeout), "-K", auth,
               "-w", "\n%{http_code}"]
        if socks_port:
            cmd += ["--socks5-hostname", "127.0.0.1:%d" % socks_port]
        else:
            cmd += ["--noproxy", "*"]
        for field, value in (post or {}).items():
            cmd += ["--data-urlencode", "%s=%s" % (field, value)]
        rc, out, err = run(cmd + [RD_API + path], timeout=timeout + 5,
                           env=_scrubbed_env())
    if rc != 0:
        return None, err or "curl exited %s" % rc
    body, _, code = out.rpartition("\n")
    code = code.strip()
    if code == "401":
        return None, "Real-Debrid rejected the key"
    if code.isdigit() and not code.startswith("2"):
        return None, "HTTP %s" % code
    if not body.strip():
        return None, "empty reply"
    try:
        return json.loads(body), ""
    except ValueError:
        return None, "Real-Debrid did not answer with JSON"


_EPISODE_PATTERNS = (
    re.compile(r"[sS](\d{1,3})[\s._-]*[eE](\d{1,3})"),
    re.compile(r"(?<!\d)(\d{1,2})[xX](\d{2})(?!\d)"),
)


def _words(text):
    return [w for w in re.split(r"[^0-9a-z]+", (text or "").lower()) if w]


def file_is_episode(filename, season, number, series_name=""):
    """True when a filename looks like this exact episode of this series.

    Deliberately loose on the name and strict on the numbering: release names
    vary wildly, but SxxEyy is near-universal and is what actually decides
    whether the right thing plays.
    """
    if not filename:
        return False
    hit = False
    for pattern in _EPISODE_PATTERNS:
        for found in pattern.finditer(filename):
            if int(found.group(1)) == int(season) and int(found.group(2)) == int(number):
                hit = True
                break
        if hit:
            break
    if not hit:
        return False
    if not series_name:
        return True
    have = set(_words(filename))
    # Ignore a year in the title: releases put their own year in, and it is
    # often a different one.
    want = [w for w in _words(series_name) if not (len(w) == 4 and w.isdigit())]
    return all(w in have for w in want) if want else True


def rd_fallback_sources(cfg, episode, series_name="", socks_port=None, log=None):
    """Episodes already sitting in the debrid account. Returns (sources, error).

    Used when the source addon cannot be reached. If the file is already on the
    account there is no need for a torrent index at all.
    """
    def say(msg):
        if log:
            log(msg)

    found = []
    downloads, err = rd_call(cfg, "/downloads?limit=200", socks_port=socks_port)
    if downloads is None:
        return [], err
    for item in downloads if isinstance(downloads, list) else []:
        name = item.get("filename") or ""
        if not item.get("download"):
            continue
        if file_is_episode(name, episode.season, episode.number, series_name):
            found.append(Source(
                quality=guess_quality(name),
                size=human_size(item.get("filesize")),
                provider="already on Real-Debrid",
                filename=name, cached=True, url=item["download"], direct=True))
    if found:
        say("Found %d copy of %s already on the debrid account."
            % (len(found), episode.code()) if len(found) == 1 else
            "Found %d copies of %s already on the debrid account."
            % (len(found), episode.code()))
        return found, ""

    # Nothing unrestricted yet, but the torrent may still be there.
    torrents, err = rd_call(cfg, "/torrents?limit=200", socks_port=socks_port)
    if torrents is None:
        return [], err
    for torrent in torrents if isinstance(torrents, list) else []:
        if torrent.get("status") != "downloaded":
            continue
        title = torrent.get("filename") or ""
        # A season pack will not name the episode, so look inside anything whose
        # title matches the series at all.
        words = [w for w in _words(series_name) if not (len(w) == 4 and w.isdigit())]
        if series_name and not all(w in set(_words(title)) for w in words):
            continue
        info, ierr = rd_call(cfg, "/torrents/info/%s" % torrent.get("id"),
                             socks_port=socks_port)
        if info is None:
            continue
        selected = [f for f in (info.get("files") or []) if f.get("selected")]
        links = info.get("links") or []
        for index, entry in enumerate(selected):
            path = entry.get("path") or ""
            if index >= len(links):
                break
            if not file_is_episode(path, episode.season, episode.number, series_name):
                continue
            fresh, uerr = rd_call(cfg, "/unrestrict/link", socks_port=socks_port,
                                  post={"link": links[index]})
            if fresh and fresh.get("download"):
                found.append(Source(
                    quality=guess_quality(path),
                    size=human_size(entry.get("bytes")),
                    provider="already on Real-Debrid",
                    filename=path.lstrip("/"), cached=True,
                    url=fresh["download"], direct=True))
        if found:
            say("Recovered %s from a torrent already on the debrid account."
                % episode.code())
            return found, ""
    return [], ""


def guess_quality(name):
    for tag in ("2160p", "4k", "1080p", "720p", "480p"):
        if tag in (name or "").lower():
            return "4k" if tag in ("2160p", "4k") else tag
    return ""


def human_size(num):
    try:
        num = float(num or 0)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return "%.2f %s" % (num, unit) if unit not in ("B", "KB") else "%d %s" % (num, unit)
        num /= 1024
    return ""


def cinemeta_search(query, socks_port=None, cache=None, refresh=False):
    """Series matching a search term. Returns (series, error)."""
    query = query.strip()
    if cache is not None and not refresh:
        hit = cache.get("search", query.lower())
        if hit is not None:
            return [Series.from_dict(d) for d in hit], ""
    url = "%s/catalog/series/top/search=%s.json" % (CINEMETA, quote(query))
    data, err = curl_json(url, socks_port=socks_port)
    if data is None:
        return [], err
    found = []
    for meta in data.get("metas") or []:
        if meta.get("id"):
            found.append(Series(meta["id"], meta.get("name") or meta["id"],
                                meta.get("releaseInfo") or ""))
    if cache is not None and found:
        cache.put("search", query.lower(), [s.to_dict() for s in found])
    return found, ""


def cinemeta_episodes(imdb_id, socks_port=None, cache=None, refresh=False):
    """Every real episode of a series. Returns (episodes, error).

    Season 0 is where Cinemeta files specials and recaps. They are not part of
    a watch-through, so they are dropped.
    """
    if cache is not None and not refresh:
        hit = cache.get("episodes", imdb_id)
        if hit is not None:
            return [Episode.from_dict(d) for d in hit], ""
    url = "%s/meta/series/%s.json" % (CINEMETA, quote(imdb_id))
    data, err = curl_json(url, socks_port=socks_port, timeout=30)
    if data is None:
        return [], err
    out = []
    for vid in ((data.get("meta") or {}).get("videos") or []):
        season = vid.get("season")
        number = vid.get("episode", vid.get("number"))
        if not isinstance(season, int) or not isinstance(number, int) or season < 1:
            continue
        out.append(Episode(imdb_id, season, number, vid.get("name") or "",
                           (vid.get("released") or "")[:10]))
    out.sort(key=lambda e: (e.season, e.number))
    if cache is not None and out:
        cache.put("episodes", imdb_id, [e.to_dict() for e in out])
    return out, ""


def torrentio_url(cfg, stream_id):
    """Build the Torrentio query. The debrid key ends up in the path."""
    opts = [o.strip() for o in str(cfg["torrentio_opts"] or "").split("|") if o.strip()]
    key = str(cfg["rd_api_key"] or "").strip()
    if key:
        opts = [o for o in opts if not o.lower().startswith("realdebrid=")]
        opts.append("realdebrid=" + key)
    prefix = ("/" + quote("|".join(opts), safe="|=,")) if opts else ""
    return "%s%s/stream/series/%s.json" % (TORRENTIO, prefix, quote(stream_id))


# When the addon last failed. Asking again before this passes is just waiting.
_torrentio_down_until = 0.0


def torrentio_is_down():
    """Seconds left on the cooldown, 0 when the addon is worth asking."""
    return max(0.0, _torrentio_down_until - time.time())


def torrentio_note_failure():
    global _torrentio_down_until
    _torrentio_down_until = time.time() + TORRENTIO_COOLDOWN


def torrentio_note_success():
    global _torrentio_down_until
    _torrentio_down_until = 0.0


def torrentio_sources(cfg, stream_id, socks_port=None, cache=None, refresh=False):
    """Sources for one episode, best first. Returns (sources, error)."""
    key = str(cfg["rd_api_key"] or "").strip()
    # The cache key describes the query, so it must not be the key itself --
    # the options string is what changes the answer, minus the secret in it.
    ckey = "%s|%s" % (stream_id, redact(str(cfg["torrentio_opts"] or ""), key))
    if cache is not None and not refresh:
        hit = cache.get("sources", ckey)
        if hit is not None:
            return [Source.from_dict(d, key) for d in hit], ""
    waiting = torrentio_is_down()
    if waiting:
        # Do not spend another timeout per episode on a service that just failed.
        return [], "skipped — Torrentio failed recently, trying again in %ds" % int(waiting)
    data, err = curl_json(torrentio_url(cfg, stream_id), socks_port=socks_port,
                          timeout=40)
    if data is None:
        torrentio_note_failure()
        return [], err
    torrentio_note_success()
    found = [parse_source(s) for s in (data.get("streams") or [])]
    if cache is not None and found:
        cache.put("sources", ckey, [s.to_dict(key) for s in found])
    return found, ""


def pick_source(sources, preferred=""):
    """Best source: already cached wins, then the wanted quality, then seeders.

    Cache state outranks quality on purpose — a 4k source that is not on the
    debrid server yet is a wait, and the point of this is not waiting.
    """
    want = str(preferred or "").strip().lower()

    def rank(s):
        qual = (s.quality or "").strip().lower()
        cached = 0 if s.cached else (1 if s.cached is None else 2)
        # An exact match beats a partial one, so asking for 1080p does not land
        # on "1080p 3D SBS" while a plain 1080p release is sitting right there.
        if not want or qual == want:
            matches = 0
        elif want in qual:
            matches = 1
        else:
            matches = 2
        return (cached, matches, -s.seeders)

    ranked = sorted(sources, key=rank)
    return ranked[0] if ranked else None
