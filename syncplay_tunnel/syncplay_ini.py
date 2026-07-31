"""Editing Syncplay's own configuration file.

Three traps, all handled: Syncplay escapes % as %%, so RawConfigParser is
mandatory; the file is UTF-8 with a BOM; and trustedDomains is a Python list
literal read back with ast.literal_eval.
"""
import ast
import configparser
import os
from pathlib import Path
from urllib.parse import urlsplit

from .constants import SYNCPLAY_SECTION

# Syncplay shows its setup dialog when forceGuiPrompt is True *or* when no file
# was given on the command line (ConfigurationGetter.py, "if
# (self._config['forceGuiPrompt'] == "True" or not self._config['file'])").
# Both have to be handled for playback to start on its own, and the trusted
# domain list decides whether a URL switches without a confirmation.
# --------------------------------------------------------------------------- #

SYNCPLAY_SECTION = "client_settings"


def syncplay_ini_path():
    """Same search order Syncplay uses: ~/.syncplay first, then XDG."""
    legacy = Path.home() / ".syncplay"
    if legacy.is_file():
        return legacy
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return xdg / "syncplay.ini"


def prepare_syncplay_ini(urls, trust_domain, log=None, path=None):
    """Turn off Syncplay's setup dialog, and optionally trust the URLs' domains.

    Takes one URL or several: a queued season can span more than one debrid
    hostname, and an untrusted one costs a confirmation prompt mid-episode.

    Values are read and written raw: Syncplay escapes % as %% and configparser's
    default interpolation would blow up on it. The file is utf-8 with a BOM,
    which is what Syncplay itself writes.
    """
    if isinstance(urls, str):
        urls = [urls]
    urls = [u for u in (urls or []) if u]

    def say(msg):
        if log:
            log(msg)

    ini = Path(path) if path else syncplay_ini_path()
    parser = configparser.RawConfigParser(strict=False)
    parser.optionxform = str
    if ini.exists():
        try:
            with ini.open("r", encoding="utf-8-sig") as fh:
                parser.read_file(fh)
        except Exception as exc:
            say("Could not read %s: %s" % (ini, exc))
            return False
    if not parser.has_section(SYNCPLAY_SECTION):
        parser.add_section(SYNCPLAY_SECTION)

    changed = []
    if parser.get(SYNCPLAY_SECTION, "forceguiprompt", fallback="True") != "False":
        parser.set(SYNCPLAY_SECTION, "forceguiprompt", "False")
        changed.append("forceguiprompt = False")

    hosts = []
    for u in urls:
        h = urlsplit(u).hostname
        if h and h not in hosts:
            hosts.append(h)
    if trust_domain and hosts:
        raw = parser.get(SYNCPLAY_SECTION, "trusteddomains", fallback="[]")
        try:
            domains = ast.literal_eval(raw)
            if not isinstance(domains, list):
                domains = []
        except Exception:
            domains = []
        added = [h for h in hosts if h not in domains]
        if added:
            domains.extend(added)
            parser.set(SYNCPLAY_SECTION, "trusteddomains", repr(domains))
            changed.append("trusteddomains += %s" % ", ".join(added))

    if not changed:
        say("Syncplay's config already lets playback start on its own.")
        return True

    try:
        ini.parent.mkdir(parents=True, exist_ok=True)
        tmp = ini.with_suffix(ini.suffix + ".syncplay-tunnel.tmp")
        with tmp.open("w", encoding="utf-8-sig") as fh:
            parser.write(fh)
        tmp.replace(ini)
    except OSError as exc:
        say("Could not write %s: %s" % (ini, exc))
        return False
    say("Syncplay config (%s): %s" % (ini, "; ".join(changed)))
    return True
