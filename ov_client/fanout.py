"""
Cross-venue message fanout for venue_user_fanout isolation mode.

When user A sends a message in group G, the message is also written to all
other venues where A is currently a member. This gives the bot cross-venue
awareness of what A has said.

Member lookup priority:
1. Platform API (aiocqhttp get_group_member_list, etc.) — cached in KV
2. Observation fallback: accumulate venues_of_user as messages arrive
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Awaitable, Callable

from .config import PluginConfig
from .identity import derive_venue

logger = logging.getLogger("astrbot_plugin_openviking_memory")


class FanoutManager:
    def __init__(
        self,
        cfg: PluginConfig,
        kv_get: Callable[[str, Any], Awaitable[Any]],
        kv_put: Callable[[str, Any], Awaitable[None]],
    ):
        self._cfg = cfg
        self._kv_get = kv_get
        self._kv_put = kv_put

    async def record_observation(self, platform: str, sender_id: str, venue_id: str):
        """Track that sender_id has been seen in venue_id."""
        key = f"venues_of::{platform}::{sender_id}"
        raw = await self._kv_get(key, "[]")
        try:
            venues: list[str] = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            venues = []
        if venue_id not in venues:
            venues.append(venue_id)
            await self._kv_put(key, json.dumps(venues))

    async def get_fanout_targets(
        self,
        platform: str,
        sender_id: str,
        current_venue_id: str,
        event: Any = None,
    ) -> list[str]:
        """Return venue_ids where sender_id is currently present, excluding current."""
        targets: set[str] = set()

        members = await self._try_platform_members(platform, sender_id, event)
        if members:
            targets.update(members)

        observed = await self._get_observed_venues(platform, sender_id)
        targets.update(observed)

        dm_venue = derive_venue(platform, "", sender_id)
        targets.add(dm_venue)

        targets.discard(current_venue_id)
        return list(targets)

    async def _try_platform_members(
        self,
        platform: str,
        sender_id: str,
        event: Any,
    ) -> list[str]:
        """Try to get all groups where sender_id is a member via platform API."""
        if event is None:
            return []

        cache_key = f"user_groups::{platform}::{sender_id}"
        cached = await self._kv_get(cache_key, None)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                if time.time() - data.get("ts", 0) < self._cfg.fanout_member_cache_ttl_seconds:
                    return data.get("venues", [])
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        venues = await self._query_user_groups(platform, sender_id, event)
        if venues:
            await self._kv_put(
                cache_key,
                json.dumps({"venues": venues, "ts": time.time()}),
            )
        return venues

    async def _query_user_groups(
        self,
        platform: str,
        sender_id: str,
        event: Any,
    ) -> list[str]:
        """Platform-specific group membership query. Best-effort."""
        try:
            bot = getattr(event, "bot", None) if event else None
            if bot is None:
                return []

            if platform == "aiocqhttp":
                group_list = await bot.api.call_action("get_group_list")
                result = []
                for g in group_list:
                    gid = str(g.get("group_id", ""))
                    if not gid:
                        continue
                    try:
                        members = await bot.api.call_action(
                            "get_group_member_list",
                            group_id=int(gid),
                        )
                        member_ids = {str(m.get("user_id", "")) for m in members}
                        if sender_id in member_ids:
                            result.append(derive_venue(platform, gid, sender_id))
                    except Exception:
                        continue
                return result
        except Exception:
            logger.debug("platform member query failed for %s/%s", platform, sender_id)
        return []

    async def _get_observed_venues(self, platform: str, sender_id: str) -> list[str]:
        key = f"venues_of::{platform}::{sender_id}"
        raw = await self._kv_get(key, "[]")
        try:
            return json.loads(raw) if isinstance(raw, str) else (raw or [])
        except (json.JSONDecodeError, TypeError):
            return []

    async def get_group_members_for_venue(
        self,
        platform: str,
        group_id: str,
        event: Any = None,
    ) -> list[str]:
        """Get member IDs of a group. Used by backfill for fanout."""
        cache_key = f"members::{platform}-group-{group_id}"
        cached = await self._kv_get(cache_key, None)
        if cached:
            try:
                data = json.loads(cached) if isinstance(cached, str) else cached
                if time.time() - data.get("ts", 0) < self._cfg.fanout_member_cache_ttl_seconds:
                    return data.get("uids", [])
            except (json.JSONDecodeError, TypeError, KeyError):
                pass

        uids = await self._query_group_members(platform, group_id, event)
        if uids:
            await self._kv_put(
                cache_key,
                json.dumps({"uids": uids, "ts": time.time()}),
            )
        return uids

    async def _query_group_members(
        self,
        platform: str,
        group_id: str,
        event: Any,
    ) -> list[str]:
        try:
            bot = getattr(event, "bot", None) if event else None
            if bot is None:
                return []
            if platform == "aiocqhttp":
                members = await bot.api.call_action(
                    "get_group_member_list",
                    group_id=int(group_id),
                )
                return [str(m.get("user_id", "")) for m in members if m.get("user_id")]
        except Exception:
            logger.debug("group member query failed for %s/%s", platform, group_id)
        return []
