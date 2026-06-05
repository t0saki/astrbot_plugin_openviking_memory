"""
Track recently-active senders (peers) per venue.

Used to widen peer recall beyond just the current speaker: in a group, when A
asks something, we also recall the profiles of recently-active members (B, C…)
by naming their peer_ids. In-memory only — resets on restart, no persistence.
"""

from __future__ import annotations

import time
from collections import OrderedDict


class PresenceTracker:
    def __init__(self, window: int = 5, ttl_seconds: float = 1800.0):
        self._window = max(0, window)
        self._ttl = max(0.0, ttl_seconds)
        # venue_id -> {peer_id: last_seen_ts}, newest at the end.
        self._venues: dict[str, OrderedDict[str, float]] = {}

    def record(self, venue_id: str, peer_id: str | None, *, now: float | None = None) -> None:
        if not peer_id or self._window <= 0:
            return
        ts = time.time() if now is None else now
        members = self._venues.setdefault(venue_id, OrderedDict())
        members.pop(peer_id, None)
        members[peer_id] = ts
        # Keep the speaker plus up to `window` other recent members.
        while len(members) > self._window + 1:
            members.popitem(last=False)

    def active(
        self,
        venue_id: str,
        *,
        exclude: str | None = None,
        now: float | None = None,
    ) -> list[str]:
        """Recently-active peer_ids, most-recent first, minus `exclude`."""
        members = self._venues.get(venue_id)
        if not members:
            return []
        cur = time.time() if now is None else now
        stale: list[str] = []
        fresh: list[str] = []
        for peer_id, ts in members.items():
            if self._ttl and cur - ts > self._ttl:
                stale.append(peer_id)
                continue
            if peer_id != exclude:
                fresh.append(peer_id)
        for peer_id in stale:
            members.pop(peer_id, None)
        return list(reversed(fresh))[: self._window]
