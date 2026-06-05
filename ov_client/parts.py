"""
Serialize AstrBot messages into OV session message parts.

OV supports three part types: text, context, tool.
Images/files are represented as text placeholders since OV has no image part.
"""

from __future__ import annotations

import re
from typing import Any

# OV tool part status values: pending | running | completed | error.
MAX_TOOL_OUTPUT_CHARS = 4000

_IMAGE_CAPTION_RE = re.compile(r"<image_caption>(.*?)</image_caption>", re.S)


def user_text_part(
    text: str,
    sender_name: str = "",
    sender_id: str = "",
    is_group: bool = False,
    group_id: str = "",
) -> dict[str, Any]:
    # In a group, prefix the sender AND the group id. The group id matters under
    # global self_scope, where messages from every group land in one bot self —
    # without it the extracted memory loses which group it came from.
    if is_group:
        bits = []
        if group_id:
            bits.append(f"group:{group_id}")
        if sender_name:
            bits.append(f"{sender_name}({sender_id})" if sender_id else sender_name)
        if bits:
            text = f"[{' · '.join(bits)}] {text}"
    return {"type": "text", "text": text}


def assistant_text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_placeholder_part(filename_or_url: str) -> dict[str, Any]:
    return {"type": "text", "text": f"[image: {filename_or_url}]"}


def file_placeholder_part(filename: str) -> dict[str, Any]:
    return {"type": "text", "text": f"[file: {filename}]"}


def tool_call_part(tool_name: str, tool_input: Any, tool_id: str = "") -> dict[str, Any]:
    # OV's ToolPart wants tool_input as an object (Optional[dict]); AstrBot passes
    # tool_args as a dict already. Status "running" marks this as the call side.
    if isinstance(tool_input, dict):
        inp: Any = tool_input
    elif tool_input is None:
        inp = None
    else:
        inp = {"value": tool_input}
    part: dict[str, Any] = {"type": "tool", "tool_name": tool_name, "tool_status": "running"}
    if tool_id:
        part["tool_id"] = tool_id
    if inp is not None:
        part["tool_input"] = inp
    return part


def tool_result_part(tool_name: str, tool_output: Any, tool_id: str = "") -> dict[str, Any]:
    out = tool_output if isinstance(tool_output, str) else _safe_json(tool_output)
    if len(out) > MAX_TOOL_OUTPUT_CHARS:
        out = out[:MAX_TOOL_OUTPUT_CHARS] + "…[truncated]"
    part: dict[str, Any] = {
        "type": "tool",
        "tool_name": tool_name,
        "tool_output": out,
        "tool_status": "completed",
    }
    if tool_id:
        part["tool_id"] = tool_id
    return part


def image_caption_part(
    caption: str,
    sender_name: str = "",
    sender_id: str = "",
    is_group: bool = False,
    group_id: str = "",
) -> dict[str, Any]:
    """A text part carrying AstrBot's image-to-text caption, marked as image-derived."""
    bits = []
    if is_group and group_id:
        bits.append(f"group:{group_id}")
    if is_group and sender_name:
        bits.append(f"{sender_name}({sender_id})" if sender_id else sender_name)
    bits.append("image")
    return {"type": "text", "text": f"[{' · '.join(bits)}] {caption}"}


def parse_image_captions(text: str) -> list[str]:
    """Extract <image_caption>…</image_caption> bodies from a content-part text."""
    if not text:
        return []
    return [m.strip() for m in _IMAGE_CAPTION_RE.findall(text) if m.strip()]


def build_message(role: str, parts: list[dict]) -> dict[str, Any]:
    if len(parts) == 1 and parts[0]["type"] == "text":
        return {"role": role, "content": parts[0]["text"]}
    return {"role": role, "parts": parts}


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 3)


def _safe_json(obj: Any) -> str:
    import json

    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)
