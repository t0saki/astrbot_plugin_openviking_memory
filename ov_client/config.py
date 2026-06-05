"""
Configuration loader for the AstrBot OpenViking memory plugin.

Resolution priority: env var > AstrBotConfig > built-in default.

Targets peer-contract OpenViking servers only (PR #2236+): identity is derived
from the Bearer key, X-OpenViking-* headers are sent only in trusted_mode.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

logger = logging.getLogger("astrbot_plugin_openviking_memory")

_DEFAULTS: dict[str, Any] = {
    "ov_base_url": "http://localhost:1933",
    "ov_admin_api_key": "",
    "ov_user_api_key": "",
    "ov_account_id": "",
    "self_scope": "global",
    "global_user_id": "astrbot-global",
    "isolation_overrides": {},
    "peer_enabled": True,
    "peer_recall_scope": "speaker_plus_active",
    "peer_recall_active_window": 5,
    "trusted_mode": False,
    "auto_recall_enabled": True,
    "recall_limit": 8,
    "recall_min_score": 0.35,
    "recall_token_budget": 2000,
    "commit_message_threshold": 20,
    "commit_token_threshold": 4096,
    "commit_idle_seconds": 1800,
    "ingest_attachments": False,
    "capture_tool_io": True,
    "backfill_on_first_seen": True,
    "backfill_max_messages": 500,
    "backfill_max_age_days": 30,
    "backfill_batch_size": 20,
    "backfill_throttle_ms": 200,
    "bypass_patterns": [],
}

_BOOL_TRUE = {"1", "true", "yes", "on"}

_ENV_PREFIX = "OPENVIKING_ASTRBOT_"

VALID_SELF_SCOPES = {"global", "venue"}

# Legacy isolation_mode values → new self_scope axis. venue_user_fanout is
# subsumed by global+peer (cross-venue per-person memory is native now).
_LEGACY_SELF_SCOPE = {
    "global_user": "global",
    "venue_user": "venue",
    "venue_user_fanout": "global",
}


def normalize_self_scope(value: Any, *, context: str = "") -> str:
    """Map a self_scope or legacy isolation_mode value to global|venue."""
    raw = str(value or "").strip()
    if raw in VALID_SELF_SCOPES:
        return raw
    if raw in _LEGACY_SELF_SCOPE:
        mapped = _LEGACY_SELF_SCOPE[raw]
        logger.warning(
            "[OV] deprecated isolation value %r%s → self_scope=%r",
            raw,
            f" ({context})" if context else "",
            mapped,
        )
        return mapped
    return "global"


def _env(key: str) -> str | None:
    return os.environ.get(f"{_ENV_PREFIX}{key.upper()}")


def _cast(key: str, raw: Any) -> Any:
    default = _DEFAULTS.get(key)
    if default is None:
        return raw
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in _BOOL_TRUE
    if isinstance(default, int):
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default
    if isinstance(default, list) and isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    if isinstance(default, dict) and isinstance(raw, str):
        import json

        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return default
    return raw


class PluginConfig:
    """Immutable-ish config snapshot built from AstrBotConfig + env."""

    def __init__(self, astrbot_config: dict | None = None):
        astrbot_config = astrbot_config or {}
        self._data: dict[str, Any] = {}
        for key, default in _DEFAULTS.items():
            env_val = _env(key)
            if env_val is not None:
                self._data[key] = _cast(key, env_val)
            elif key in astrbot_config:
                self._data[key] = _cast(key, astrbot_config[key])
            else:
                self._data[key] = default

        self._apply_legacy(astrbot_config)
        self._normalize_isolation_overrides()
        self._bypass_re: list[re.Pattern] | None = None

    def _apply_legacy(self, astrbot_config: dict) -> None:
        """Map a legacy isolation_mode onto self_scope when self_scope is unset."""
        self_scope_set = _env("self_scope") is not None or "self_scope" in astrbot_config
        legacy = _env("isolation_mode")
        if legacy is None:
            legacy = astrbot_config.get("isolation_mode")
        if not self_scope_set and legacy:
            self._data["self_scope"] = normalize_self_scope(legacy, context="legacy isolation_mode")
        else:
            self._data["self_scope"] = normalize_self_scope(self._data["self_scope"])

    def _normalize_isolation_overrides(self) -> None:
        overrides = self._data.get("isolation_overrides") or {}
        if not isinstance(overrides, dict):
            self._data["isolation_overrides"] = {}
            return
        self._data["isolation_overrides"] = {
            str(group): normalize_self_scope(val, context=f"override {group}")
            for group, val in overrides.items()
        }

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(f"PluginConfig has no attribute {name!r}")

    @property
    def bypass_regexes(self) -> list[re.Pattern]:
        if self._bypass_re is None:
            self._bypass_re = [re.compile(p) for p in self.bypass_patterns]
        return self._bypass_re

    def is_bypassed(self, venue_id: str) -> bool:
        return any(r.search(venue_id) for r in self.bypass_regexes)
