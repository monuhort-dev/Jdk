"""
BotHost process sandbox — Python edition.

Uses sys.addaudithook() (Python 3.8+, one-way — cannot be removed or bypassed
by user code) to intercept EVERY filesystem operation and block access to any
path inside the entire bot-hosting app directory that does not belong to this
server's own upload folder.

Blocked zone
────────────
  Everything inside BOTHOST_APP_DIR  (e.g. /…/artifacts/bot-hosting/)
  …EXCEPT this server's own folder:  BOTHOST_SANDBOX_DIR
                                      (e.g. /…/uploads/<server_id>/)

This means bots cannot read:
  • app.py, templates/, static/          ← web source code
  • _sandbox_preamble.py / .js           ← sandbox scripts
  • uploads/<other_server_id>/           ← other users' files
  • bothost.db                           ← database

Bots CAN read/write:
  • uploads/<own_server_id>/             ← their own directory only
  • /nix/, /usr/, /lib/, /tmp/, /proc/   ← system / runtime paths

Covered audit events
────────────────────
  open / io.open / io.open_code / os.open
  os.listdir / os.scandir
  os.stat (→ os.path.exists / isdir / isfile / getsize / …)
  os.unlink / os.mkdir / os.rmdir
  os.rename / os.link / os.symlink
  os.chmod / os.chown / os.truncate
  shutil.copyfile

Environment variables set by BotHost before launch:
  BOTHOST_SANDBOX_DIR   – this server's own upload dir (absolute)
  BOTHOST_APP_DIR       – root of the bot-hosting web app (absolute)
  BOTHOST_ENTRY_FILE    – absolute path to the bot's entry-point file
"""
import sys
import os
from pathlib import Path

_BOT_DIR  = Path(os.environ['BOTHOST_SANDBOX_DIR']).resolve()
_APP_DIR  = Path(os.environ['BOTHOST_APP_DIR']).resolve()
_ENTRY    = os.environ['BOTHOST_ENTRY_FILE']


def _is_blocked(raw) -> bool:
    """
    Return True when 'raw' resolves to a path that is:
      • inside the app directory  (i.e. web source code or other servers)
      • but NOT inside this server's own upload folder
    """
    if not raw:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode(errors='replace')
    if not isinstance(raw, str):
        return False
    try:
        resolved = Path(raw).resolve()
    except Exception:
        return False

    # Only restrict paths that live inside the app directory
    try:
        resolved.relative_to(_APP_DIR)
    except ValueError:
        return False          # system path / outside app — allow freely

    # Inside the app dir: allow ONLY this bot's own upload folder
    try:
        resolved.relative_to(_BOT_DIR)
        return False          # own upload dir — allow
    except ValueError:
        return True           # app source code or another server — BLOCK


def _deny(path):
    raise PermissionError(
        f"[BotHost Sandbox] Access denied: '{path}' is outside your server's directory."
    )


# Events where args[0] is the path
_SINGLE_PATH = frozenset({
    'open', 'io.open', 'io.open_code', 'os.open',
    'os.listdir', 'os.scandir',
    'os.stat',
    'os.unlink', 'os.mkdir', 'os.rmdir',
    'os.chmod', 'os.chown', 'os.truncate',
})

# Events where args[0] = src and args[1] = dst
_TWO_PATH = frozenset({
    'os.rename',
    'os.link',
    'os.symlink',
    'shutil.copyfile',
})


def _sandbox_audit(event, args):
    if not args:
        return
    if event in _SINGLE_PATH:
        if _is_blocked(args[0]):
            _deny(args[0])
    elif event in _TWO_PATH:
        if _is_blocked(args[0]):
            _deny(args[0])
        if len(args) > 1 and _is_blocked(args[1]):
            _deny(args[1])


sys.addaudithook(_sandbox_audit)

import runpy
runpy.run_path(_ENTRY, run_name='__main__')
