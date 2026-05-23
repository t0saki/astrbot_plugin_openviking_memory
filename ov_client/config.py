"""
Configuration loader for the AstrBot OpenViking memory plugin.

Resolution priority: env var > AstrBotConfig > built-in default.
"""

from __future__ import annotations

import os
import re
from typing import Any


_DEFAULTS: dict[str, Any] = {
    "ov_base_url": "http://localhost:1933",
    "ov_admin_api_key": "",
    "ov_account_id": "",
    "isolation_mode": "venue_user",
    "isolation_overrides": {},
    "global_user_id": "astrbot-global",
    "auto_recall_enabled": True,
    "recall_limit": 8,
    "recall_min_score": 0.35,
    "recall_token_budget": 2000,
    "commit_message_threshold": 20,
    "commit_token_threshold": 4096,
    "commit_idle_seconds": 1800,
    "ingest_attachments": False,
    "capture_tool_io": True,
    "fanout_member_cache_ttl_seconds": 3600,
    "backfill_on_first_seen": True,
    "backfill_max_messages": 500,
    "backfill_max_age_days": 30,
    "backfill_batch_size": 20,
    "backfill_throttle_ms": 200,
    "bypass_patterns": [],
}

_BOOL_TRUE = {"1", "true", "yes", "on"}

_ENV_PREFIX = "OPENVIKING_ASTRBOT_"


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

        self._bypass_re: list[re.Pattern] | None = None

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
