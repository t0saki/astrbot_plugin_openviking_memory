"""Tests for the OpenViking peer-contract adaptation."""

from __future__ import annotations

import asyncio
import json

import httpx

from ov_client.backfill import BackfillManager
from ov_client.client import PEER_MEMORY_POLICY, OVClient
from ov_client.commit_scheduler import CommitScheduler
from ov_client.config import PluginConfig, normalize_self_scope
from ov_client.identity import get_effective_self_scope, safe_peer_id
from ov_client.parts import (
    MAX_TOOL_OUTPUT_CHARS,
    clean_onebot_text,
    image_caption_part,
    image_placeholder_part,
    parse_image_captions,
    tool_call_part,
    tool_result_part,
    user_text_part,
)
from ov_client.presence import PresenceTracker
from ov_client.recall import _build_recall_targets

# -- safe_peer_id ---------------------------------------------------------


def test_safe_peer_id_strips_path_separators():
    assert safe_peer_id("123") == "123"
    assert safe_peer_id("a/b") == "ab"
    assert safe_peer_id("a\\b") == "ab"


def test_safe_peer_id_empty_to_none():
    assert safe_peer_id("") is None
    assert safe_peer_id("   ") is None
    assert safe_peer_id(None) is None


# -- self_scope mapping ---------------------------------------------------


def test_normalize_self_scope():
    assert normalize_self_scope("global") == "global"
    assert normalize_self_scope("venue") == "venue"
    assert normalize_self_scope("global_user") == "global"
    assert normalize_self_scope("venue_user") == "venue"
    assert normalize_self_scope("venue_user_fanout") == "global"
    assert normalize_self_scope("nonsense") == "global"


def test_config_legacy_isolation_mode_maps_to_self_scope():
    assert PluginConfig({"isolation_mode": "venue_user"}).self_scope == "venue"
    assert PluginConfig({"isolation_mode": "venue_user_fanout"}).self_scope == "global"
    # explicit self_scope wins over legacy isolation_mode
    assert (
        PluginConfig({"self_scope": "venue", "isolation_mode": "global_user"}).self_scope == "venue"
    )


def test_config_overrides_mapped():
    cfg = PluginConfig({"isolation_overrides": {"123": "venue_user", "456": "venue"}})
    assert get_effective_self_scope(cfg, "123") == "venue"
    assert get_effective_self_scope(cfg, "456") == "venue"
    assert get_effective_self_scope(cfg, "789") == "global"  # default


def test_removed_keys_absent():
    cfg = PluginConfig({})
    for missing in ("ov_agent_id", "isolation_mode", "fanout_member_cache_ttl_seconds"):
        raised = False
        try:
            getattr(cfg, missing)
        except AttributeError:
            raised = True
        assert raised, f"{missing} should no longer be a config attribute"


# -- recall target building -----------------------------------------------


def test_recall_targets_speaker_plus_active_sanitizes_and_dedupes():
    cfg = PluginConfig({"peer_enabled": True, "peer_recall_scope": "speaker_plus_active"})
    targets, peers = _build_recall_targets(cfg, "bob", "alice", ["carol", "da/ve", "alice"])
    assert targets[0] == "viking://user/bob/memories"
    assert peers == ["alice", "carol", "dave"]  # sanitized + deduped (alice once)
    assert "viking://user/bob/peers/alice/memories" in targets
    assert "viking://user/bob/peers/dave/memories" in targets


def test_recall_targets_speaker_only():
    cfg = PluginConfig({"peer_recall_scope": "speaker"})
    targets, peers = _build_recall_targets(cfg, "bob", "alice", ["carol"])
    assert peers == ["alice"]
    assert targets == [
        "viking://user/bob/memories",
        "viking://user/bob/peers/alice/memories",
    ]


def test_recall_targets_none_scope():
    cfg = PluginConfig({"peer_recall_scope": "none"})
    targets, peers = _build_recall_targets(cfg, "bob", "alice", ["carol"])
    assert peers == []
    assert targets == ["viking://user/bob/memories"]


def test_recall_targets_peer_disabled():
    cfg = PluginConfig({"peer_enabled": False})
    targets, peers = _build_recall_targets(cfg, "bob", "alice", ["carol"])
    assert peers == []
    assert targets == ["viking://user/bob/memories"]


# -- presence -------------------------------------------------------------


def test_presence_most_recent_first_excludes_speaker():
    p = PresenceTracker(window=3, ttl_seconds=1000)
    p.record("g", "a", now=1.0)
    p.record("g", "b", now=2.0)
    p.record("g", "c", now=3.0)
    assert p.active("g", exclude="c", now=4.0) == ["b", "a"]
    assert p.active("g", now=4.0) == ["c", "b", "a"]


def test_presence_window_cap():
    p = PresenceTracker(window=2, ttl_seconds=1000)
    for i, name in enumerate(["a", "b", "c", "d"]):
        p.record("g", name, now=float(i))
    assert p.active("g", exclude="d", now=10.0) == ["c", "b"]


def test_presence_ttl_drops_stale():
    p = PresenceTracker(window=5, ttl_seconds=10)
    p.record("g", "old", now=0.0)
    p.record("g", "new", now=100.0)
    assert p.active("g", now=105.0) == ["new"]


def test_presence_window_zero_disables():
    p = PresenceTracker(window=0)
    p.record("g", "a")
    assert p.active("g") == []


# -- commit scheduler policy ----------------------------------------------


def test_scheduler_memory_policy_follows_peer_enabled():
    # Constructed inside a running loop (CommitScheduler creates an asyncio.Lock,
    # which on Python 3.9 binds to the running loop at construction time).
    async def build(peer_enabled):
        return CommitScheduler(client=object(), cfg=PluginConfig({"peer_enabled": peer_enabled}))

    on = asyncio.run(build(True))
    assert on._memory_policy == PEER_MEMORY_POLICY
    off = asyncio.run(build(False))
    assert off._memory_policy is None


# -- client headers + body shaping ----------------------------------------


def test_headers_drop_identity_in_api_key_mode():
    c = OVClient("http://x", api_key="k", account_id="acme", trusted_mode=False)
    h = c._headers(user_id="bob")
    assert h["Authorization"] == "Bearer k"
    assert "X-OpenViking-Account" not in h
    assert "X-OpenViking-User" not in h
    assert "X-OpenViking-Agent" not in h


def test_headers_assert_identity_in_trusted_mode():
    c = OVClient("http://x", api_key="k", account_id="acme", trusted_mode=True)
    h = c._headers(user_id="bob")
    assert h["X-OpenViking-Account"] == "acme"
    assert h["X-OpenViking-User"] == "bob"
    assert "X-OpenViking-Agent" not in h


def _run_with_mock(handler, coro_factory):
    transport = httpx.MockTransport(handler)

    async def run():
        c = OVClient("http://x", api_key="k")
        c._http = httpx.AsyncClient(transport=transport)
        try:
            return await coro_factory(c)
        finally:
            await c.close()

    return asyncio.run(run())


def test_add_message_injects_peer_id():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {}})

    ok = _run_with_mock(
        handler,
        lambda c: c.add_message("sess", {"role": "user", "content": "hi"}, peer_id="alice"),
    )
    assert ok
    assert captured["body"]["peer_id"] == "alice"
    assert captured["body"]["role"] == "user"


def test_add_message_omits_peer_id_when_none():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {}})

    _run_with_mock(
        handler,
        lambda c: c.add_message("sess", {"role": "assistant", "content": "hi"}),
    )
    assert "peer_id" not in captured["body"]


def test_commit_sends_memory_policy():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {"ok": True}})

    _run_with_mock(handler, lambda c: c.commit_session("sess", memory_policy=PEER_MEMORY_POLICY))
    assert captured["body"]["memory_policy"] == PEER_MEMORY_POLICY


def test_commit_empty_body_without_policy():
    captured: dict = {}

    def handler(request):
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"result": {}})

    _run_with_mock(handler, lambda c: c.commit_session("sess"))
    assert captured["body"] == {}


# -- message parts (group id) ---------------------------------------------


def test_user_text_part_includes_group_in_group_chat():
    part = user_text_part("hello", "Alice", "12345", is_group=True, group_id="732524901")
    assert part["text"] == "[group:732524901 · Alice(12345)] hello"


def test_user_text_part_group_without_sender():
    part = user_text_part("hi", "", "", is_group=True, group_id="732524901")
    assert part["text"] == "[group:732524901] hi"


def test_user_text_part_dm_has_no_prefix():
    part = user_text_part("hi", "Alice", "12345", is_group=False)
    assert part["text"] == "hi"


# -- tool parts (OV ToolPart shape) ---------------------------------------


def test_tool_call_part_keeps_dict_input_and_running_status():
    part = tool_call_part("search", {"q": "weather"})
    assert part["type"] == "tool"
    assert part["tool_name"] == "search"
    assert part["tool_input"] == {"q": "weather"}  # dict preserved, not stringified
    assert part["tool_status"] == "running"


def test_tool_call_part_wraps_non_dict_input():
    assert tool_call_part("t", "raw")["tool_input"] == {"value": "raw"}
    assert "tool_input" not in tool_call_part("t", None)


def test_tool_result_part_completed_and_truncated():
    part = tool_result_part("search", "x" * (MAX_TOOL_OUTPUT_CHARS + 500))
    assert part["tool_status"] == "completed"
    assert part["tool_output"].endswith("…[truncated]")
    assert len(part["tool_output"]) <= MAX_TOOL_OUTPUT_CHARS + 20


# -- image caption --------------------------------------------------------


def test_parse_image_captions():
    text = "<image_caption>a cat on a mat</image_caption>"
    assert parse_image_captions(text) == ["a cat on a mat"]
    assert parse_image_captions("[Image Attachment: path /x]") == []
    assert parse_image_captions("") == []


def test_image_caption_part_group_and_dm():
    g = image_caption_part("a cat", "Alice", "12345", is_group=True, group_id="732524901")
    assert g["text"] == "[group:732524901 · Alice(12345) · image] a cat"
    dm = image_caption_part("a cat", is_group=False)
    assert dm["text"] == "[image] a cat"


# -- OneBot CQ-code cleanup -----------------------------------------------


def test_clean_onebot_text_image_strips_url_noise():
    raw = (
        "[CQ:image,summary=&#91;？&#93;,file=A.jpg,"
        "url=https://x.qq.com/d?appid=1&amp;fileid=Eh,file_size=2027]"
    )
    assert clean_onebot_text(raw) == "[image]"


def test_clean_onebot_text_at_and_face_and_text():
    assert clean_onebot_text("[CQ:at,qq=123] hi [CQ:face,id=4]") == "@123 hi"
    assert clean_onebot_text("[CQ:at,qq=all] all") == "@all all"


def test_image_placeholder_is_bare_marker():
    assert image_placeholder_part("https://ephemeral/url")["text"] == "[image]"


# -- backfill dedup -------------------------------------------------------


def test_backfill_maybe_trigger_dedups_concurrent():
    store: dict = {}

    async def kv_get(k, d=None):
        return store.get(k, d)

    async def kv_put(k, v):
        store[k] = v

    async def run():
        bm = BackfillManager(
            client=object(),
            cfg=PluginConfig({}),
            kv_get=kv_get,
            kv_put=kv_put,
            kv_prefix="t_",
        )
        started = []

        async def fake_run(venue_id, platform, group_id, auth, event):
            started.append(venue_id)
            await asyncio.sleep(0.01)
            bm._running.discard(venue_id)

        bm._run_backfill = fake_run
        await asyncio.gather(
            bm.maybe_trigger("v", "p", "g", {}),
            bm.maybe_trigger("v", "p", "g", {}),
        )
        await asyncio.sleep(0.05)
        return started

    assert asyncio.run(run()) == ["v"]
