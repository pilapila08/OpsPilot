"""Allowlisted read-only Git and CI deployment metadata Tools."""

from __future__ import annotations

import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from collections.abc import Callable
from typing import Any, Literal, cast

from pydantic import Field, field_validator, model_validator

from opspilot.agent.schemas import StrictSchema
from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes.models import JsonObject, NamespaceName, ResourceName
from opspilot.integrations.release import CicdReader, GitReader, ReleaseBoundaryError
from opspilot.tools.errors import ToolExecutionError
from opspilot.tools.models import RetryPolicy, ToolDefinition, ToolRiskLevel

_SHA = r"^[0-9a-f]{40}$"
_REPOSITORY = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"
_ENVIRONMENT = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$"
_CATEGORIES = ("config", "code", "docs", "manifests", "sensitive", "tests", "other")


class ReleaseScopeError(ToolExecutionError):
    code = ErrorCode.POLICY_REJECTED


class ReleaseScope(StrictSchema):
    namespace: NamespaceName
    deployment_name: ResourceName
    repository: str = Field(min_length=3, max_length=201, pattern=_REPOSITORY)
    project: str = Field(min_length=3, max_length=201, pattern=_REPOSITORY)
    environment: str = Field(min_length=1, max_length=63, pattern=_ENVIRONMENT)

    @field_validator("repository", "project")
    @classmethod
    def reject_ambiguous_slug(cls, value: str) -> str:
        if any(part in {".", ".."} for part in value.split("/")):
            raise ValueError("repository/project slug is ambiguous")
        return value


class ReleaseAllowlist(StrictSchema):
    scopes: tuple[ReleaseScope, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def unique_targets(self) -> ReleaseAllowlist:
        keys = {(scope.namespace, scope.deployment_name) for scope in self.scopes}
        if len(keys) != len(self.scopes):
            raise ValueError("release scope repeats a workload")
        return self

    def lookup(self, namespace: str, deployment_name: str) -> ReleaseScope:
        for scope in self.scopes:
            if scope.namespace == namespace and scope.deployment_name == deployment_name:
                return scope
        raise ReleaseScopeError("release target is outside configured allowlist")


class ReleaseTargetInput(StrictSchema):
    namespace: NamespaceName
    deployment_name: ResourceName


class GitCommitInput(ReleaseTargetInput):
    commit_sha: str = Field(pattern=_SHA)


class GitDiffInput(ReleaseTargetInput):
    base_sha: str = Field(pattern=_SHA)
    head_sha: str = Field(pattern=_SHA)

    @model_validator(mode="after")
    def distinct_commits(self) -> GitDiffInput:
        if self.base_sha == self.head_sha:
            raise ValueError("Git comparison requires two commits")
        return self


class GitCommitOutput(ReleaseTargetInput):
    repository: str = Field(pattern=_REPOSITORY)
    commit_sha: str = Field(pattern=_SHA)
    parent_shas: tuple[str, ...] = Field(max_length=8)
    committed_at: datetime

    @field_validator("committed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return _aware(value)


class DiffCategory(StrictSchema):
    category: Literal["config", "code", "docs", "manifests", "sensitive", "tests", "other"]
    file_count: int = Field(ge=1, le=100)


class GitDiffOutput(ReleaseTargetInput):
    repository: str = Field(pattern=_REPOSITORY)
    base_sha: str = Field(pattern=_SHA)
    head_sha: str = Field(pattern=_SHA)
    file_count: int = Field(ge=0, le=100)
    additions: int = Field(ge=0, le=50_000)
    deletions: int = Field(ge=0, le=50_000)
    categories: tuple[DiffCategory, ...] = Field(max_length=7)

    @model_validator(mode="after")
    def category_total(self) -> GitDiffOutput:
        if sum(item.file_count for item in self.categories) != self.file_count:
            raise ValueError("diff category counts do not sum to file count")
        if len({item.category for item in self.categories}) != len(self.categories):
            raise ValueError("diff categories repeat")
        return self


class CicdDeploymentInput(ReleaseTargetInput):
    lookback_minutes: int = Field(default=120, ge=30, le=1_440)


class ReleaseRecord(StrictSchema):
    release_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    commit_sha: str = Field(pattern=_SHA)
    deployed_at: datetime
    status: Literal["success", "failure", "error", "inactive", "in_progress", "queued", "pending", "unknown"]

    @field_validator("deployed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return _aware(value)


class CicdDeploymentOutput(ReleaseTargetInput):
    project: str = Field(pattern=_REPOSITORY)
    environment: str = Field(pattern=_ENVIRONMENT)
    releases: tuple[ReleaseRecord, ...] = Field(max_length=10)
    truncated: bool
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        return _aware(value)

    @model_validator(mode="after")
    def ordered_releases(self) -> CicdDeploymentOutput:
        if len({item.release_id for item in self.releases}) != len(self.releases):
            raise ValueError("deployment IDs repeat")
        if any(left.deployed_at < right.deployed_at for left, right in zip(self.releases, self.releases[1:])):
            raise ValueError("deployment history is not newest-first")
        return self


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("release time requires timezone")
    return value.astimezone(UTC)


def _object(value: object, field: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ReleaseBoundaryError(f"{field} is invalid")
    return cast(JsonObject, value)


def _text(value: object, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ReleaseBoundaryError(f"{field} is invalid")
    return value


def _sha(value: object, field: str) -> str:
    selected = _text(value, field, 40)
    if re.fullmatch(_SHA, selected) is None:
        raise ReleaseBoundaryError(f"{field} is invalid")
    return selected


def _nonnegative(value: object, field: str, limit: int) -> int:
    if type(value) is not int or not 0 <= value <= limit:
        raise ReleaseBoundaryError(f"{field} is invalid or exceeds limit")
    return value


def _time(value: object, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(_text(value, field, 64).replace("Z", "+00:00"))
        return _aware(parsed)
    except ValueError:
        raise ReleaseBoundaryError(f"{field} is invalid") from None


def _category(path: object) -> str:
    selected = _text(path, "diff path", 256)
    parts = selected.split("/")
    if (selected.startswith("/") or "\\" in selected or any(
        part in {"", ".", ".."} or any(ord(char) < 32 for char in part) for part in parts
    )):
        raise ReleaseBoundaryError("diff path is unsafe")
    lowered = selected.lower()
    if any(token in lowered for token in ("secret", "credential", ".env", "private_key", "id_rsa")):
        return "sensitive"
    if parts[0] in {"deploy", "k8s", "kubernetes", "manifests", "helm"}:
        return "manifests"
    if parts[0] in {"tests", "test", "spec"} or lowered.endswith(("_test.py", ".spec.ts", ".test.ts")):
        return "tests"
    if lowered.endswith((".md", ".rst", ".txt")):
        return "docs"
    if lowered.endswith((".yaml", ".yml", ".toml", ".ini", ".json")):
        return "config"
    if lowered.endswith((".py", ".go", ".java", ".js", ".ts", ".rs")):
        return "code"
    return "other"


class ReleaseToolHandlers:
    def __init__(
        self, *, scopes: ReleaseAllowlist, git: GitReader, cicd: CicdReader,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scopes = scopes
        self._git = git
        self._cicd = cicd
        self._clock = clock or (lambda: datetime.now(UTC))

    async def get_recent_commit(self, arguments: GitCommitInput) -> GitCommitOutput:
        scope = self._scopes.lookup(arguments.namespace, arguments.deployment_name)
        raw = await self._git.read_commit(repository=scope.repository, sha=arguments.commit_sha)
        if _sha(raw.get("sha"), "commit SHA") != arguments.commit_sha:
            raise ReleaseBoundaryError("Git commit differs from requested immutable SHA")
        commit = _object(raw.get("commit"), "commit metadata")
        committer = _object(commit.get("committer"), "commit committer")
        parents = raw.get("parents")
        if not isinstance(parents, list) or len(parents) > 8:
            raise ReleaseBoundaryError("commit parent list is invalid")
        return GitCommitOutput(
            namespace=arguments.namespace, deployment_name=arguments.deployment_name,
            repository=scope.repository, commit_sha=arguments.commit_sha,
            parent_shas=tuple(_sha(_object(item, "commit parent").get("sha"), "parent SHA") for item in parents),
            committed_at=_time(committer.get("date"), "commit time"),
        )

    async def diff(self, arguments: GitDiffInput) -> GitDiffOutput:
        scope = self._scopes.lookup(arguments.namespace, arguments.deployment_name)
        raw = await self._git.compare(
            repository=scope.repository, base_sha=arguments.base_sha,
            head_sha=arguments.head_sha,
        )
        if (_sha(_object(raw.get("base_commit"), "diff base").get("sha"), "diff base SHA") != arguments.base_sha
            or _sha(_object(raw.get("merge_base_commit"), "merge base").get("sha"), "merge base SHA") != arguments.base_sha):
            raise ReleaseBoundaryError("Git comparison is not an exact ancestor range")
        commits = _nonnegative(raw.get("total_commits"), "diff commit count", 20)
        if commits == 0 or raw.get("status") != "ahead":
            raise ReleaseBoundaryError("Git comparison does not advance the base")
        commit_items = raw.get("commits")
        if (not isinstance(commit_items, list) or len(commit_items) != commits
            or _sha(_object(commit_items[-1], "diff head").get("sha"), "diff head SHA") != arguments.head_sha):
            raise ReleaseBoundaryError("Git comparison head is incomplete or mismatched")
        files = raw.get("files")
        if not isinstance(files, list) or len(files) > 100:
            raise ReleaseBoundaryError("Git comparison file list is invalid or unbounded")
        categories: Counter[str] = Counter()
        additions = 0
        deletions = 0
        for item in files:
            file = _object(item, "diff file")
            categories[_category(file.get("filename"))] += 1
            additions += _nonnegative(file.get("additions"), "diff additions", 50_000)
            deletions += _nonnegative(file.get("deletions"), "diff deletions", 50_000)
            if additions > 50_000 or deletions > 50_000:
                raise ReleaseBoundaryError("Git diff changes exceed limit")
        return GitDiffOutput(
            namespace=arguments.namespace, deployment_name=arguments.deployment_name,
            repository=scope.repository, base_sha=arguments.base_sha,
            head_sha=arguments.head_sha, file_count=len(files),
            additions=additions, deletions=deletions,
            categories=tuple(DiffCategory(category=cast(Any, category), file_count=count)
                             for category, count in sorted(categories.items())),
        )

    async def get_recent_deployment(
        self, arguments: CicdDeploymentInput,
    ) -> CicdDeploymentOutput:
        scope = self._scopes.lookup(arguments.namespace, arguments.deployment_name)
        now = _aware(self._clock())
        raw, has_next = await self._cicd.list_deployments(
            project=scope.project, environment=scope.environment,
        )
        if len(raw) > 10:
            raise ReleaseBoundaryError("deployment history exceeds page limit")
        if has_next and not raw:
            raise ReleaseBoundaryError("deployment pagination has no first page")
        cutoff = now - timedelta(minutes=arguments.lookback_minutes)
        selected: list[tuple[int, str, datetime]] = []
        previous_time: datetime | None = None
        for item in raw:
            if item.get("environment") != scope.environment:
                raise ReleaseBoundaryError("deployment environment differs from allowlist")
            created = _time(item.get("created_at"), "deployment time")
            if created > now + timedelta(seconds=30):
                raise ReleaseBoundaryError("deployment time is in the future")
            if previous_time is not None and created > previous_time:
                raise ReleaseBoundaryError("deployment history is not newest-first")
            previous_time = created
            identifier = _nonnegative(item.get("id"), "deployment ID", 2**63 - 1)
            if identifier == 0:
                raise ReleaseBoundaryError("deployment ID is zero")
            if created >= cutoff:
                selected.append((identifier, _sha(item.get("sha"), "deployment SHA"), created))
        selected.sort(key=lambda item: (item[2], item[0]), reverse=True)
        releases: list[ReleaseRecord] = []
        for identifier, sha, created in selected:
            statuses = await self._cicd.list_statuses(
                project=scope.project, deployment_id=identifier,
            )
            if len(statuses) > 10:
                raise ReleaseBoundaryError("deployment status history exceeds page limit")
            state = "unknown" if not statuses else _text(statuses[0].get("state"), "deployment status", 32)
            if state not in {"success", "failure", "error", "inactive", "in_progress", "queued", "pending", "unknown"}:
                raise ReleaseBoundaryError("deployment status is invalid")
            status_times: list[tuple[str, datetime]] = []
            previous_status_time: datetime | None = None
            for status in statuses:
                status_state = _text(status.get("state"), "deployment status", 32)
                status_time = _time(status.get("created_at"), "deployment status time")
                if (status_state not in {"success", "failure", "error", "inactive", "in_progress", "queued", "pending"}
                    or (previous_status_time is not None and status_time > previous_status_time)):
                    raise ReleaseBoundaryError("deployment status history is invalid")
                previous_status_time = status_time
                status_times.append((status_state, status_time))
            successful = next((time for status_state, time in status_times if status_state == "success"), None)
            completed = successful or (status_times[0][1] if status_times else created)
            if completed < created or completed > now + timedelta(seconds=30):
                raise ReleaseBoundaryError("deployment status time is outside the release window")
            if state == "inactive" and successful is None:
                state = "unknown"
            releases.append(ReleaseRecord(
                release_id=str(identifier), commit_sha=sha, deployed_at=completed,
                status=cast(Any, state),
            ))
        truncated = has_next and bool(raw) and _time(raw[-1].get("created_at"), "deployment time") >= cutoff
        return CicdDeploymentOutput(
            namespace=arguments.namespace, deployment_name=arguments.deployment_name,
            project=scope.project, environment=scope.environment,
            releases=tuple(releases), truncated=truncated, observed_at=now,
        )


def release_tool_definitions(
    *, scopes: ReleaseAllowlist, git: GitReader, cicd: CicdReader,
) -> tuple[ToolDefinition[Any, Any], ...]:
    handlers = ReleaseToolHandlers(scopes=scopes, git=git, cicd=cicd)
    retry = RetryPolicy(
        max_retries=1,
        retryable_errors=(ErrorCode.TOOL_TIMEOUT, ErrorCode.EXTERNAL_SERVICE_ERROR),
    )
    return (
        ToolDefinition(
            name="cicd.get_recent_deployment", description="Read bounded releases for an allowlisted workload.",
            risk_level=ToolRiskLevel.READ_ONLY, input_model=CicdDeploymentInput,
            output_model=CicdDeploymentOutput, handler=handlers.get_recent_deployment,
            source="cicd", version="v2", timeout_seconds=20, retry_policy=retry,
        ),
        ToolDefinition(
            name="git.get_recent_commit", description="Read immutable commit metadata for an allowlisted workload.",
            risk_level=ToolRiskLevel.READ_ONLY, input_model=GitCommitInput,
            output_model=GitCommitOutput, handler=handlers.get_recent_commit,
            source="git", version="v2", timeout_seconds=10, retry_policy=retry,
        ),
        ToolDefinition(
            name="git.diff", description="Read bounded, patch-free categories for an immutable compare range.",
            risk_level=ToolRiskLevel.READ_ONLY, input_model=GitDiffInput,
            output_model=GitDiffOutput, handler=handlers.diff,
            source="git", version="v2", timeout_seconds=12, retry_policy=retry,
        ),
    )
