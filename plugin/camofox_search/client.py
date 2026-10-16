"""Thin HTTP client for the Camofox browser server's REST API.

Camofox (https://github.com/jo-inc/camofox-browser) is a self-hosted
anti-detection browser (Camoufox engine) exposing a tab-oriented REST API:
``POST /tabs`` create, ``POST /tabs/{id}/navigate``, ``POST /tabs/{id}/evaluate``,
``DELETE /sessions/{userId}`` teardown. Requests are authenticated with
``Authorization: Bearer <CAMOFOX_API_KEY>`` whenever a key is configured.

Response shapes here were verified against a live camofox-browser 2.4.8:
``POST /tabs`` needs ``userId`` + ``sessionKey`` + ``url`` and returns
``{"tabId": ...}``; ``POST /tabs/{id}/evaluate`` returns
``{"ok": true, "result": <decoded value>, "resultType": ..., "truncated": ...}``
(the ``result`` may arrive as a JSON *string* on other versions, so it is
normalized by :func:`parse_eval_result`).
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional

import requests

from .settings import camofox_api_key, camofox_base_url, camofox_timeout

logger = logging.getLogger(__name__)


class CamofoxError(RuntimeError):
    """A Camofox request failed (HTTP error, malformed payload, protocol violation)."""


class CamofoxConnectionError(CamofoxError):
    """The Camofox server is unreachable."""


class CamofoxAuthError(CamofoxError):
    """Camofox rejected the credentials (HTTP 403)."""


def parse_eval_result(value: Any) -> Any:
    """Normalize an ``/evaluate`` result.

    Some Camofox versions return the JS value as an already-decoded JSON
    structure; others return a JSON string. Parse only when it looks like a
    JSON object/array; leave every other raw value (string, number, bool) as is.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in {"{", "["}:
            try:
                return json.loads(stripped)
            except ValueError:
                return value
    return value


class CamofoxClient:
    """Minimal Camofox client: tab creation, navigation, JS evaluation, session cleanup."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.base_url = (base_url or camofox_base_url()).rstrip("/")
        self.api_key = camofox_api_key() if api_key is None else api_key
        self.timeout = float(timeout or camofox_timeout())

    # ---- HTTP plumbing ----

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[dict] = None,
        params: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(
                method, url, headers=self._headers(), json=json_body,
                params=params, timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise CamofoxConnectionError(
                f"Cannot reach Camofox at {self.base_url}: {exc}") from exc
        if resp.status_code == 403:
            raise CamofoxAuthError(
                "Camofox rejected the request (HTTP 403). Check CAMOFOX_API_KEY.")
        if resp.status_code >= 400:
            raise CamofoxError(
                f"Camofox {method.upper()} {path} failed: HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise CamofoxError(
                f"Camofox {method.upper()} {path} returned a non-JSON response") from exc

    # ---- Session / tab lifecycle ----

    @staticmethod
    def new_session_id() -> str:
        """Unique per-call ``userId`` so concurrent calls never share a tab."""
        return f"camofox_search_{uuid.uuid4().hex[:12]}"

    def create_tab(self, user_id: str, url: str, session_key: str = "camofox_search") -> str:
        """Create a tab for ``user_id`` opened at ``url``; returns the tab id."""
        data = self._request("post", "/tabs", json_body={
            "userId": user_id, "sessionKey": session_key, "url": url})
        tab_id = data.get("tabId") if isinstance(data, dict) else None
        if not tab_id:
            raise CamofoxError(f"Camofox created no tab (response: {data!r})")
        return str(tab_id)

    def navigate(self, tab_id: str, user_id: str, url: str) -> dict:
        """Navigate an existing tab (kept for callers that reopen a tab)."""
        data = self._request("post", f"/tabs/{tab_id}/navigate",
                             json_body={"userId": user_id, "url": url})
        return data if isinstance(data, dict) else {}

    def evaluate(
        self,
        tab_id: str,
        user_id: str,
        expression: str,
        timeout: Optional[float] = None,
    ) -> Any:
        """Evaluate ``expression`` in the tab and return the normalized result."""
        data = self._request(
            "post", f"/tabs/{tab_id}/evaluate",
            json_body={"userId": user_id, "expression": expression}, timeout=timeout,
        )
        return parse_eval_result(data.get("result") if isinstance(data, dict) else None)

    def close_session(self, user_id: str) -> None:
        """Best-effort session teardown — never raises (cleanup must not mask results)."""
        try:
            self._request("delete", f"/sessions/{user_id}", timeout=15)
        except Exception as exc:  # noqa: BLE001 - deliberate best-effort cleanup
            logger.debug("Camofox session cleanup failed for %s: %s", user_id, exc)
