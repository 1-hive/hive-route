# SPDX-License-Identifier: GPL-3.0-or-later
"""Errors. ``code`` is the contract; ``reason`` is for humans and must never be parsed."""

from __future__ import annotations


class RouteError(Exception):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"[{code}] {reason}")
        self.code = code
        self.reason = reason
