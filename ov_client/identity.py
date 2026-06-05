"""
Derive OV venue / user / session / peer identifiers from AstrBot events.

Memory model: one bot "self" (an OV user) + one "peer" per person.

- self_scope=global: a single bot self for the whole instance; peers are shared
  across all venues (one profile per person, keyed by sender_id).
- self_scope=venue: one bot self per group/DM; peers are isolated per venue.

Sessions are rolling conversation buffers per venue regardless of self_scope.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .config import normalize_self_scope

if TYPE_CHECKING:
    from .config import PluginConfig


def get_effective_self_scope(cfg: PluginConfig, group_id: str) -> str:
    """Resolve self_scope for a venue, honoring per-group overrides."""
    if group_id and group_id in cfg.isolation_overrides:
        return normalize_self_scope(cfg.isolation_overrides[group_id])
    return normalize_self_scope(cfg.self_scope)


def derive_venue(platform: str, group_id: str, sender_id: str) -> str:
    if group_id:
        return f"{platform}-group-{group_id}"
    return f"{platform}-dm-{sender_id}"


def derive_ov_user_id(
    cfg: PluginConfig,
    platform: str,
    group_id: str,
    sender_id: str,
) -> str:
    """OV user (self) id. In global scope this is the single bot user; in venue
    scope it is per-venue. Only meaningful for the venue-minting path."""
    scope = get_effective_self_scope(cfg, group_id)
    if scope == "global":
        return cfg.global_user_id
    venue_id = derive_venue(platform, group_id, sender_id)
    return f"astrbot-{venue_id}"


def derive_session_id(venue_id: str) -> str:
    # Sessions are keyed globally by viking://session/<id>. The old "astrbot::<venue>"
    # ids collide with sessions created by pre-peer deployments, whose .meta.json
    # lacks account attribution and fails OV's account-scoped visibility check
    # (NotFoundError → 404 on append/commit). Use a fresh, colon-free namespace so
    # every session is created cleanly under the current identity.
    return f"astrbot-sess-{venue_id}"


def venue_is_group(venue_id: str) -> bool:
    return "-group-" in venue_id


def safe_peer_id(sender_id: str | None) -> str | None:
    """Normalize a sender id into a usable peer_id.

    OV rejects path separators (they would cross into another namespace), so
    strip them; empty/whitespace-only ids become None (→ routes to self).
    """
    if not sender_id:
        return None
    cleaned = str(sender_id).replace("/", "").replace("\\", "").strip()
    return cleaned or None


def parse_venue_origin(venue_id: str) -> str:
    """Human-readable origin label from a venue_id like 'aiocqhttp-group-123'."""
    parts = venue_id.split("-", 2)
    if len(parts) < 3:
        return venue_id
    platform, kind, raw_id = parts[0], parts[1], parts[2]
    return f"{platform}-{kind}:{raw_id}"
