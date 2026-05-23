"""
Semantic recall from OV and context injection formatting.

Mirrors the CC memory plugin's auto-recall.mjs: multi-source find,
client-side ranking with boosts, token-budgeted injection block.
"""

from __future__ import annotations

import re
from typing import Any

from .client import OVClient
from .config import PluginConfig
from .identity import parse_venue_origin, venue_is_group
from .parts import estimate_tokens

_PREFERENCE_RE = re.compile(
    r"prefer|preference|favorite|favourite|like|偏好|喜欢|爱好|更倾向", re.I
)
_TEMPORAL_RE = re.compile(
    r"when|what time|date|day|month|year|yesterday|today|tomorrow|last|next"
    r"|什么时候|何时|哪天|几月|几年|昨天|今天|明天",
    re.I,
)
_TOKEN_RE = re.compile(r"[a-z0-9一-鿿]{2,}", re.I)
_STOPWORDS = {
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "did",
    "does",
    "is",
    "are",
    "was",
    "were",
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "your",
    "you",
}

_space_cache: dict[str, str] = {}


async def _resolve_user_space(
    client: OVClient,
    api_key: str | None,
    user_id: str | None,
) -> str:
    cache_key = f"{api_key or ''}::{user_id or ''}"
    if cache_key in _space_cache:
        return _space_cache[cache_key]
    space = await client.resolve_user_space(api_key=api_key, user_id=user_id)
    _space_cache[cache_key] = space
    return space


def _build_query_profile(query: str) -> dict:
    tokens = [t for t in _TOKEN_RE.findall(query.lower()) if t not in _STOPWORDS]
    return {
        "tokens": tokens,
        "wants_preference": bool(_PREFERENCE_RE.search(query)),
        "wants_temporal": bool(_TEMPORAL_RE.search(query)),
    }


def _lexical_overlap_boost(tokens: list[str], text: str) -> float:
    if not tokens or not text:
        return 0.0
    haystack = f" {text.lower()} "
    matched = sum(1 for t in tokens[:8] if t in haystack)
    return min(0.2, (matched / min(len(tokens), 4)) * 0.2)


def _rank_item(item: dict, profile: dict) -> float:
    base = max(0.0, min(1.0, item.get("score", 0)))
    abstract = (item.get("abstract") or item.get("overview") or "").strip()
    uri = (item.get("uri") or "").lower()

    leaf_boost = 0.12 if (item.get("level") == 2 or uri.endswith(".md")) else 0.0
    event_boost = 0.1 if profile["wants_temporal"] and "/events/" in uri else 0.0
    pref_boost = 0.08 if profile["wants_preference"] and "/preferences/" in uri else 0.0
    overlap = _lexical_overlap_boost(profile["tokens"], f"{uri} {abstract}")
    return base + leaf_boost + event_boost + pref_boost + overlap


def _dedup(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        uri = it.get("uri", "")
        cat = (it.get("category") or "").lower()
        if cat in ("events", "cases") or "/events/" in uri or "/cases/" in uri:
            key = f"uri:{uri}"
        else:
            key = (it.get("abstract") or it.get("overview") or "").strip().lower() or f"uri:{uri}"
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out


async def recall_and_format(
    client: OVClient,
    cfg: PluginConfig,
    query: str,
    venue_id: str,
    ov_user_id: str,
    api_key: str | None = None,
    user_id: str | None = None,
) -> str | None:
    if not cfg.auto_recall_enabled or not query.strip():
        return None

    space = await _resolve_user_space(client, api_key, user_id)
    target_uri = f"viking://user/{space}/memories"

    per_source_limit = max(cfg.recall_limit * 2, 8)
    items = await client.find(
        query=query,
        target_uri=target_uri,
        limit=per_source_limit,
        api_key=api_key,
        user_id=user_id,
    )
    if not items:
        return None

    profile = _build_query_profile(query)
    filtered = [it for it in items if it.get("score", 0) >= cfg.recall_min_score]
    filtered.sort(key=lambda it: _rank_item(it, profile), reverse=True)
    picked = _dedup(filtered)[: cfg.recall_limit]

    if not picked:
        return None

    return await _build_injection_block(client, cfg, picked, venue_id, api_key, user_id)


async def _build_injection_block(
    client: OVClient,
    cfg: PluginConfig,
    items: list[dict[str, Any]],
    venue_id: str,
    api_key: str | None,
    user_id: str | None = None,
) -> str | None:
    budget = cfg.recall_token_budget
    is_group = venue_is_group(venue_id)
    origin_label = parse_venue_origin(venue_id)

    lines = [
        "<openviking-context>",
        "Relevant context from OpenViking. Use the read MCP tool to expand URIs.",
    ]
    content_count = 0

    for item in items:
        score_pct = max(0, min(100, int(item.get("score", 0) * 100)))
        uri = item.get("uri", "")
        abstract = (item.get("abstract") or item.get("overview") or "").strip()

        header = f"[memory {score_pct}%"
        header += f" · {origin_label}"
        sender = _extract_sender(abstract)
        if is_group and sender:
            header += f" · from:{sender}"
        header += "]"

        uri_line = f"- {header} {uri}"

        if budget > 0:
            content = await _resolve_content(client, item, cfg, api_key, user_id)
            content_line = f"- {header} {content}"
            cost = estimate_tokens(content_line)
            if cost > budget and content_count > 0:
                lines.append(uri_line)
            else:
                lines.append(content_line)
                budget -= cost
                content_count += 1
        else:
            lines.append(uri_line)

    lines.append("</openviking-context>")
    return "\n".join(lines)


async def _resolve_content(
    client: OVClient,
    item: dict[str, Any],
    cfg: PluginConfig,
    api_key: str | None,
    user_id: str | None = None,
) -> str:
    uri = item.get("uri", "")
    abstract = (item.get("abstract") or item.get("overview") or "").strip()

    if item.get("level") == 2 and uri:
        full = await client.read_content(uri, api_key=api_key, user_id=user_id)
        if full and full.strip():
            return full.strip()

    return abstract or uri


def _extract_sender(abstract: str) -> str:
    if abstract.startswith("[") and "]" in abstract:
        bracket_end = abstract.index("]")
        return abstract[1:bracket_end]
    return ""
