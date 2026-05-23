"""
Derive OV venue / user / session identifiers from AstrBot events.

Memory isolation is at the OV *user* level, not the session level.
Each venue (group or DM) maps to one OV user; sessions are rolling
conversation buffers within that user.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import PluginConfig

VALID_ISOLATION_MODES = {"venue_user", "venue_user_fanout", "global_user"}


def get_effective_mode(cfg: PluginConfig, group_id: str) -> str:
    if group_id and group_id in cfg.isolation_overrides:
        mode = cfg.isolation_overrides[group_id]
        if mode in VALID_ISOLATION_MODES:
            return mode
    return cfg.isolation_mode


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
    mode = get_effective_mode(cfg, group_id)
    if mode == "global_user":
        return cfg.global_user_id
    venue_id = derive_venue(platform, group_id, sender_id)
    return f"astrbot-{venue_id}"


def derive_session_id(venue_id: str) -> str:
    return f"astrbot::{venue_id}"


def venue_is_group(venue_id: str) -> bool:
    return "-group-" in venue_id


def parse_venue_origin(venue_id: str) -> str:
    """Human-readable origin label from a venue_id like 'aiocqhttp-group-123'."""
    parts = venue_id.split("-", 2)
    if len(parts) < 3:
        return venue_id
    platform, kind, raw_id = parts[0], parts[1], parts[2]
    return f"{platform}-{kind}:{raw_id}"
