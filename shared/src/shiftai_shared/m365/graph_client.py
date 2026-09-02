"""Microsoft Graph HTTP client: MSAL client-credential auth, timeout, retry/backoff.

Credentials come from environment configuration only. 429/5xx and transport errors
retry with exponential backoff; 4xx fail permanently.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from shiftai_shared.config import SharedSettings
from shiftai_shared.resilience import PermanentError, TransientError, with_retries

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GraphError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Graph {status_code}: {message}")
        self.status_code = status_code


class _MsalTokenProvider:
    def __init__(self, tenant_id: str, client_id: str, client_secret: str) -> None:
        import msal

        self._app = msal.ConfidentialClientApplication(
            client_id,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
            client_credential=client_secret,
        )

    def token(self) -> str:
        result = self._app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        if "access_token" not in result:
            raise PermanentError(f"Graph auth failed: {result.get('error_description')}")
        return str(result["access_token"])


class GraphClient:
    """Thin, testable Graph client. Inject ``http`` and/or ``token_provider`` in tests."""

    def __init__(
        self,
        settings: SharedSettings | None = None,
        *,
        http: httpx.Client | None = None,
        token_provider: Any | None = None,
        timeout_s: float = 30.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._http = http or httpx.Client(timeout=timeout_s)
        self._timeout_s = timeout_s
        self._retries = retries
        self._sleep = sleep
        if token_provider is not None:
            self._tokens = token_provider
        else:
            if settings is None or not (
                settings.graph_tenant_id
                and settings.graph_client_id
                and settings.graph_client_secret
            ):
                raise ValueError(
                    "GraphClient needs GRAPH_TENANT_ID, GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET "
                    "or an injected token_provider"
                )
            self._tokens = _MsalTokenProvider(
                settings.graph_tenant_id,
                settings.graph_client_id,
                settings.graph_client_secret.get_secret_value(),
            )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"

        def call() -> httpx.Response:
            hdrs = {"Authorization": f"Bearer {self._tokens.token()}"}
            if headers:
                hdrs.update(headers)
            try:
                response = self._http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    content=content,
                    headers=hdrs,
                    timeout=self._timeout_s,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise TransientError(str(exc)) from exc
            if response.status_code in _RETRYABLE_STATUS:
                raise TransientError(f"Graph {response.status_code}: {response.text[:200]}")
            if response.status_code >= 400:
                raise GraphError(response.status_code, response.text[:500])
            return response

        return with_retries(call, retries=self._retries, sleep=self._sleep)

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        data: dict[str, Any] = self.request("GET", path, params=params).json()
        return data

    def post_json(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = self.request("POST", path, json_body=json_body).json()
        return data

    def put_content(
        self, path: str, content: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        response = self.request(
            "PUT", path, content=content, headers={"Content-Type": content_type}
        )
        data: dict[str, Any] = response.json()
        return data

    def patch_json(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        data: dict[str, Any] = self.request("PATCH", path, json_body=json_body).json()
        return data
