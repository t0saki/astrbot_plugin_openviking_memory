"""
Historical message backfill on first venue encounter.

When the plugin first sees a venue, it pulls platform history and writes
it to OV in chronological order. Runs as a background asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .client import OVClient
from .config import PluginConfig
from .identity import derive_session_id, venue_is_group
from .parts import build_message, user_text_part

logger = logging.getLogger("astrbot_plugin_openviking_memory")

STALE_RUNNING_SECONDS = 600


class BackfillManager:
    def __init__(
        self,
        client: OVClient,
        cfg: PluginConfig,
        kv_get: Callable[[str, Any], Awaitable[Any]],
        kv_put: Callable[[str, Any], Awaitable[None]],
    ):
        self._client = client
        self._cfg = cfg
        self._kv_get = kv_get
        self._kv_put = kv_put
        self._running: set[str] = set()

    async def maybe_trigger(
        self,
        venue_id: str,
        platform: str,
        group_id: str,
        api_key: str,
        event: Any = None,
        fanout_write: Callable | None = None,
    ):
        if not self._cfg.backfill_on_first_seen:
            return
        if venue_id in self._running:
            return

        done_key = f"backfill_done::{venue_id}"
        done = await self._kv_get(done_key, None)
        if done:
            return

        status_key = f"backfill_status::{venue_id}"
        status_raw = await self._kv_get(status_key, None)
        if status_raw:
            try:
                status = json.loads(status_raw) if isinstance(status_raw, str) else status_raw
                if time.time() - status.get("ts", 0) < STALE_RUNNING_SECONDS:
                    return
            except (json.JSONDecodeError, TypeError):
                pass

        self._running.add(venue_id)
        asyncio.create_task(
            self._run_backfill(venue_id, platform, group_id, api_key, event, fanout_write)
        )

    async def force_backfill(
        self,
        venue_id: str,
        platform: str,
        group_id: str,
        api_key: str,
        event: Any = None,
        fanout_write: Callable | None = None,
    ):
        done_key = f"backfill_done::{venue_id}"
        await self._kv_put(done_key, "")
        self._running.discard(venue_id)
        await self.maybe_trigger(venue_id, platform, group_id, api_key, event, fanout_write)

    async def _run_backfill(
        self,
        venue_id: str,
        platform: str,
        group_id: str,
        api_key: str,
        event: Any,
        fanout_write: Callable | None,
    ):
        status_key = f"backfill_status::{venue_id}"
        await self._kv_put(status_key, json.dumps({"status": "running", "ts": time.time()}))

        try:
            messages = await self._fetch_history(platform, group_id, event)
            if not messages:
                logger.info("backfill %s: no history available", venue_id)
                await self._mark_done(venue_id, 0)
                return

            session_id = derive_session_id(venue_id)
            is_group = venue_is_group(venue_id)
            count = 0

            for batch_start in range(0, len(messages), self._cfg.backfill_batch_size):
                batch = messages[batch_start : batch_start + self._cfg.backfill_batch_size]
                for msg in batch:
                    text = msg.get("text", "")
                    if not text.strip():
                        continue
                    sender_name = msg.get("sender_name", "")
                    sender_id = msg.get("sender_id", "")
                    parts = [user_text_part(text, sender_name, sender_id, is_group)]
                    payload = build_message("user", parts)
                    await self._client.add_message(session_id, payload, api_key=api_key)

                    if fanout_write and is_group:
                        await fanout_write(
                            text=text,
                            sender_name=sender_name,
                            sender_id=sender_id,
                            origin_venue_id=venue_id,
                            platform=platform,
                            api_key=api_key,
                            event=event,
                        )
                    count += 1

                if batch_start + self._cfg.backfill_batch_size < len(messages):
                    await asyncio.sleep(self._cfg.backfill_throttle_ms / 1000.0)

            if count > 0:
                await self._client.commit_session(session_id, api_key=api_key)

            logger.info("backfill %s: ingested %d messages", venue_id, count)
            await self._mark_done(venue_id, count)

        except Exception:
            logger.exception("backfill failed for %s", venue_id)
            await self._kv_put(status_key, json.dumps({"status": "failed", "ts": time.time()}))
        finally:
            self._running.discard(venue_id)

    async def _mark_done(self, venue_id: str, count: int):
        done_key = f"backfill_done::{venue_id}"
        await self._kv_put(done_key, json.dumps({"count": count, "ts": time.time()}))
        status_key = f"backfill_status::{venue_id}"
        await self._kv_put(status_key, json.dumps({"status": "done", "ts": time.time()}))

    async def _fetch_history(
        self,
        platform: str,
        group_id: str,
        event: Any,
    ) -> list[dict[str, str]]:
        bot = getattr(event, "bot", None) if event else None
        if bot is None:
            return []

        cutoff_ts = time.time() - self._cfg.backfill_max_age_days * 86400
        max_msgs = self._cfg.backfill_max_messages

        if platform == "aiocqhttp" and group_id:
            return await self._fetch_aiocqhttp(bot, group_id, cutoff_ts, max_msgs)

        return []

    async def _fetch_aiocqhttp(
        self,
        bot: Any,
        group_id: str,
        cutoff_ts: float,
        max_msgs: int,
    ) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        message_seq = 0

        try:
            for _ in range(max_msgs // 20 + 1):
                kwargs: dict[str, Any] = {"group_id": int(group_id)}
                if message_seq:
                    kwargs["message_seq"] = message_seq

                resp = await bot.api.call_action("get_group_msg_history", **kwargs)
                messages = resp if isinstance(resp, list) else resp.get("messages", [])
                if not messages:
                    break

                for msg in messages:
                    ts = msg.get("time", 0)
                    if ts and ts < cutoff_ts:
                        continue

                    raw_msg = msg.get("raw_message") or msg.get("message", "")
                    if isinstance(raw_msg, list):
                        text_parts = [
                            seg.get("data", {}).get("text", "")
                            for seg in raw_msg
                            if seg.get("type") == "text"
                        ]
                        raw_msg = " ".join(t for t in text_parts if t)

                    sender = msg.get("sender", {})
                    results.append(
                        {
                            "text": str(raw_msg),
                            "sender_name": sender.get("nickname", sender.get("card", "")),
                            "sender_id": str(msg.get("user_id", sender.get("user_id", ""))),
                            "ts": str(ts),
                        }
                    )

                    if len(results) >= max_msgs:
                        break

                if len(results) >= max_msgs:
                    break

                first_seq = messages[0].get("message_seq") if messages else None
                if first_seq and first_seq != message_seq:
                    message_seq = first_seq
                else:
                    break

        except Exception:
            logger.debug("aiocqhttp history fetch failed for group %s", group_id)

        results.sort(key=lambda m: m.get("ts", "0"))
        return results

    async def get_status(self, venue_id: str) -> str:
        done = await self._kv_get(f"backfill_done::{venue_id}", None)
        if done:
            try:
                data = json.loads(done) if isinstance(done, str) else done
                return f"done ({data.get('count', '?')} msgs)"
            except (json.JSONDecodeError, TypeError):
                return "done"
        if venue_id in self._running:
            return "running"
        return "pending"
