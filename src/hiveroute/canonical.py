# SPDX-License-Identifier: GPL-3.0-or-later
"""Canonical JSON and digests. Tables, routes and log lines use one encoding, so a
digest computed anywhere names the same content."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

# datetime.UTC needs Python 3.11; hive-route runs on 3.10 (Ubuntu 22.04's default).
UTC = timezone.utc


def dumps(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def digest(obj: object) -> str:
    return "sha256:" + hashlib.sha256(dumps(obj).encode()).hexdigest()


def parse_time(value: str) -> datetime:
    """An RFC 3339 timestamp with an explicit offset, as an aware UTC datetime."""
    if value[-1:] in ("Z", "z"):  # fromisoformat accepts Z only from Python 3.11
        value = value[:-1] + "+00:00"
    t = datetime.fromisoformat(value)
    if t.tzinfo is None:
        raise ValueError(f"timestamp has no offset: {value!r}")
    return t.astimezone(UTC)


def format_time(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")
