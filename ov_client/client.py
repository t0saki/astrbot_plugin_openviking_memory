"""
Async HTTP client for the OpenViking server API.

All auth uses Authorization: Bearer header (no X-Api-Key).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("astrbot_plugin_openviking_memory")

DEFAULT_TIMEOUT = 15.0


class OVClient:
    """Thin wrapper over OV REST endpoints needed by the plugin."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        account_id: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.account_id = account_id
        self._http = httpx.AsyncClient(timeout=timeout)

    def _headers(self, api_key: str | None = None) -> dict[str, str]:
        key = api_key or self.api_key
        h: dict[str, str] = {"Content-Type": "application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if self.account_id:
            h["X-OpenViking-Account"] = self.account_id
        return h

    async def close(self):
        await self._http.aclose()

    # -- health ---------------------------------------------------------------

    async def health(self) -> bool:
        try:
            r = await self._http.get(
                f"{self.base_url}/health",
                headers=self._headers(),
            )
            return r.status_code == 200
        except Exception:
            return False

    # -- admin: create user ---------------------------------------------------

    async def create_user(
        self,
        user_id: str,
        admin_api_key: str,
    ) -> dict[str, Any] | None:
        r = await self._http.post(
            f"{self.base_url}/api/v1/admin/accounts/{quote(self.account_id)}/users",
            headers=self._headers(api_key=admin_api_key),
            json={"user_id": user_id, "role": "user"},
        )
        if r.status_code == 200:
            body = r.json()
            return body.get("result", body)
        logger.warning("create_user %s failed: %d %s", user_id, r.status_code, r.text[:200])
        return None

    # -- sessions -------------------------------------------------------------

    async def add_message(
        self,
        session_id: str,
        payload: dict[str, Any],
        api_key: str | None = None,
    ) -> bool:
        r = await self._http.post(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}/messages",
            headers=self._headers(api_key=api_key),
            json=payload,
        )
        if r.status_code != 200:
            logger.warning("add_message %s failed: %d", session_id, r.status_code)
        return r.status_code == 200

    async def commit_session(
        self,
        session_id: str,
        api_key: str | None = None,
    ) -> dict[str, Any] | None:
        r = await self._http.post(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}/commit",
            headers=self._headers(api_key=api_key),
            json={},
        )
        if r.status_code == 200:
            return r.json().get("result")
        logger.warning("commit_session %s failed: %d", session_id, r.status_code)
        return None

    async def get_session(
        self,
        session_id: str,
        api_key: str | None = None,
        auto_create: bool = False,
    ) -> dict[str, Any] | None:
        q = "?auto_create=true" if auto_create else ""
        r = await self._http.get(
            f"{self.base_url}/api/v1/sessions/{quote(session_id)}{q}",
            headers=self._headers(api_key=api_key),
        )
        if r.status_code == 200:
            return r.json().get("result")
        return None

    # -- search ---------------------------------------------------------------

    async def search(
        self,
        query: str,
        target_uri: str = "",
        limit: int = 8,
        min_score: float = 0.35,
        session_id: str = "",
        api_key: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "query": query,
            "limit": limit,
            "score_threshold": min_score,
        }
        if target_uri:
            body["target_uri"] = target_uri
        if session_id:
            body["session_id"] = session_id
        r = await self._http.post(
            f"{self.base_url}/api/v1/search/search",
            headers=self._headers(api_key=api_key),
            json=body,
        )
        if r.status_code == 200:
            result = r.json().get("result", [])
            return result if isinstance(result, list) else []
        logger.warning("search failed: %d", r.status_code)
        return []

    async def read_content(
        self,
        uri: str,
        api_key: str | None = None,
    ) -> str | None:
        r = await self._http.get(
            f"{self.base_url}/api/v1/content/read",
            params={"uri": uri},
            headers=self._headers(api_key=api_key),
        )
        if r.status_code == 200:
            result = r.json().get("result")
            return result if isinstance(result, str) else None
        return None

    # -- resources ------------------------------------------------------------

    async def add_resource(
        self,
        path: str,
        to_uri: str,
        api_key: str | None = None,
        wait: bool = False,
    ) -> dict[str, Any] | None:
        r = await self._http.post(
            f"{self.base_url}/api/v1/resources",
            headers=self._headers(api_key=api_key),
            json={"path": path, "to": to_uri, "wait": wait},
        )
        if r.status_code == 200:
            return r.json().get("result")
        logger.warning("add_resource failed: %d", r.status_code)
        return None
