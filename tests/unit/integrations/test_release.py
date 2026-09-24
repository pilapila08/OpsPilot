import asyncio
import json
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import cast

import httpx
import pytest
from pydantic import JsonValue, SecretStr, ValidationError

from opspilot.errors import ErrorCode
from opspilot.evidence.models import Evidence
from opspilot.evidence.v2 import V2EvidenceExtractorRegistry
from opspilot.integrations.kubernetes.models import JsonObject
from opspilot.integrations.release import (
    CicdReader, GitReader, GithubReadOnlyReader, GithubReadSettings,
    ReleaseBoundaryError, ReleasePermissionError, ReleaseTimeoutError,
)
from opspilot.tools import ToolInvocation, ToolRegistry, ToolRiskLevel
from opspilot.planning.v2 import ObservationSummaryV2
from opspilot.tools.release import (
    CicdDeploymentInput, CicdDeploymentOutput, GitCommitInput, GitDiffInput, ReleaseAllowlist,
    ReleaseScope, ReleaseToolHandlers, release_tool_definitions,
)

NOW = datetime.now(UTC).replace(microsecond=0)
BASE = "a" * 40
HEAD = "b" * 40


class FakeReleaseReader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.files: list[JsonObject] = [
            {"filename": "src/server.py", "additions": 12, "deletions": 3,
             "patch": "LEAK_TOKEN=secret"},
            {"filename": "secrets/prod.env", "additions": 1, "deletions": 0,
             "patch": "password=secret"},
        ]
        self.truncated = False
        self.deployment_rows: tuple[JsonObject, ...] | None = None
        self.status_rows: tuple[JsonObject, ...] | None = None

    async def read_commit(self, *, repository: str, sha: str) -> JsonObject:
        self.calls.append(("commit", repository))
        return cast(JsonObject, {"sha": sha, "commit": {"committer": {"date": NOW.isoformat()},
                                        "message": "password=secret"},
                "parents": [{"sha": BASE}]})

    async def compare(self, *, repository: str, base_sha: str, head_sha: str) -> JsonObject:
        self.calls.append(("diff", repository))
        return cast(JsonObject, {"base_commit": {"sha": base_sha}, "merge_base_commit": {"sha": base_sha},
                "status": "ahead", "total_commits": 1,
                "commits": [{"sha": head_sha}], "files": self.files})

    async def list_deployments(
        self, *, project: str, environment: str,
    ) -> tuple[tuple[JsonObject, ...], bool]:
        self.calls.append(("deployments", project))
        assert environment == "staging"
        if self.deployment_rows is not None:
            return self.deployment_rows, self.truncated
        return ((
            {"id": 42, "sha": HEAD, "environment": environment,
             "created_at": NOW.isoformat(), "payload": {"token": "secret"}},
            {"id": 41, "sha": BASE, "environment": environment,
             "created_at": (NOW - timedelta(minutes=30)).isoformat()},
        ), self.truncated)

    async def list_statuses(
        self, *, project: str, deployment_id: int,
    ) -> tuple[JsonObject, ...]:
        self.calls.append(("status", project))
        if self.status_rows is not None:
            return self.status_rows
        if deployment_id == 41:
            return (
                {"state": "inactive", "created_at": NOW.isoformat()},
                {"state": "success", "created_at": (NOW - timedelta(minutes=29)).isoformat()},
            )
        return ({"state": "success", "created_at": NOW.isoformat(),
                 "log_url": "https://secret.example/log"},)


def _scopes() -> ReleaseAllowlist:
    return ReleaseAllowlist(scopes=(ReleaseScope(
        namespace="team-a", deployment_name="api",
        repository="approved/backend", project="approved/deployments",
        environment="staging",
    ),))


def _git(fake: FakeReleaseReader) -> GitReader:
    return cast(GitReader, fake)


def _cicd(fake: FakeReleaseReader) -> CicdReader:
    return cast(CicdReader, fake)


def test_three_release_tools_are_read_only_allowlisted_and_patch_free() -> None:
    fake = FakeReleaseReader()
    registry = ToolRegistry()
    for definition in release_tool_definitions(scopes=_scopes(), git=_git(fake), cicd=_cicd(fake)):
        registry.register(definition)
    assert {item.name for item in registry.descriptors()} == {
        "cicd.get_recent_deployment", "git.get_recent_commit", "git.diff",
    }
    assert all(item.risk_level is ToolRiskLevel.READ_ONLY for item in registry.descriptors())
    calls = (
        ("cicd.get_recent_deployment", {"namespace": "team-a", "deployment_name": "api"}),
        ("git.get_recent_commit", {"namespace": "team-a", "deployment_name": "api", "commit_sha": HEAD}),
        ("git.diff", {"namespace": "team-a", "deployment_name": "api", "base_sha": BASE, "head_sha": HEAD}),
    )
    outputs: list[dict[str, JsonValue]] = []
    evidence: list[Evidence] = []
    evidence_numbers = count(1)
    for index, (tool, arguments) in enumerate(calls, 1):
        invocation = ToolInvocation(
            call_id=f"call_{index}", tool=tool,
            arguments=cast(dict[str, JsonValue], arguments),
        )
        response = asyncio.run(registry.invoke(invocation))
        assert response.success, response.error
        assert response.data is not None
        outputs.append(response.data)
        evidence.extend(V2EvidenceExtractorRegistry().extract(
            invocation=invocation, response=response, trace_id="trace_release",
            tool_attempt_id=f"tool_{index}", collected_at=datetime.now(UTC),
            evidence_id_factory=lambda: f"ev_{next(evidence_numbers)}",
        ))
    deployment = CicdDeploymentOutput.model_validate_json(json.dumps(outputs[0]), strict=True)
    assert deployment.releases[1].status == "inactive"
    assert deployment.releases[1].deployed_at == NOW - timedelta(minutes=29)
    assert outputs[1]["commit_sha"] == HEAD
    assert outputs[2]["file_count"] == 2
    assert outputs[2]["additions"] == 13
    encoded = json.dumps(outputs)
    summary = ObservationSummaryV2.from_persisted("trace_release", tuple(evidence))
    assert any(item.source == "git_diff" for item in evidence)
    assert any(item.source == "cicd_deployment" for item in evidence)
    assert "commit_sha" in summary.model_dump_json()
    encoded += summary.model_dump_json()
    assert all(secret not in encoded for secret in (
        "LEAK_TOKEN", "password", "prod.env", "server.py", "log_url", "secret.example",
    ))
    assert fake.calls[0] == ("deployments", "approved/deployments")
    assert ("diff", "approved/backend") in fake.calls


def test_release_scope_and_sha_reject_model_supplied_repo_or_foreign_target() -> None:
    fake = FakeReleaseReader()
    registry = ToolRegistry()
    for definition in release_tool_definitions(scopes=_scopes(), git=_git(fake), cicd=_cicd(fake)):
        registry.register(definition)
    for arguments, expected in (
        ({"namespace": "team-a", "deployment_name": "api", "commit_sha": HEAD,
          "repository": "attacker/other"}, ErrorCode.INVALID_ARGUMENT),
        ({"namespace": "team-a", "deployment_name": "other", "commit_sha": HEAD},
         ErrorCode.POLICY_REJECTED),
        ({"namespace": "team-b", "deployment_name": "api", "commit_sha": HEAD},
         ErrorCode.POLICY_REJECTED),
    ):
        response = asyncio.run(registry.invoke(ToolInvocation(
            call_id="call_rejected", tool="git.get_recent_commit",
            arguments=cast(dict[str, JsonValue], arguments),
        )))
        assert not response.success
        assert response.error is not None and response.error.code is expected
    assert fake.calls == []
    with pytest.raises(ValidationError):
        GitCommitInput(namespace="team-a", deployment_name="api", commit_sha="main")
    with pytest.raises(ValidationError):
        GitDiffInput(namespace="team-a", deployment_name="api", base_sha=BASE, head_sha=BASE)
    with pytest.raises(ValidationError):
        ReleaseScope(namespace="team-a", deployment_name="api", repository="../escape",
                     project="approved/deployments", environment="staging")


def test_diff_bounds_unsafe_paths_and_deployment_truncation() -> None:
    fake = FakeReleaseReader()
    handlers = ReleaseToolHandlers(scopes=_scopes(), git=_git(fake), cicd=_cicd(fake), clock=lambda: NOW)
    request = GitDiffInput(namespace="team-a", deployment_name="api", base_sha=BASE, head_sha=HEAD)
    fake.files[0]["filename"] = "../secret"
    with pytest.raises(ReleaseBoundaryError):
        asyncio.run(handlers.diff(request))
    fake.files = [{"filename": f"src/file-{index}.py", "additions": 1, "deletions": 0}
                  for index in range(101)]
    with pytest.raises(ReleaseBoundaryError):
        asyncio.run(handlers.diff(request))
    fake.truncated = True
    deployment = asyncio.run(handlers.get_recent_deployment(CicdDeploymentInput(
        namespace="team-a", deployment_name="api",
    )))
    assert deployment.truncated is True


def test_release_history_rejects_clock_skew_and_out_of_order_statuses() -> None:
    fake = FakeReleaseReader()
    handlers = ReleaseToolHandlers(scopes=_scopes(), git=_git(fake), cicd=_cicd(fake), clock=lambda: NOW)
    request = CicdDeploymentInput(namespace="team-a", deployment_name="api")
    fake.deployment_rows = (
        {"id": 41, "sha": BASE, "environment": "staging",
         "created_at": (NOW - timedelta(minutes=30)).isoformat()},
        {"id": 42, "sha": HEAD, "environment": "staging",
         "created_at": NOW.isoformat()},
    )
    with pytest.raises(ReleaseBoundaryError, match="newest-first"):
        asyncio.run(handlers.get_recent_deployment(request))
    fake.deployment_rows = None
    fake.status_rows = (
        {"state": "success", "created_at": (NOW - timedelta(minutes=1)).isoformat()},
        {"state": "pending", "created_at": NOW.isoformat()},
    )
    with pytest.raises(ReleaseBoundaryError, match="history is invalid"):
        asyncio.run(handlers.get_recent_deployment(request))
    fake.status_rows = ({"state": "success", "created_at": (NOW + timedelta(minutes=2)).isoformat()},)
    with pytest.raises(ReleaseBoundaryError, match="outside the release window"):
        asyncio.run(handlers.get_recent_deployment(request))


def test_github_reader_fixed_get_routes_permission_timeout_and_pagination() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/compare/" in request.url.path:
            return httpx.Response(200, json={}, headers={
                "Link": '<https://api.github.com/next>; rel="next"',
            })
        return httpx.Response(200, json={"sha": HEAD})

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    reader = GithubReadOnlyReader(GithubReadSettings(token=SecretStr("private-token")), client=client)
    assert asyncio.run(reader.read_commit(repository="approved/backend", sha=HEAD))["sha"] == HEAD
    assert seen[0].method == "GET"
    assert seen[0].url.host == "api.github.com"
    assert seen[0].headers["authorization"] == "Bearer private-token"
    with pytest.raises(ReleaseBoundaryError, match="bounded page"):
        asyncio.run(reader.compare(repository="approved/backend", base_sha=BASE, head_sha=HEAD))
    with pytest.raises(ReleaseBoundaryError):
        asyncio.run(reader.read_commit(repository="../escape", sha=HEAD))
    asyncio.run(client.aclose())

    for status, error_type in ((403, ReleasePermissionError), (200, ReleaseBoundaryError)):
        response = httpx.Response(status, text="token=private-token")
        another = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response))
        with pytest.raises(error_type) as caught:
            asyncio.run(GithubReadOnlyReader(GithubReadSettings(), client=another).read_commit(
                repository="approved/backend", sha=HEAD,
            ))
        assert "private-token" not in str(caught.value)
        asyncio.run(another.aclose())

    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private-token")

    timed = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    with pytest.raises(ReleaseTimeoutError) as caught:
        asyncio.run(GithubReadOnlyReader(GithubReadSettings(), client=timed).read_commit(
            repository="approved/backend", sha=HEAD,
        ))
    assert "private-token" not in str(caught.value)
    asyncio.run(timed.aclose())
