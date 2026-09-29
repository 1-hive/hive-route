# SPDX-License-Identifier: GPL-3.0-or-later
"""hive-route: One Hive R8, a slim model router (ROUTING.md)."""

from .decide import decide
from .errors import RouteError
from .table import Table, load_table

__version__ = "0.1.0.dev0"
__all__ = ["RouteError", "Table", "__version__", "decide", "load_table"]
