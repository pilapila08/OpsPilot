"""Fixed-endpoint, read-only Prometheus HTTP boundary."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

import httpx
from pydantic import Field, HttpUrl, SecretStr, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.tools.errors import ToolExecutionError


class PrometheusSettings(StrictSchema):
    base_url: HttpUrl
    bearer_token: SecretStr | None = None
    request_timeout_seconds: float = Field(default=8, gt=0, le=20)

    @model_validator(mode="after")
    def require_private_https_endpoint(self) -> PrometheusSettings:
        if (
            self.base_url.scheme != "https" or self.base_url.username is not None
            or self.base_url.password is not None or self.base_url.query is not None
            or self.base_url.fragment is not None
            or self.base_url.path not in (None, "/")
        ):
            raise ValueError("Prometheus endpoint must be an HTTPS origin")
        return self


class PrometheusBoundaryError(ToolExecutionError):
    code = ErrorCode.TOOL_OUTPUT_INVALID


class PrometheusPermissionError(PrometheusBoundaryError):
    code = ErrorCode.PERMISSION_DENIED


class PrometheusUnavailableError(PrometheusBoundaryError):
    code = ErrorCode.EXTERNAL_SERVICE_ERROR


class PrometheusTimeoutError(PrometheusBoundaryError):
    code = ErrorCode.TOOL_TIMEOUT


class PrometheusReader(Protocol):
    async def query_range(
        self, *, query: str, start: datetime, end: datetime, step_seconds: int,
    ) -> JsonObject: ...


class PrometheusHttpReader:
    def __init__(
        self, settings: PrometheusSettings,
        *, client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            verify=True, trust_env=False, follow_redirects=False,
        )
        self._owns_client = client is None

    async def query_range(
        self, *, query: str, start: datetime, end: datetime, step_seconds: int,
    ) -> JsonObject:
        headers: dict[str, str] = {}
        if self._settings.bearer_token is not None:
            headers["Authorization"] = (
                f"Bearer {self._settings.bearer_token.get_secret_value()}"
            )
        try:
            response = await self._client.get(
                f"{str(self._settings.base_url).rstrip('/')}/api/v1/query_range",
                params={
                    "query": query, "start": start.timestamp(),
                    "end": end.timestamp(), "step": step_seconds,
                },
                headers=headers,
            )
        except httpx.TimeoutException:
            raise PrometheusTimeoutError("Prometheus request timed out") from None
        except httpx.RequestError:
            raise PrometheusUnavailableError("Prometheus request failed") from None
        if response.status_code in (401, 403):
            raise PrometheusPermissionError("Prometheus read permission denied")
        if response.status_code != 200:
            raise PrometheusUnavailableError("Prometheus query unavailable")
        if len(response.content) > 262_144:
            raise PrometheusBoundaryError("Prometheus response exceeds size limit")
        try:
            payload = response.json()
        except ValueError:
            raise PrometheusBoundaryError("Prometheus response is not JSON") from None
        if not isinstance(payload, dict):
            raise PrometheusBoundaryError("Prometheus response is not an object")
        return payload

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
