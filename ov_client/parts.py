"""
Serialize AstrBot messages into OV session message parts.

OV supports three part types: text, context, tool.
Images/files are represented as text placeholders since OV has no image part.
"""

from __future__ import annotations

from typing import Any


def user_text_part(
    text: str,
    sender_name: str = "",
    sender_id: str = "",
    is_group: bool = False,
) -> dict[str, Any]:
    if is_group and sender_name:
        label = f"[{sender_name}({sender_id})] " if sender_id else f"[{sender_name}] "
        text = label + text
    return {"type": "text", "text": text}


def assistant_text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def image_placeholder_part(filename_or_url: str) -> dict[str, Any]:
    return {"type": "text", "text": f"[image: {filename_or_url}]"}


def file_placeholder_part(filename: str) -> dict[str, Any]:
    return {"type": "text", "text": f"[file: {filename}]"}


def tool_call_part(tool_name: str, tool_input: Any) -> dict[str, Any]:
    inp = tool_input if isinstance(tool_input, str) else _safe_json(tool_input)
    return {
        "type": "tool",
        "tool_name": tool_name,
        "tool_input": inp,
    }


def tool_result_part(tool_name: str, tool_output: Any) -> dict[str, Any]:
    out = tool_output if isinstance(tool_output, str) else _safe_json(tool_output)
    return {
        "type": "tool",
        "tool_name": tool_name,
        "tool_output": out,
    }


def fanout_text_part(
    text: str,
    origin_venue_id: str,
    sender_name: str = "",
    sender_id: str = "",
) -> dict[str, Any]:
    """Text part with origin marker for fanout writes."""
    prefix = f"[from {origin_venue_id}"
    if sender_name:
        prefix += f" · {sender_name}({sender_id})" if sender_id else f" · {sender_name}"
    prefix += "] "
    return {"type": "text", "text": prefix + text}


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
