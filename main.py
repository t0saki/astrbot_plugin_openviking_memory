"""
AstrBot OpenViking Memory Plugin — main entry point.

Star subclass that registers hooks for auto-capture, auto-recall, commit
scheduling, and backfill.

Memory model (peer contract, OpenViking PR #2236+): the bot is the session
"self" (an OV user); each person is a "peer" keyed by sender_id. Incoming
messages carry peer_id; the bot's own replies and tool I/O stay self. Commit
sends memory_policy {self, peer} so OV builds a per-person profile under
viking://user/<bot>/peers/<sender_id>/.
"""

from __future__ import annotations

import asyncio
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
from .ov_client.identity import (
    derive_ov_user_id,
    derive_session_id,
    derive_venue,
    get_effective_self_scope,
    safe_peer_id,
    venue_is_group,
)
from .ov_client.parts import (
    assistant_text_part,
    build_message,
    estimate_tokens,
    file_placeholder_part,
    image_caption_part,
    image_placeholder_part,
    parse_image_captions,
    tool_call_part,
    tool_result_part,
    user_text_part,
)
from .ov_client.presence import PresenceTracker
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
            trusted_mode=self.cfg.trusted_mode,
        )
        import hashlib

        url_hash = hashlib.md5(self.cfg.ov_base_url.encode()).hexdigest()[:8]
        self._kv_prefix = f"ov_{url_hash}_"

        self.scheduler = CommitScheduler(self.ov, self.cfg)
        self.presence = PresenceTracker(
            window=self.cfg.peer_recall_active_window,
            ttl_seconds=float(self.cfg.commit_idle_seconds),
        )
        self.backfill = BackfillManager(
            self.ov,
            self.cfg,
            kv_get=self._kv_get,
            kv_put=self._kv_put,
            kv_prefix=self._kv_prefix,
        )
        # venue_id -> (api_key, fallback_user_id). fallback_user_id is only used
        # for X-OpenViking-User assertion in trusted_mode.
        self._venue_auth: dict[str, tuple[str, str]] = {}

    async def _kv_get(self, key: str, default: Any = None) -> Any:
        return await self.get_kv_data(key, default)

    async def _kv_put(self, key: str, value: Any) -> None:
        await self.put_kv_data(key, value)

    # -- auth helpers ---------------------------------------------------------

    async def _mint_user_key(self, user_id: str, cache_key: str) -> str:
        """Mint (or load cached) an OV user key via the admin API. '' on failure."""
        cached = await self._kv_get(cache_key)
        if cached:
            return cached
        if not self.cfg.ov_admin_api_key:
            return ""
        self.logger.info("[OV] creating user %s (account=%s)", user_id, self.ov.account_id)
        result, err = await self.ov.create_user(user_id, self.cfg.ov_admin_api_key)
        if result and "user_key" in result:
            key = result["user_key"]
            await self._kv_put(cache_key, key)
            self.logger.info("[OV] created user %s OK", user_id)
            return key
        self.logger.warning("[OV] create_user %s failed: %s", user_id, err)
        return ""

    async def _ensure_self_auth(self, venue_id: str, group_id: str, ov_user_id: str):
        """Resolve the Bearer identity (the bot self) for a venue.

        global scope → one bot self for the whole instance (user key, or a single
        minted global user). venue scope → one minted self per venue.
        """
        if venue_id in self._venue_auth:
            return

        scope = get_effective_self_scope(self.cfg, group_id)

        if scope == "global":
            if self.cfg.ov_user_api_key:
                self._venue_auth[venue_id] = (self.cfg.ov_user_api_key, "")
                return
            cache_key = f"{self._kv_prefix}gkey::{self.cfg.global_user_id}"
            key = await self._mint_user_key(self.cfg.global_user_id, cache_key)
            self._venue_auth[venue_id] = self._auth_or_fallback(key, self.cfg.global_user_id)
            return

        # venue scope: mint a per-venue self.
        key = await self._mint_user_key(ov_user_id, f"{self._kv_prefix}key::{venue_id}")
        self._venue_auth[venue_id] = self._auth_or_fallback(key, ov_user_id)

    def _auth_or_fallback(self, key: str, user_id: str) -> tuple[str, str]:
        if key:
            return (key, "")
        if self.cfg.trusted_mode:
            # Gateway asserts identity via X-OpenViking-User using the root/admin key.
            return ("", user_id)
        self.logger.warning(
            "[OV] no user key for %s (mint failed, not trusted_mode) — memory disabled",
            user_id,
        )
        return ("", "")

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

    def _peer_id_for(self, sender_id: str) -> str | None:
        return safe_peer_id(sender_id) if self.cfg.peer_enabled else None

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
        # Don't capture our own commands as memory, and don't let them double-fire
        # backfill alongside the command handler.
        if _is_self_command(info["text"]):
            return

        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        msg_chain = getattr(event, "message_obj", None)
        images = (
            self._collect_images(msg_chain) if (self.cfg.caption_all_images and msg_chain) else []
        )
        has_text = bool(info["text"].strip())
        if not has_text and not images:
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        is_group = venue_is_group(venue_id)
        peer_id = self._peer_id_for(info["sender_id"])
        self.presence.record(venue_id, peer_id)

        if has_text:
            parts = [
                user_text_part(
                    info["text"],
                    info["sender_name"],
                    info["sender_id"],
                    is_group,
                    group_id=info["group_id"],
                )
            ]
            if msg_chain:
                self._append_media_placeholders(msg_chain, parts)
            ok = await self.ov.add_message(
                session_id, build_message("user", parts), peer_id=peer_id, **auth
            )
            if ok:
                self.scheduler.set_auth(session_id, auth)
                await self.scheduler.record_message(session_id, estimate_tokens(info["text"]))
            else:
                self.logger.warning("[OV] add_message failed for %s", session_id)

        # Actively transcribe every image (not just bot-directed ones) in the
        # background so the VLM call doesn't block message handling.
        if images:
            asyncio.create_task(
                self._caption_images(images, session_id, auth, peer_id, info, is_group)
            )

        await self.backfill.maybe_trigger(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
        )

    def _collect_images(self, msg_chain: Any) -> list:
        chain = getattr(msg_chain, "message", None) or []
        return [c for c in chain if type(c).__name__ == "Image"]

    def _astrbot_provider_setting(self, key: str, default: str = "") -> str:
        try:
            cfg = self.context.get_config()
            return (cfg.get("provider_settings", {}) or {}).get(key, default)
        except Exception:
            return default

    def _image_caption_provider(self):
        pid = self.cfg.image_caption_provider_id or self._astrbot_provider_setting(
            "default_image_caption_provider_id", ""
        )
        if not pid:
            self.logger.warning("[OV] caption_all_images on but no image caption provider set")
            return None
        try:
            return self.context.get_provider_by_id(pid)
        except Exception:
            self.logger.warning("[OV] image caption provider %s not found", pid)
            return None

    def _image_caption_prompt(self) -> str:
        return (
            self.cfg.image_caption_prompt
            or self._astrbot_provider_setting("image_caption_prompt", "")
            or "请用中文描述这张图片，尽量包含其中的文字内容。"
        )

    async def _caption_images(self, images, session_id, auth, peer_id, info, is_group):
        provider = self._image_caption_provider()
        if provider is None:
            return
        prompt = self._image_caption_prompt()
        for comp in images:
            try:
                path = await comp.convert_to_file_path()
                resp = await provider.text_chat(prompt=prompt, image_urls=[path])
                cap = (getattr(resp, "completion_text", "") or "").strip()
            except Exception:
                self.logger.exception("[OV] image caption failed")
                continue
            if not cap:
                continue
            part = image_caption_part(
                cap, info["sender_name"], info["sender_id"], is_group, info["group_id"]
            )
            if await self.ov.add_message(
                session_id, build_message("user", [part]), peer_id=peer_id, **auth
            ):
                self.scheduler.set_auth(session_id, auth)
                await self.scheduler.record_message(session_id, estimate_tokens(cap))

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

    # -- hook: recall on LLM request ------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any):
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        peer_id = self._peer_id_for(info["sender_id"])

        # AstrBot turns images into a <image_caption>…</image_caption> text part on
        # the request (req built before this hook fires). Image-only messages have
        # empty message_str, so on_user_message skipped them — capture the caption
        # here as the image's textual content.
        # When caption_all_images is on, on_user_message already transcribes every
        # image (including this one) — don't also capture AstrBot's caption here.
        if self.cfg.capture_image_caption and not self.cfg.caption_all_images:
            is_group = venue_is_group(venue_id)
            for cap in _extract_image_captions(req):
                part = image_caption_part(
                    cap, info["sender_name"], info["sender_id"], is_group, info["group_id"]
                )
                if await self.ov.add_message(
                    session_id, build_message("user", [part]), peer_id=peer_id, **auth
                ):
                    self.scheduler.set_auth(session_id, auth)
                    await self.scheduler.record_message(session_id, estimate_tokens(cap))

        if not self.cfg.auto_recall_enabled or not info["text"].strip():
            return

        active = self.presence.active(venue_id, exclude=peer_id)
        block = await recall_and_format(
            self.ov,
            self.cfg,
            info["text"],
            venue_id,
            ov_user_id,
            speaker_id=info["sender_id"],
            active_member_ids=active,
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

        # The bot's own reply is the session owner (self) — no peer_id.
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        parts = [assistant_text_part(reply_text)]
        payload = build_message("assistant", parts)
        await self.ov.add_message(session_id, payload, **auth)
        await self.scheduler.record_message(session_id, estimate_tokens(reply_text))

    # -- hook: tool I/O capture -----------------------------------------------

    @filter.on_using_llm_tool()
    async def on_tool_call(self, event: AstrMessageEvent, *args, **kwargs):
        # AstrBot signature: (event, tool: FunctionTool, tool_args: dict | None)
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        tool = kwargs.get("tool", args[0] if args else None)
        tool_args = kwargs.get("tool_args", args[1] if len(args) > 1 else None)
        t_name = _tool_name(tool)
        if not t_name:
            return
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        payload = build_message("assistant", [tool_call_part(t_name, tool_args)])
        await self.ov.add_message(session_id, payload, **auth)

    @filter.on_llm_tool_respond()
    async def on_tool_respond(self, event: AstrMessageEvent, *args, **kwargs):
        # AstrBot signature: (event, tool, tool_args, tool_result: CallToolResult | None)
        if not self.cfg.capture_tool_io:
            return
        info = self._extract_event_info(event)
        venue_id = derive_venue(info["platform"], info["group_id"], info["sender_id"])
        if self.cfg.is_bypassed(venue_id):
            return
        tool = kwargs.get("tool", args[0] if args else None)
        tool_result = kwargs.get("tool_result", args[2] if len(args) > 2 else None)
        t_name = _tool_name(tool)
        if not t_name:
            return
        auth = self._auth(venue_id)
        session_id = derive_session_id(venue_id)
        payload = build_message(
            "assistant", [tool_result_part(t_name, _tool_result_text(tool_result))]
        )
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
        scope = get_effective_self_scope(self.cfg, info["group_id"])

        ov_user_id = derive_ov_user_id(
            self.cfg, info["platform"], info["group_id"], info["sender_id"]
        )
        api_key, fallback_uid = self._venue_auth.get(venue_id, ("", ""))
        if api_key and scope == "global" and self.cfg.ov_user_api_key:
            key_status = "user key (global self)"
        elif api_key:
            key_status = "minted user key"
        elif fallback_uid:
            key_status = f"trusted header (user={fallback_uid})"
        else:
            key_status = "no auth"

        peer_status = f"on ({self.cfg.peer_recall_scope})" if self.cfg.peer_enabled else "off"

        lines = [
            "OpenViking Memory Plugin v0.1.0",
            f"Server: {self.cfg.ov_base_url} ({'OK' if healthy else 'UNREACHABLE'})",
            f"Account: {self.ov.account_id or '(not set)'}",
            f"Self scope: {scope}",
            f"OV self user: {ov_user_id}",
            f"Auth: {key_status}",
            f"Peer memory: {peer_status}",
            f"Venue: {venue_id}",
            f"Pending: {sched['pending_messages']} msgs / ~{sched['pending_tokens']} tokens",
            f"Last commit: {_fmt_ts(sched['last_commit_ts'])}",
            f"Backfill: {bf_status}",
            f"Active peers: {len(self.presence.active(venue_id))}",
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
        await self._ensure_self_auth(venue_id, info["group_id"], ov_user_id)
        auth = self._auth(venue_id)

        await self.backfill.force_backfill(
            venue_id,
            info["platform"],
            info["group_id"],
            auth,
            event=event,
        )
        yield event.plain_result(f"Backfill triggered for {venue_id}")

    # -- lifecycle ------------------------------------------------------------

    async def terminate(self):
        await self.scheduler.flush_all()
        await self.ov.close()
        self.logger.info("[OV] plugin terminated, all sessions flushed")


_SELF_COMMANDS = ("ov_backfill", "ov-backfill", "ov_status", "ov-status")


def _is_self_command(text: str) -> bool:
    t = text.strip().lstrip("/").lower()
    return any(t == c or t.startswith(c + " ") for c in _SELF_COMMANDS)


def _tool_name(tool: Any) -> str:
    """Extract a tool's name from AstrBot's FunctionTool (or a fallback)."""
    if tool is None:
        return ""
    name = getattr(tool, "name", None)
    return str(name) if name else str(tool)


def _tool_result_text(result: Any) -> str:
    """Best-effort text from an AstrBot/MCP CallToolResult."""
    if result is None:
        return ""
    content = getattr(result, "content", None)
    if content is None:
        return str(result)
    blocks = content if isinstance(content, list) else [content]
    texts = []
    for b in blocks:
        t = getattr(b, "text", None)
        texts.append(t if t else (b if isinstance(b, str) else str(b)))
    joined = "\n".join(s for s in texts if s)
    return joined or str(result)


def _extract_image_captions(req: Any) -> list[str]:
    """Pull AstrBot image captions out of req.extra_user_content_parts."""
    captions: list[str] = []
    for part in getattr(req, "extra_user_content_parts", None) or []:
        captions.extend(parse_image_captions(getattr(part, "text", "") or ""))
    return captions


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
