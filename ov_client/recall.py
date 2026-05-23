"""
Semantic recall from OV and context injection formatting.

Mirrors the injection format from claude-code-memory-plugin auto-recall.mjs:
<openviking-context> envelope with token-budgeted full content + degraded URI hints.
"""

from __future__ import annotations

from typing import Any

from .client import OVClient
from .config import PluginConfig
from .identity import parse_venue_origin, venue_is_group
from .parts import estimate_tokens


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

    target_uri = f"viking://user/{ov_user_id}/memories"
    items = await client.search(
        query=query,
        target_uri=target_uri,
        limit=cfg.recall_limit,
        min_score=cfg.recall_min_score,
        api_key=api_key,
        user_id=user_id,
    )
    if not items:
        return None

    return await _build_injection_block(client, cfg, items, venue_id, api_key, user_id)


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
        "Relevant memories from OpenViking.",
    ]
    content_count = 0

    for item in items:
        score_pct = _clamp_score(item.get("score", 0))
        uri = item.get("uri", "")
        abstract = (item.get("abstract") or item.get("overview") or "").strip()

        header = f"[memory {score_pct}%"
        header += f" · {origin_label}"
        sender = _extract_sender(abstract)
        if is_group and sender:
            header += f" · from:{sender}"
        header += "]"

        if budget > 0:
            content = await _resolve_content(client, item, cfg, api_key, user_id)
            line = f"- {header} {content}"
            cost = estimate_tokens(line)
            if cost > budget and content_count > 0:
                lines.append(f"- {header} {uri}")
            else:
                lines.append(line)
                budget -= cost
                content_count += 1
        else:
            lines.append(f"- {header} {uri}")

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

    if cfg.recall_token_budget > 500 and uri:
        full = await client.read_content(uri, api_key=api_key, user_id=user_id)
        if full and full.strip():
            return full.strip()

    return abstract or uri


def _clamp_score(score: float) -> int:
    return max(0, min(100, int(score * 100)))


def _extract_sender(abstract: str) -> str:
    """Try to extract sender from text part prefix like '[张三(uid:123)] ...'."""
    if abstract.startswith("[") and "]" in abstract:
        bracket_end = abstract.index("]")
        inner = abstract[1:bracket_end]
        return inner
    return ""
