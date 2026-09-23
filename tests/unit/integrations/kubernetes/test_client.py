import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

from opspilot.errors import ErrorCode
from opspilot.integrations.kubernetes import (
    KubernetesClientSettings,
    KubernetesConfigurationError,
    KubernetesConnectionMode,
    KubernetesDataError,
    KubernetesInvalidRequestError,
    KubernetesNotFoundError,
    KubernetesPermissionError,
    KubernetesSdkReader,
    KubernetesTimeoutError,
    KubernetesTransientError,
    build_kubernetes_reader,
)


class FakeApiClient:
    def __init__(self) -> None:
        self.closed = False

    def sanitize_for_serialization(self, value: object) -> object:
        return value

    def close(self) -> None:
        self.closed = True


class FakeCoreApi:
    def __init__(self) -> None:
        self.last_call: tuple[str, dict[str, object]] | None = None
        self.log_response = FakeRawLogResponse(b"bounded log")

    def read_namespaced_pod(self, **kwargs: object) -> object:
        self.last_call = ("read_pod", kwargs)
        return {
            "metadata": {
                "namespace": kwargs["namespace"],
                "name": kwargs["name"],
                "uid": "pod-uid",
            },
            "spec": {"containers": [{"name": "api"}]},
        }

    def list_namespaced_pod(self, **kwargs: object) -> object:
        self.last_call = ("list_pods", kwargs)
        return {"items": []}

    def list_namespaced_event(self, **kwargs: object) -> object:
        self.last_call = ("list_events", kwargs)
        return {"items": []}

    def read_namespaced_pod_log(self, **kwargs: object) -> object:
        self.last_call = ("read_log", kwargs)
        return self.log_response


class FakeRawLogResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.read_amount: int | None = None
        self.closed = False
        self.released = False

    def read(self, amount: int) -> bytes:
        self.read_amount = amount
        return self.payload[:amount]

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class FakeAppsApi:
    def __init__(self) -> None:
        self.last_call: tuple[str, dict[str, object]] | None = None

    def read_namespaced_deployment(self, **kwargs: object) -> object:
        self.last_call = ("read_deployment", kwargs)
        return {
            "metadata": {
                "namespace": kwargs["namespace"],
                "name": kwargs["name"],
                "uid": "deployment-uid",
            }
        }


def _reader() -> tuple[KubernetesSdkReader, FakeCoreApi, FakeAppsApi, FakeApiClient]:
    core = FakeCoreApi()
    apps = FakeAppsApi()
    api_client = FakeApiClient()
    reader = KubernetesSdkReader(
        core_api=core,
        apps_api=apps,
        api_client=api_client,
        request_timeout_seconds=7.5,
    )
    return reader, core, apps, api_client


def test_reader_passes_bounded_arguments_and_sanitizes_results() -> None:
    reader, core, apps, api_client = _reader()

    pod = asyncio.run(reader.read_pod(namespace="team-a", pod_name="api-7d9f"))
    pod_metadata = cast(dict[str, object], pod["metadata"])
    assert pod_metadata["uid"] == "pod-uid"
    assert core.last_call == (
        "read_pod",
        {
            "name": "api-7d9f",
            "namespace": "team-a",
            "_request_timeout": 7.5,
        },
    )

    log = asyncio.run(
        reader.read_pod_log(
            namespace="team-a",
            pod_name="api-7d9f",
            container_name="api",
            previous=True,
            tail_lines=200,
            since_seconds=300,
            max_bytes=65_536,
        )
    )
    assert log == "bounded log"
    assert core.last_call is not None
    assert core.last_call[1]["_request_timeout"] == 7.5
    assert core.last_call[1]["limit_bytes"] == 65_536
    assert core.last_call[1]["_preload_content"] is False
    assert core.log_response.read_amount == 65_537
    assert core.log_response.closed and core.log_response.released

    assert asyncio.run(
        reader.list_pods(namespace="team-a", label_selector="app=api")
    ) == ()
    assert core.last_call == (
        "list_pods",
        {
            "namespace": "team-a",
            "label_selector": "app=api",
            "_request_timeout": 7.5,
        },
    )

    assert asyncio.run(
        reader.list_events(
            namespace="team-a",
            field_selector="involvedObject.uid=pod-uid",
            limit=100,
        )
    ) == ()
    assert core.last_call == (
        "list_events",
        {
            "namespace": "team-a",
            "field_selector": "involvedObject.uid=pod-uid",
            "limit": 100,
            "_request_timeout": 7.5,
        },
    )

    deployment = asyncio.run(
        reader.read_deployment(
            namespace="team-a",
            deployment_name="api",
        )
    )
    deployment_metadata = cast(dict[str, object], deployment["metadata"])
    assert deployment_metadata["name"] == "api"
    assert apps.last_call is not None

    reader.close()
    assert api_client.closed is True


def test_reader_decodes_raw_pod_log_lines_without_bytes_repr() -> None:
    reader, core, _, _ = _reader()
    core.log_response = FakeRawLogResponse(
        b"2026-09-23T10:09:04Z application boot started; configured_startup_delay_seconds=40\n"
        b"2026-09-23T10:09:28Z process terminated before application ready\n"
    )

    result = asyncio.run(reader.read_pod_log(
        namespace="opspilot-fixtures", pod_name="slow-start-api-001",
        container_name="slow-start-api", previous=True, tail_lines=200,
        since_seconds=None, max_bytes=65_536,
    ))

    assert result.count("\n") == 2
    assert result.startswith("2026-09-23T10:09:04Z ")
    assert not result.startswith("b'")
    assert core.log_response.closed and core.log_response.released


class RaisingCoreApi:
    def __init__(self, error_factory: Callable[[], Exception]) -> None:
        self._error_factory = error_factory

    def read_namespaced_pod(self, **kwargs: object) -> object:
        del kwargs
        raise self._error_factory()


@pytest.mark.parametrize(
    ("error_factory", "expected_type", "expected_code"),
    [
        (
            lambda: ApiException(status=404, reason="sensitive upstream body"),
            KubernetesNotFoundError,
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda: ApiException(status=403, reason="sensitive upstream body"),
            KubernetesPermissionError,
            ErrorCode.PERMISSION_DENIED,
        ),
        (
            lambda: ApiException(status=400, reason="sensitive upstream body"),
            KubernetesInvalidRequestError,
            ErrorCode.INVALID_ARGUMENT,
        ),
        (
            lambda: ApiException(status=503, reason="sensitive upstream body"),
            KubernetesTransientError,
            ErrorCode.EXTERNAL_SERVICE_ERROR,
        ),
        (
            lambda: ApiException(status=429, reason="sensitive upstream body"),
            KubernetesTransientError,
            ErrorCode.EXTERNAL_SERVICE_ERROR,
        ),
        (
            lambda: TimeoutError("sensitive endpoint"),
            KubernetesTimeoutError,
            ErrorCode.TOOL_TIMEOUT,
        ),
        (
            lambda: RuntimeError("sensitive client state"),
            KubernetesDataError,
            ErrorCode.TOOL_EXECUTION_FAILED,
        ),
    ],
)
def test_reader_translates_errors_without_leaking_original_text(
    error_factory: Callable[[], Exception],
    expected_type: type[Exception],
    expected_code: ErrorCode,
) -> None:
    reader = KubernetesSdkReader(
        core_api=RaisingCoreApi(error_factory),
        apps_api=FakeAppsApi(),
        api_client=FakeApiClient(),
        request_timeout_seconds=5,
    )

    with pytest.raises(expected_type) as caught:
        asyncio.run(reader.read_pod(namespace="team-a", pod_name="api-7d9f"))

    assert "sensitive" not in str(caught.value)
    assert getattr(caught.value, "code") is expected_code


def test_reader_rejects_non_object_sdk_payload() -> None:
    class InvalidCoreApi:
        def read_namespaced_pod(self, **kwargs: object) -> object:
            del kwargs
            return "not-an-object"

    reader = KubernetesSdkReader(
        core_api=InvalidCoreApi(),
        apps_api=FakeAppsApi(),
        api_client=FakeApiClient(),
        request_timeout_seconds=5,
    )

    with pytest.raises(KubernetesDataError, match="invalid shape"):
        asyncio.run(reader.read_pod(namespace="team-a", pod_name="api-7d9f"))


def test_build_reader_rejects_missing_explicit_kubeconfig(tmp_path: Path) -> None:
    settings = KubernetesClientSettings(
        mode=KubernetesConnectionMode.KUBECONFIG,
        kubeconfig_path=tmp_path / "missing",
        context="kind-opspilot",
    )

    with pytest.raises(
        KubernetesConfigurationError,
        match="does not exist",
    ):
        build_kubernetes_reader(settings)
