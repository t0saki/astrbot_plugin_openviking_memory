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
