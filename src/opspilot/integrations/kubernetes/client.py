"""Official Kubernetes SDK adapter behind a narrow async reader protocol."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, cast, runtime_checkable

from kubernetes import client, config  # type: ignore[import-untyped]
from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]
from kubernetes.config.config_exception import (  # type: ignore[import-untyped]
    ConfigException,
)
from urllib3.exceptions import MaxRetryError
from urllib3.exceptions import TimeoutError as Urllib3TimeoutError

from opspilot.integrations.kubernetes.errors import (
    KubernetesConfigurationError,
    KubernetesDataError,
    KubernetesIntegrationError,
    KubernetesInvalidRequestError,
    KubernetesNotFoundError,
    KubernetesPermissionError,
    KubernetesTimeoutError,
    KubernetesTransientError,
)
from opspilot.integrations.kubernetes.models import (
    JsonObject,
    KubernetesClientSettings,
    KubernetesConnectionMode,
)


@runtime_checkable
class KubernetesReader(Protocol):
    """SDK-independent reads needed by the five V1 Kubernetes tools."""

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject: ...

    async def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str,
    ) -> tuple[JsonObject, ...]: ...

    async def list_events(
        self,
        *,
        namespace: str,
        field_selector: str,
        limit: int,
    ) -> tuple[JsonObject, ...]: ...

    async def read_pod_log(
        self,
        *,
        namespace: str,
        pod_name: str,
        container_name: str | None,
        previous: bool,
        tail_lines: int,
        since_seconds: int | None,
        max_bytes: int,
    ) -> str: ...

    async def read_deployment(
        self,
        *,
        namespace: str,
        deployment_name: str,
    ) -> JsonObject: ...


class KubernetesSdkReader:
    """Async facade over the synchronous official Kubernetes Python client."""

    def __init__(
        self,
        *,
        core_api: Any,
        apps_api: Any,
        api_client: Any,
        request_timeout_seconds: float,
    ) -> None:
        self._core_api = core_api
        self._apps_api = apps_api
        self._api_client = api_client
        self._request_timeout_seconds = request_timeout_seconds

    async def read_pod(self, *, namespace: str, pod_name: str) -> JsonObject:
        result = await self._call(
            self._core_api.read_namespaced_pod,
            operation="read pod",
            name=pod_name,
            namespace=namespace,
            _request_timeout=self._request_timeout_seconds,
        )
        return self._sanitize_object(result, "pod")

    async def list_pods(
        self,
        *,
        namespace: str,
        label_selector: str,
    ) -> tuple[JsonObject, ...]:
        result = await self._call(
            self._core_api.list_namespaced_pod,
            operation="list pods",
            namespace=namespace,
            label_selector=label_selector,
            _request_timeout=self._request_timeout_seconds,
        )
        return self._sanitize_items(result, "pod list")

    async def list_events(
        self,
        *,
        namespace: str,
        field_selector: str,
        limit: int,
    ) -> tuple[JsonObject, ...]:
        result = await self._call(
            self._core_api.list_namespaced_event,
            operation="list events",
            namespace=namespace,
            field_selector=field_selector,
            limit=limit,
            _request_timeout=self._request_timeout_seconds,
        )
        return self._sanitize_items(result, "event list")

    async def read_pod_log(
        self,
        *,
        namespace: str,
        pod_name: str,
        container_name: str | None,
        previous: bool,
        tail_lines: int,
        since_seconds: int | None,
        max_bytes: int,
    ) -> str:
        kwargs: dict[str, object] = {
            "name": pod_name,
            "namespace": namespace,
            "previous": previous,
            "tail_lines": tail_lines,
            "limit_bytes": max_bytes,
            "_request_timeout": self._request_timeout_seconds,
        }
        if container_name is not None:
            kwargs["container"] = container_name
        if since_seconds is not None:
            kwargs["since_seconds"] = since_seconds

        result = await self._call(
            self._read_bounded_log_bytes,
            operation="read pod log",
            read_limit=max_bytes + 1,
            **kwargs,
        )
        if not isinstance(result, bytes):
            raise KubernetesDataError(
                "Kubernetes pod log response has an invalid shape"
            )
        return result.decode("utf-8", errors="replace")

    def _read_bounded_log_bytes(self, *, read_limit: int, **kwargs: object) -> bytes:
        response = self._core_api.read_namespaced_pod_log(
            _preload_content=False, **kwargs,
        )
        try:
            payload = response.read(read_limit)
            if not isinstance(payload, bytes):
                raise KubernetesDataError(
                    "Kubernetes pod log response has an invalid shape"
                )
            return payload
        finally:
            response.close()
            response.release_conn()

    async def read_deployment(
        self,
        *,
        namespace: str,
        deployment_name: str,
    ) -> JsonObject:
        result = await self._call(
            self._apps_api.read_namespaced_deployment,
            operation="read deployment",
            name=deployment_name,
            namespace=namespace,
            _request_timeout=self._request_timeout_seconds,
        )
        return self._sanitize_object(result, "deployment")

    def close(self) -> None:
        close = getattr(self._api_client, "close", None)
        if callable(close):
            close()

    async def _call(
        self,
        operation_callable: Callable[..., object],
        *,
        operation: str,
        **kwargs: object,
    ) -> object:
        try:
            return await asyncio.to_thread(operation_callable, **kwargs)
        except Exception as exc:
            raise _translate_sdk_error(exc, operation) from None

    def _sanitize_object(self, value: object, resource: str) -> JsonObject:
        serialized = self._api_client.sanitize_for_serialization(value)
        if not isinstance(serialized, dict):
            raise KubernetesDataError(
                f"Kubernetes {resource} response has an invalid shape"
            )
        return cast(JsonObject, serialized)

    def _sanitize_items(
        self,
        value: object,
        resource: str,
    ) -> tuple[JsonObject, ...]:
        serialized = self._sanitize_object(value, resource)
        items = serialized.get("items")
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise KubernetesDataError(
                f"Kubernetes {resource} response has invalid items"
            )
        return tuple(cast(JsonObject, item) for item in items)


def build_kubernetes_reader(
    settings: KubernetesClientSettings,
) -> KubernetesSdkReader:
    """Create an isolated SDK client without modifying global configuration."""

    configuration = build_kubernetes_configuration(settings)
    api_client = client.ApiClient(configuration=configuration)
    return KubernetesSdkReader(
        core_api=client.CoreV1Api(api_client),
        apps_api=client.AppsV1Api(api_client),
        api_client=api_client,
        request_timeout_seconds=settings.request_timeout_seconds,
    )


def build_kubernetes_configuration(
    settings: KubernetesClientSettings,
) -> Any:
    """Build one explicit, isolated SDK configuration for a reader version."""

    configuration = client.Configuration()
    configuration.debug = False
    try:
        if settings.mode is KubernetesConnectionMode.IN_CLUSTER:
            config.load_incluster_config(client_configuration=configuration)
        else:
            path = settings.kubeconfig_path
            context = settings.context
            if path is None or context is None:
                raise KubernetesConfigurationError(
                    "Explicit kubeconfig path and context are required"
                )
            if not path.is_file():
                raise KubernetesConfigurationError(
                    "Configured kubeconfig file does not exist"
                )
            config.load_kube_config(
                config_file=str(path),
                context=context,
                client_configuration=configuration,
                persist_config=False,
            )
    except KubernetesConfigurationError:
        raise
    except (ConfigException, OSError):
        raise KubernetesConfigurationError(
            "Kubernetes client configuration could not be loaded"
        ) from None

    return configuration


def _translate_sdk_error(
    exc: Exception,
    operation: str,
) -> KubernetesIntegrationError:
    if isinstance(exc, ApiException):
        status = exc.status
        if status == 404:
            return KubernetesNotFoundError(
                f"Kubernetes resource was not found during {operation}"
            )
        if status == 400:
            return KubernetesInvalidRequestError(
                f"Kubernetes request was rejected during {operation}"
            )
        if status in {401, 403}:
            return KubernetesPermissionError(
                f"Kubernetes access was denied during {operation}"
            )
        if status == 429 or (status is not None and 500 <= status <= 599):
            return KubernetesTransientError(
                f"Kubernetes API was temporarily unavailable during {operation}"
            )
        return KubernetesDataError(
            f"Kubernetes API request failed during {operation}"
        )

    if isinstance(exc, (TimeoutError, Urllib3TimeoutError)):
        return KubernetesTimeoutError(
            f"Kubernetes API timed out during {operation}"
        )
    if isinstance(exc, (MaxRetryError, ConnectionError, OSError)):
        return KubernetesTransientError(
            f"Kubernetes API connection failed during {operation}"
        )
    return KubernetesDataError(
        f"Kubernetes client failed during {operation}"
    )
