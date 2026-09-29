# SPDX-License-Identifier: GPL-3.0-or-later
"""The qualifications file (ROUTING.md §3): ``{route_id: {status, route_pin, ...}}``.

Written by canary runs and drift checks, read into the state (§7). Updates hold a lock,
so parallel canary runs on different pools don't overwrite each other's results.
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")


def path_of(p: str) -> Path:
    return Path(os.path.expanduser(p))


def update(path: str, change: Callable[[dict], T]) -> T:
    """Apply ``change`` to the file's content under an exclusive lock, and write it back."""
    p = path_of(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p.with_name(p.name + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        quals = json.loads(p.read_text()) if p.exists() else {}
        result = change(quals)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(quals, indent=2) + "\n")
        tmp.replace(p)
        return result
