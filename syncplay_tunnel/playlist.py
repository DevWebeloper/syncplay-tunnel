"""Setting the room's shared playlist over Syncplay's own protocol.

Syncplay's command line takes exactly one file, so a queue cannot be handed over
at launch. The playlist is room state on the server, so this joins the room, sets
it, and leaves -- after the real client is in, because the server discards the
playlist of a room that empties.
"""
import json
import socket
import ssl
import time

from .constants import (PLAYLIST_MAX_CHARACTERS, PLAYLIST_MAX_ITEMS,
                        SYNCPLAY_DEFAULT_PORT, SYNCPLAY_PROTOCOL_VERSION)


class SyncplayPush:
    """A short-lived Syncplay client that sets the room's shared playlist."""

    def __init__(self, server, room, username, log=None):
        host, _, port = str(server or "").strip().rpartition(":")
        if not host:
            host, port = port, ""
        self.host = host
        self.port = int(port) if port.isdigit() else SYNCPLAY_DEFAULT_PORT
        self.room = room
        self.username = username
        self._log = log

    def say(self, msg):
        if self._log:
            self._log(msg)

    @staticmethod
    def _send(fh, obj):
        fh.write((json.dumps(obj) + "\r\n").encode("utf-8"))
        fh.flush()

    @staticmethod
    def _read(fh):
        line = fh.readline()
        if not line:
            return None
        try:
            return json.loads(line.decode("utf-8", "replace").strip() or "{}")
        except ValueError:
            return {}

    def _connect(self, timeout):
        """Open the connection, doing Syncplay's startTLS negotiation first."""
        sock = socket.create_connection((self.host, self.port), timeout=timeout)
        sock.settimeout(timeout)
        fh = sock.makefile("rwb")
        self._send(fh, {"TLS": {"startTLS": "send"}})
        reply = self._read(fh) or {}
        mode = str(((reply.get("TLS") or {}).get("startTLS") or "false")).lower()
        if mode == "true":
            # Verification is left on. A server that cannot be verified is
            # exactly the warning the user should be seeing, not one to skip.
            ctx = ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
            sock.settimeout(timeout)
            fh = sock.makefile("rwb")
        return sock, fh

    def _hello(self, fh):
        self._send(fh, {"Hello": {
            "username": self.username,
            "room": {"name": self.room},
            "version": SYNCPLAY_PROTOCOL_VERSION,
            "realversion": SYNCPLAY_PROTOCOL_VERSION,
            "features": {"sharedPlaylists": True, "chat": True,
                         "featureList": True, "readiness": True,
                         "managedRooms": True},
        }})

    def _pong(self, fh, msg):
        """Answer the server's ping so it does not treat us as a dead client."""
        ping = (msg.get("State") or {}).get("ping") or {}
        self._send(fh, {"State": {
            "ping": {
                "latencyCalculation": ping.get("latencyCalculation"),
                "clientLatencyCalculation": time.time(),
            },
            "playstate": {"position": 0.0, "paused": True, "doSeek": False},
        }})

    def _others(self, msg):
        """Names in our room that are not us, from either message that carries
        them: the List reply, and the Set/user event when somebody joins."""
        seen = set()
        listing = msg.get("List")
        if isinstance(listing, dict):
            for room, users in listing.items():
                if room == self.room and isinstance(users, dict):
                    seen.update(users)
        users = (msg.get("Set") or {}).get("user")
        if isinstance(users, dict):
            for name, info in users.items():
                room = ((info or {}).get("room") or {}).get("name")
                if room in (None, self.room):
                    seen.add(name)
        seen.discard(self.username)
        return seen

    def push(self, files, index=0, wait=45.0, timeout=15.0):
        """Set the room playlist once someone is there to keep it. (ok, message)."""
        files = [f for f in (files or []) if f]
        if not files:
            return False, "Nothing to queue."
        if len(files) > PLAYLIST_MAX_ITEMS:
            return False, ("Syncplay caps a playlist at %d items and this is %d."
                           % (PLAYLIST_MAX_ITEMS, len(files)))
        total = sum(len(f) for f in files)
        if total > PLAYLIST_MAX_CHARACTERS:
            return False, ("Syncplay caps a playlist at %d characters and these %d "
                           "links come to %d. Queue fewer episodes."
                           % (PLAYLIST_MAX_CHARACTERS, len(files), total))
        if not self.host:
            return False, "No Syncplay server is set, so the queue has nowhere to go."

        sock = fh = None
        try:
            sock, fh = self._connect(timeout)
            self._hello(fh)

            # Short reads so the roster can be re-requested while waiting.
            sock.settimeout(2.0)
            deadline = time.time() + wait
            others = set()
            next_ask = 0.0
            while time.time() < deadline and not others:
                # The roster has to be asked for. The server volunteers a
                # Set/user frame when somebody joins *after* us, but says
                # nothing about whoever is already sitting in the room — so a
                # client that only listens never learns it has company.
                if time.time() >= next_ask:
                    self._send(fh, {"List": None})
                    next_ask = time.time() + 3.0
                try:
                    msg = self._read(fh)
                except (socket.timeout, TimeoutError):
                    continue
                if msg is None:
                    return False, "The Syncplay server closed the connection."
                if "State" in msg:
                    self._pong(fh, msg)
                others = self._others(msg)
            sock.settimeout(timeout)

            if not others:
                return False, ("Nobody joined room '%s' within %ds, and a playlist set "
                               "in an empty room is discarded by the server."
                               % (self.room, int(wait)))

            self._send(fh, {"Set": {"playlistChange": {"files": files}}})
            self._send(fh, {"Set": {"playlistIndex": {"index": index}}})
            # Let the server broadcast before the socket goes away.
            time.sleep(1.0)
            return True, ("Queued %d episode%s for %s."
                          % (len(files), "" if len(files) == 1 else "s",
                             ", ".join(sorted(others))))
        except ssl.SSLError as exc:
            return False, "Syncplay server's TLS could not be verified: %s" % exc
        except OSError as exc:
            return False, "Could not reach the Syncplay server: %s" % exc
        finally:
            for closer in (fh, sock):
                try:
                    if closer is not None:
                        closer.close()
                except Exception:
                    pass
