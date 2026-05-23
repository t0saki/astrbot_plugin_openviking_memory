"""
AstrBot OpenViking Memory Plugin — main entry point.

Star subclass that registers hooks for auto-capture, auto-recall,
commit scheduling, fanout, and backfill.
"""

from __future__ import annotations

import logging
from typing import Any

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType, PermissionType
from astrbot.api.star import Context, Star, register

from .ov_client.backfill import BackfillManager
from .ov_client.client import OVClient
from .ov_client.commit_scheduler import CommitScheduler
from .ov_client.config import PluginConfig
from .ov_client.fanout import FanoutManager
from .ov_client.identity import (
    derive_ov_user_id,
    derive_session_id,
    derive_venue,
    get_effective_mode,
    venue_is_group,
)
from .ov_client.parts import (
    assistant_text_part,
    build_message,
    estimate_tokens,
    fanout_text_part,
    file_placeholder_part,
    image_placeholder_part,
    tool_call_part,
    tool_result_part,
    user_text_part,
)
from .ov_client.recall import recall_and_format


@register(
    "astrbot_plugin_openviking_memory",
    "tosaki",
    "OpenViking Memory Plugin",
    "0.1.0",
    "https://github.com/t0saki/astrbot_plugin_openviking_memory",
)
class OpenVikingMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.logger = logging.getLogger("astrbot")
        raw_config = dict(config) if config else {}
        self.cfg = PluginConfig(raw_config)

        account_id = self.cfg.ov_account_id
        effective_key = self.cfg.ov_admin_api_key or self.cfg.ov_user_api_key
        if not account_id and effective_key:
            account_id = _parse_account_from_key(effective_key)

        self.ov = OVClient(
            base_url=self.cfg.ov_base_url,
            api_key=effective_key,
            account_id=account_id,
        )
        import hashlib

        url_hash = hashlib.md5(self.cfg.ov_base_url.encode()).hexdigest()[:8]
        self._kv_prefix = f"ov_{url_hash}_"

        self.scheduler = CommitScheduler(self.ov, self.cfg)
        self.fanout = FanoutManager(
            self.cfg,
            kv_get=self._kv_get,
            kv_put=self._kv_put,
        )
        self.backfill = BackfillManager(
            self.ov,
            self.cfg,
            kv_get=self._kv_get,
            kv_put=self._kv_put,
            kv_prefix=self._kv_prefix,
        )
        self._venue_auth: dict[str, tuple[str, str]] = {}

    async def _kv_get(self, key: str, default: Any = None) -> Any:
        return await self.get_kv_data(key, default)

    async def _kv_put(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    # -- auth helpers ---------------------------------------------------------

    async def _ensure_venue_user(self, venue_id: str, ov_user_id: str):
        if venue_id in self._venue_auth:
            return

        if self.cfg.ov_user_api_key and self.cfg.isolation_mode == "global_user":
            self._venue_auth[venue_id] = (self.cfg.ov_user_api_key, "")
            self.logger.debug("[OV] global_user mode: using user key directly")
            return

        cached_key = await self._kv_get(f"{self._kv_prefix}key::{venue_id}")
        if cached_key:
            self._venue_auth[venue_id] = (cached_key, "")
            self.logger.debug("[OV] venue %s: loaded cached key", venue_id)
            return

        if not self.cfg.ov_admin_api_key:
            self._venue_auth[venue_id] = ("", "")
            self.logger.warning("[OV] no admin key — no user isolation")
            return

        self.logger.info("[OV] creating user %s (account=%s)", ov_user_id, self.ov.account_id)
        result, err = await self.ov.create_user(ov_user_id, self.cfg.ov_admin_api_key)
        if result and "user_key" in result:
            key = result["user_key"]
            await self._kv_put(f"{self._kv_prefix}key::{venue_id}", key)
            self._venue_auth[venue_id] = (key, "")
            self.logger.info("[OV] created user %s OK", ov_user_id)
            return

        self._venue_auth[venue_id] = ("", ov_user_id)
        self.logger.warning("[OV] create_user %s failed: %s — admin fallback", ov_user_id, err)

    def _auth(self, venue_id: str) -> dict[str, str | None]:
        api_key, user_id = self._venue_auth.get(venue_id, ("", ""))
        return {"api_key": api_key or None, "user_id": user_id or None}

    def _extract_event_info(self, event: AstrMessageEvent) -> dict:
        platform = getattr(event, "get_platform_name", lambda: "unknown")()
        group_id = getattr(event, "get_group_id", lambda: "")() or ""
        sender_id = getattr(event, "get_sender_id", lambda: "")() or ""
        sender_name = getattr(event, "get_sender_name", lambda: "")() or ""
        text = getattr(event, "message_str", "") or ""
        return {
            "platform": str(platform),
            "group_id": str(group_id),
            "sender_id": str(sender_id),
            "sender_name": str(sender_name),
            "text": str(text),
        }

    # -- hook: on_astrbot_loaded ----------------------------------------------

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        ok = await self.ov.health()
        if ok:
            self.logger.info(
                "[OV] server reachable at %s (account=%s)",
                self.cfg.ov_base_url,
                self.ov.account_id or "(not set)",
            )
        else:
            self.logger.warning("[OV] server NOT reachable at %s", self.cfg.ov_base_url)

    # -- hook: capture user messages ------------------------------------------

    @filter.event_message_type(EventMessageType.ALL)
    async def on_user_message(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        if not info["text"].strip():
            return

        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_venue_user(venue_id, ov_user_id)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        is_group = venue_is_group(venue_id)

        parts = [user_text_part(info["text"], info["sender_name"], info["sender_id"], is_group)]

        msg_chain = getattr(event, "message_obj", None)
        if msg_chain:
            self._append_media_placeholders(msg_chain, parts)

        payload = build_message("user", parts)
        ok = await self.ov.add_message(session_id, payload, **auth)
        if ok:
            self.scheduler.set_auth(session_id, auth)
            await self.scheduler.record_message(session_id, estimate_tokens(info["text"]))
        else:
            self.logger.warning("[OV] add_message failed for %s", session_id)

        await self.fanout.record_observation(info["platform"], info["sender_id"], venue_id)

        mode = get_effective_mode(self.cfg, info["group_id"])
        if mode == "venue_user_fanout":
            await self._fanout_message(
                info["text"],
                info["sender_name"],
                info["sender_id"],
                venue_id,
                info["platform"],
                event,
            )

        await self.backfill.maybe_trigger(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
            fanout_write=(self._fanout_backfill_message if mode == "venue_user_fanout" else None),
        )

    def _append_media_placeholders(self, msg_chain: Any, parts: list):
        chain = getattr(msg_chain, "message", None) or []
        for comp in chain:
            comp_type = type(comp).__name__
            if comp_type == "Image":
                url = getattr(comp, "url", "") or getattr(comp, "file", "") or ""
                if url:
                    parts.append(image_placeholder_part(url))
            elif comp_type == "File":
                name = getattr(comp, "name", "") or getattr(comp, "file", "") or ""
                if name:
                    parts.append(file_placeholder_part(name))

    async def _fanout_message(
        self,
        text: str,
        sender_name: str,
        sender_id: str,
        origin_venue_id: str,
        platform: str,
        event: Any,
    ):
        targets = await self.fanout.get_fanout_targets(
            platform,
            sender_id,
            origin_venue_id,
            event=event,
        )
        for target_venue_id in targets:
            target_ov_user_id = f"astrbot-{target_venue_id}"
            await self._ensure_venue_user(target_venue_id, target_ov_user_id)
            target_auth = self._auth(target_venue_id)
            target_session_id = derive_session_id(target_venue_id)
            parts = [fanout_text_part(text, origin_venue_id, sender_name, sender_id)]
            payload = build_message("user", parts)
            await self.ov.add_message(target_session_id, payload, **target_auth)
            self.scheduler.set_auth(target_session_id, target_auth)
            await self.scheduler.record_message(target_session_id, estimate_tokens(text))

    async def _fanout_backfill_message(self, **kwargs):
        await self._fanout_message(
            text=kwargs["text"],
            sender_name=kwargs["sender_name"],
            sender_id=kwargs["sender_id"],
            origin_venue_id=kwargs["origin_venue_id"],
            platform=kwargs["platform"],
            event=kwargs.get("event"),
        )

    # -- hook: recall on LLM request ------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        if not self.cfg.auto_recall_enabled:
            return

        info = self._extract_event_info(event)
        query = info["text"]
        if not query.strip():
            return

        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        auth = self._auth(venue_id)

        block = await recall_and_format(
            self.ov,
            self.cfg,
            query,
            venue_id,
            ov_user_id,
            **auth,
        )
        if block:
            req.system_prompt = (req.system_prompt or "") + "\n\n" + block

    # -- hook: capture LLM response -------------------------------------------

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: Any):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        reply_text = ""
        if hasattr(resp, "completion_text"):
            reply_text = resp.completion_text or ""
        elif hasattr(resp, "text"):
            reply_text = resp.text or ""
        elif hasattr(resp, "result_chain"):
            chain = resp.result_chain or []
            reply_text = " ".join(getattr(c, "text", str(c)) for c in chain if hasattr(c, "text"))

        if not reply_text.strip():
            return

        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        parts = [assistant_text_part(reply_text)]
        payload = build_message("assistant", parts)
        await self.ov.add_message(session_id, payload, **auth)
        await self.scheduler.record_message(session_id, estimate_tokens(reply_text))

        mode = get_effective_mode(self.cfg, info["group_id"])
        if mode == "venue_user_fanout":
            targets = await self.fanout.get_fanout_targets(
                info["platform"],
                info["sender_id"],
                venue_id,
                event=event,
            )
            for target_venue_id in targets:
                target_ov_uid = f"astrbot-{target_venue_id}"
                await self._ensure_venue_user(target_venue_id, target_ov_uid)
                target_auth = self._auth(target_venue_id)
                target_session_id = derive_session_id(target_venue_id)
                fo_parts = [fanout_text_part(reply_text, venue_id)]
                fo_payload = build_message("assistant", fo_parts)
                await self.ov.add_message(target_session_id, fo_payload, **target_auth)

    # -- hook: tool I/O capture -----------------------------------------------

    @filter.on_using_llm_tool()
    async def on_tool_call(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        t_name = str(kwargs.get("tool_name", args[0] if args else ""))
        t_input = kwargs.get("tool_input", args[1] if len(args) > 1 else None)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        parts = [tool_call_part(t_name, t_input)]
        payload = build_message("assistant", parts)
        await self.ov.add_message(session_id, payload, **auth)

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, *args, **kwargs):
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        t_name = str(kwargs.get("tool_name", args[0] if args else ""))
        t_output = kwargs.get("tool_output", args[-1] if args else None)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        parts = [tool_result_part(t_name, t_output)]
        payload = build_message("user", parts)
        await self.ov.add_message(session_id, payload, **auth)

    # -- hook: after message sent → commit eval -------------------------------

    @filter.after_message_sent()
    async def after_sent(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        session_id = derive_session_id(venue_id)
        await self.scheduler.evaluate(session_id)

    # -- commands -------------------------------------------------------------

    @filter.command("ov_status", alias={"ov-status"})
    async def cmd_status(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        session_id = derive_session_id(venue_id)

        healthy = await self.ov.health()
        sched = self.scheduler.get_status(session_id)
        bf_status = await self.backfill.get_status(venue_id)
        mode = get_effective_mode(self.cfg, info["group_id"])

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        api_key, fallback_uid = self._venue_auth.get(venue_id, ("", ""))
        if api_key and self.cfg.ov_user_api_key and mode == "global_user":
            key_status = "user key (global)"
        elif api_key:
            key_status = "per-venue key"
        elif fallback_uid:
            key_status = f"admin fallback (user={fallback_uid})"
        else:
            key_status = "no auth"

        lines = [
            "OpenViking Memory Plugin v0.1.0",
            f"Server: {self.cfg.ov_base_url} ({'OK' if healthy else 'UNREACHABLE'})",
            f"Account: {self.ov.account_id or '(not set)'}",
            f"OV User: {ov_user_id}",
            f"Auth: {key_status}",
            f"Isolation: {mode}",
            f"Venue: {venue_id}",
            f"Pending: {sched['pending_messages']} msgs / ~{sched['pending_tokens']} tokens",
            f"Last commit: {_fmt_ts(sched['last_commit_ts'])}",
            f"Backfill: {bf_status}",
            f"Venues: {len(self._venue_auth)}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("ov_backfill", alias={"ov-backfill"})
    async def cmd_backfill(self, event: AstrMessageEvent):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_venue_user(venue_id, ov_user_id)
        auth = self._auth(venue_id)
        mode = get_effective_mode(self.cfg, info["group_id"])

        await self.backfill.force_backfill(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
            fanout_write=(self._fanout_backfill_message if mode == "venue_user_fanout" else None),
        )
        yield event.plain_result(f"Backfill triggered for {venue_id}")

    # -- lifecycle ------------------------------------------------------------

    async def terminate(self):
        await self.scheduler.flush_all()
        await self.ov.close()
        self.logger.info("[OV] plugin terminated, all sessions flushed")


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return "never"
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _parse_account_from_key(api_key: str) -> str:
    import base64

    parts = api_key.split(".")
    if len(parts) >= 2:
        try:
            account = base64.b64decode(parts[0] + "==").decode("utf-8")
            if account:
                return account
        except Exception:
            pass
    return ""
