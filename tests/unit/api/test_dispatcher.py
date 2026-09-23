from __future__ import annotations

import asyncio

import pytest

from opspilot.api.dispatcher import DispatchUnavailable, RunDispatcher
from opspilot.api.settings import ApiSettings
from opspilot.runtime import DiagnosisRequest, DiagnosisRunResult
from opspilot.storage.runtime import InMemoryRuntimeRepository

REQUEST = DiagnosisRequest(
    query="Why restarting?", namespace="opspilot-fixtures",
    mode="replay", case_id="crashloop_liveness_v1",
)


def test_admission_idempotency_capacity_and_shutdown() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        repository = InMemoryRuntimeRepository()
        calls: list[str] = []

        async def run(_: DiagnosisRequest, task_id: str) -> DiagnosisRunResult:
            calls.append(task_id)
            started.set()
            await release.wait()
            raise RuntimeError("simulated worker failure")

        dispatcher = RunDispatcher(
            tasks=repository, run=run, max_concurrent=1,
            max_pending=0, shutdown_seconds=1,
        )
        first = await dispatcher.submit(REQUEST, "repeat")
        await started.wait()
        duplicate = await dispatcher.submit(REQUEST, "repeat")
        assert duplicate.task_id == first.task_id
        with pytest.raises(DispatchUnavailable):
            await dispatcher.submit(REQUEST, "different")
        assert calls == [first.task_id]
        release.set()
        await dispatcher.close()
        assert repository.get_task(first.task_id).status.value == "FAILED"  # type: ignore[union-attr]
        with pytest.raises(DispatchUnavailable):
            await dispatcher.submit(REQUEST)

    asyncio.run(scenario())


def test_environment_settings_are_strict_and_live_requires_configuration() -> None:
    settings = ApiSettings.from_env({
        "OPSPILOT_MAX_CONCURRENT": "2",
        "OPSPILOT_MAX_TOOL_CALLS": "3",
        "OPSPILOT_LIVE_ENABLED": "false",
    })
    assert settings.max_concurrent == 2
    assert settings.budget.max_tool_calls == 3
    with pytest.raises(ValueError):
        ApiSettings.from_env({"OPSPILOT_LIVE_ENABLED": "true"})
    with pytest.raises(ValueError):
        ApiSettings.from_env({"OPSPILOT_LIVE_ENABLED": "maybe"})
