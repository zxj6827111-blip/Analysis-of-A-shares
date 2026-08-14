"""App version and git build information.

Version display scheme (shown in the top bar / /api/v1/version):
    v0.1.0-b84-abc1234        released commits, no local edits
    v0.1.0-b84-abc1234*       working tree has uncommitted changes

The semantic version (APP_VERSION) is bumped manually on release; the
build suffix (commit count + short hash + dirty flag) is derived from
git automatically, so every committed change yields a new visible
version string without manual bookkeeping.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Dict, Optional

APP_VERSION = "2.5"

# git build info changes only on commit; a long TTL keeps page refreshes
# from re-running `git status` (which takes ~0.5s on Windows).
_CACHE_TTL_SEC = 300.0
_CACHE_LOCK = threading.Lock()
_CACHE = {"ts": 0.0, "info": None}

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _git(args) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", _REPO_ROOT] + args,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return proc.stdout.strip()
    except Exception:
        return ""


def get_build_info() -> Dict:
    """Return git-derived build info; falls back to empty fields off-git."""
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE["info"] is not None and now - _CACHE["ts"] < _CACHE_TTL_SEC:
            return dict(_CACHE["info"])
        commit = _git(["rev-parse", "--short", "HEAD"])
        count = _git(["rev-list", "--count", "HEAD"])
        branch = _git(["branch", "--show-current"])
        last_commit_at = _git(["log", "-1", "--format=%cs"])
        dirty = bool(_git(["status", "--porcelain"]))
        info = {
            "commit": commit or None,
            "commit_count": int(count) if count.isdigit() else None,
            "branch": branch or None,
            "last_commit_at": last_commit_at or None,
            "dirty": dirty,
        }
        _CACHE["ts"] = now
        _CACHE["info"] = info
        return dict(info)


def get_version_string() -> str:
    """Human-readable version, e.g. v0.1.0-b84-abc1234*."""
    info = get_build_info()
    s = f"v{APP_VERSION}"
    if info.get("commit_count"):
        s += f"-b{info['commit_count']}"
    if info.get("commit"):
        s += f"-{info['commit']}"
    if info.get("dirty"):
        s += "*"
    return s


def get_version_info() -> Dict:
    """Full version payload for /api/v1/version and the top bar."""
    return {
        "app": "astock",
        "version": APP_VERSION,
        "version_string": get_version_string(),
        "build": get_build_info(),
    }


def refresh_build_info() -> Optional[Dict]:
    """Force re-read of git info (ignores cache). For debugging/tests."""
    with _CACHE_LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["info"] = None
    return get_build_info()
