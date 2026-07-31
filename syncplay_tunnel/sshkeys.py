"""Enrolling this machine's key on the host, and narrowing what it may do.

An enrolled key is a full shell on the host unless it carries options, so the
app writes them on and can retrofit them onto keys enrolled earlier.
"""
import os
import pty
import re
import select
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .constants import KEY_OPTIONS
from .util import run, which

# ssh-copy-id has no way to take a password on stdin, and `sshpass -p` would put
# it in the process list where anyone on the box can read it. So we run the real
# command on a pseudo-terminal and answer its prompt directly — stdlib only, and
# the password never touches argv, the environment, or disk.
# --------------------------------------------------------------------------- #

KEY_CANDIDATES = ["id_ed25519", "id_ecdsa", "id_rsa"]


def find_ssh_key():
    ssh_dir = Path.home() / ".ssh"
    for name in KEY_CANDIDATES:
        pub = ssh_dir / (name + ".pub")
        if pub.exists():
            return pub
    return None


def ensure_ssh_key(log=None):
    """Return the public key path, generating an ed25519 key if there is none."""
    existing = find_ssh_key()
    if existing:
        return existing, None
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(mode=0o700, exist_ok=True)
    priv = ssh_dir / "id_ed25519"
    if log:
        log("No SSH key found — generating one.")
    rc, _, err = run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-f", str(priv),
         "-C", "syncplay-tunnel@%s" % socket.gethostname()],
        timeout=60,
    )
    if rc != 0:
        return None, err or "ssh-keygen failed"
    return priv.with_suffix(".pub"), None


# What our key is allowed to do on the host once it is installed.
#
# `restrict` turns everything off — pty, agent and X11 forwarding, user rc —
# and `port-forwarding` puts back the one thing the tunnel actually needs. A
# forced command= is deliberately NOT used: route check 2 runs `ssh host true`
# and check 7 runs `ssh host curl …` for ground truth, and a forced command
# would break the strongest half of the verification.
KEY_OPTIONS = "restrict,port-forwarding"

# Lines whose first field is a key type carry no options yet. Anything else
# already has an options field and is left exactly as the owner wrote it.
_RESTRICT_AWK = r'''
BEGIN { changed = 0 }
{
    if (index($0, blob) > 0 && $1 ~ /^(ssh-|ecdsa-|sk-)/) {
        print opts " " $0
        changed = 1
    } else {
        print $0
    }
}
END { exit changed ? 0 : 1 }
'''


def restrict_authorized_key(ssh_cmd, pub_path, log=None):
    """Prefix our key line on the host with KEY_OPTIONS.

    Runs after enrolment, over the key auth that was just proved to work, so no
    password is involved. Only the line holding our own key blob is touched, and
    only when it has no options field yet — so re-running is a no-op and nobody
    else's entry is rewritten.
    """
    def say(msg):
        if log:
            log(msg)

    try:
        parts = Path(pub_path).read_text().split()
    except OSError as exc:
        say("Could not read %s: %s" % (pub_path, exc))
        return False
    if len(parts) < 2:
        say("%s does not look like a public key." % pub_path)
        return False
    blob = parts[1]

    remote = (
        'f=~/.ssh/authorized_keys; '
        '[ -f "$f" ] || exit 3; '
        't=$(mktemp "$f.XXXXXX") || exit 4; '
        'awk -v blob=%s -v opts=%s %s "$f" > "$t"; rc=$?; '
        'if [ $rc -ne 0 ]; then rm -f "$t"; exit $rc; fi; '
        'chmod 600 "$t" && mv "$t" "$f"'
        % (shlex.quote(blob), shlex.quote(KEY_OPTIONS), shlex.quote(_RESTRICT_AWK))
    )

    # Wrapped in sh -c because the remote login shell is whatever the host user
    # picked — fish, for one, does not read this syntax.
    rc, _, err = run(list(ssh_cmd) + ["sh -c " + shlex.quote(remote)], timeout=30)
    if rc == 0:
        say("Key restricted on the host: %s. It can forward ports and run the "
            "route check, nothing else." % KEY_OPTIONS)
        return True
    if rc == 1:
        say("No key line needed changing on the host — it already carries its own "
            "options.")
        return True
    say("Could not restrict the key on the host (exit %s). It works, but it is a "
        "full shell login: %s" % (rc, err or "no error output"))
    return False


def key_line_is_open(line):
    """True when an authorized_keys line carries no options — a full shell."""
    line = line.strip()
    if not line or line.startswith("#"):
        return False
    return line.split()[0].startswith(("ssh-", "ecdsa-", "sk-"))


def restrict_local_keys(path=None, log=None):
    """Add KEY_OPTIONS to every unrestricted line in our own authorized_keys.

    For keys enrolled before this existed. Options are only ever added, never
    removed, no line is dropped, and the previous file is kept as .bak — a
    mistake here locks clients out, so it stays reversible.
    Returns (restricted, total).
    """
    def say(msg):
        if log:
            log(msg)

    keys = Path(path) if path else (Path.home() / ".ssh/authorized_keys")
    try:
        original = keys.read_text()
    except OSError as exc:
        say("No authorized_keys to change: %s" % exc)
        return 0, 0

    out, changed, total = [], 0, 0
    for line in original.splitlines():
        if line.strip() and not line.strip().startswith("#"):
            total += 1
        if key_line_is_open(line):
            out.append("%s %s" % (KEY_OPTIONS, line.strip()))
            changed += 1
        else:
            out.append(line)

    if not changed:
        say("Every authorised key already has options set — nothing to change.")
        return 0, total

    try:
        keys.with_suffix(keys.suffix + ".bak").write_text(original)
        tmp = keys.with_suffix(keys.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n")
        tmp.chmod(0o600)
        tmp.replace(keys)
    except OSError as exc:
        say("Could not rewrite %s: %s" % (keys, exc))
        return 0, total

    say("Restricted %d of %d authorised key%s to %s. Previous file kept as %s.bak."
        % (changed, total, "" if total == 1 else "s", KEY_OPTIONS, keys.name))
    return changed, total


PROMPT_PW = re.compile(r"(password|passcode|passphrase)\s*:", re.I)
PROMPT_YN = re.compile(r"\(yes/no", re.I)
DENIED = re.compile(r"permission denied|too many authentication failures", re.I)


def ssh_copy_id(user, host, port, password, log=None, timeout=90):
    """Install our public key on the host. Returns (ok, message).

    `password` is a bytearray so the caller can zero it afterwards.
    """
    import pty

    pub, err = ensure_ssh_key(log=log)
    if pub is None:
        return False, "Could not create an SSH key: %s" % err

    argv = [
        "ssh-copy-id",
        "-i", str(pub),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "PubkeyAuthentication=no",          # force the password path
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "ConnectTimeout=10",
        "-o", "NumberOfPasswordPrompts=1",
        "-p", str(port),
        "%s@%s" % (user, host),
    ]
    if not which("ssh-copy-id"):
        return False, "ssh-copy-id is not installed on this machine."

    try:
        pid, fd = pty.fork()
    except OSError as exc:
        return False, "Could not start a terminal for ssh-copy-id: %s" % exc

    if pid == 0:                                   # child
        try:
            os.execvp(argv[0], argv)
        except Exception:
            os._exit(127)

    transcript = ""
    sent = False
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            text = chunk.decode("utf-8", "replace")
            transcript += text

            if not sent and PROMPT_PW.search(text):
                os.write(fd, bytes(password) + b"\n")
                sent = True
            elif PROMPT_YN.search(text):
                os.write(fd, b"yes\n")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass

    try:
        _, status = os.waitpid(pid, 0)
        code = os.waitstatus_to_exitcode(status)
    except Exception:
        code = 1

    tail = transcript.strip().splitlines()
    tail = tail[-1] if tail else ""

    if code == 0:
        return True, "Key installed on %s@%s." % (user, host)
    # Order matters: a refusal with no prompt is not a wrong password.
    if not sent:
        return False, ("The host never asked for a password. It may accept keys only, "
                       "or the account name is wrong. Last line: %s" % tail)
    if DENIED.search(transcript):
        return False, "The host rejected that password."
    return False, "ssh-copy-id exited with %s. %s" % (code, tail)
