"""Fixed GitHub REST reads behind provider-neutral Git and deployment protocols."""

from __future__ import annotations

import re
from typing import Protocol, cast

import httpx
from pydantic import Field, SecretStr

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.tools.errors import ToolExecutionError

_SHA = re.compile(r"^[0-9a-f]{40}$")
_SEGMENT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?$")
_ENVIRONMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


class ReleaseBoundaryError(ToolExecutionError):
    code = ErrorCode.TOOL_OUTPUT_INVALID


class ReleasePermissionError(ReleaseBoundaryError):
    code = ErrorCode.PERMISSION_DENIED


class ReleaseUnavailableError(ReleaseBoundaryError):
    code = ErrorCode.EXTERNAL_SERVICE_ERROR


class ReleaseTimeoutError(ReleaseBoundaryError):
    code = ErrorCode.TOOL_TIMEOUT


class GithubReadSettings(StrictSchema):
    token: SecretStr | None = None
    request_timeout_seconds: float = Field(default=8, gt=0, le=20)


class GitReader(Protocol):
    async def read_commit(self, *, repository: str, sha: str) -> JsonObject: ...

    async def compare(self, *, repository: str, base_sha: str, head_sha: str) -> JsonObject: ...


class CicdReader(Protocol):
    async def list_deployments(self, *, project: str, environment: str) -> tuple[tuple[JsonObject, ...], bool]: ...

    async def list_statuses(self, *, project: str, deployment_id: int) -> tuple[JsonObject, ...]: ...


def _repository_path(repository: str) -> str:
    parts = repository.split("/")
    if len(parts) != 2 or any(_SEGMENT.fullmatch(part) is None for part in parts):
        raise ReleaseBoundaryError("release repository is invalid")
    return f"repos/{parts[0]}/{parts[1]}"


class GithubReadOnlyReader:
    def __init__(
        self, settings: GithubReadSettings, *, client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout_seconds, verify=True,
            trust_env=False, follow_redirects=False,
        )
        self._owns_client = client is None

    async def read_commit(self, *, repository: str, sha: str) -> JsonObject:
        if _SHA.fullmatch(sha) is None:
            raise ReleaseBoundaryError("release commit SHA is invalid")
        payload, _ = await self._get(f"{_repository_path(repository)}/commits/{sha}")
        return self._object(payload)

    async def compare(self, *, repository: str, base_sha: str, head_sha: str) -> JsonObject:
        if _SHA.fullmatch(base_sha) is None or _SHA.fullmatch(head_sha) is None:
            raise ReleaseBoundaryError("release compare SHA is invalid")
        payload, has_next = await self._get(
            f"{_repository_path(repository)}/compare/{base_sha}...{head_sha}",
            params={"per_page": "100", "page": "1"},
        )
        if has_next:
            raise ReleaseBoundaryError("GitHub comparison exceeds one bounded page")
        return self._object(payload)

    async def list_deployments(
        self, *, project: str, environment: str,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        if _ENVIRONMENT.fullmatch(environment) is None:
            raise ReleaseBoundaryError("release environment is invalid")
        payload, has_next = await self._get(
            f"{_repository_path(project)}/deployments",
            params={"environment": environment, "per_page": "10", "page": "1"},
        )
        return self._list(payload), has_next

    async def list_statuses(
        self, *, project: str, deployment_id: int,
    ) -> tuple[JsonObject, ...]:
        if type(deployment_id) is not int or deployment_id <= 0:
            raise ReleaseBoundaryError("deployment ID is invalid")
        payload, _ = await self._get(
            f"{_repository_path(project)}/deployments/{deployment_id}/statuses",
            params={"per_page": "10", "page": "1"},
        )
        return self._list(payload)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> tuple[object, bool]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if self._settings.token is not None:
            headers["Authorization"] = f"Bearer {self._settings.token.get_secret_value()}"
        try:
            response = await self._client.get(
                f"https://api.github.com/{path}", params=params, headers=headers,
            )
        except httpx.TimeoutException:
            raise ReleaseTimeoutError("GitHub read timed out") from None
        except httpx.RequestError:
            raise ReleaseUnavailableError("GitHub read failed") from None
        if response.status_code in (401, 403):
            raise ReleasePermissionError("GitHub read permission denied")
        if response.status_code != 200:
            raise ReleaseUnavailableError("GitHub read unavailable")
        if len(response.content) > 524_288:
            raise ReleaseBoundaryError("GitHub response exceeds size limit")
        try:
            return response.json(), 'rel="next"' in response.headers.get("link", "")
        except ValueError:
            raise ReleaseBoundaryError("GitHub response is not JSON") from None

    @staticmethod
    def _object(value: object) -> JsonObject:
        if not isinstance(value, dict):
            raise ReleaseBoundaryError("GitHub response is not an object")
        return cast(JsonObject, value)

    @staticmethod
    def _list(value: object) -> tuple[JsonObject, ...]:
        if not isinstance(value, list) or len(value) > 10 or any(not isinstance(item, dict) for item in value):
            raise ReleaseBoundaryError("GitHub response list is invalid or unbounded")
        return tuple(cast(JsonObject, item) for item in value)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
