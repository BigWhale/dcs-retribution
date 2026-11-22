"""The state of the link to DCS Companion.

Its own module, so the game (`liverecord`) can name it without importing the
link and `httpx`.
"""

from __future__ import annotations

from enum import Enum


class LinkStatus(Enum):
    WAITING = "waiting"  # no connection
    LINKED = "linked"  # connected, no snapshot yet
    RECEIVING = "receiving"  # at least one snapshot on this connection
    REFUSED = "refused"  # 401
