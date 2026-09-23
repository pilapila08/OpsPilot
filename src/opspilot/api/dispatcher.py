"""Bounded in-process task admission and execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from opspilot.agent.state import AgentStatus
from opspilot.runtime import DiagnosisRequest, DiagnosisRunResult
from opspilot.storage.contracts import TaskRepository, TaskSnapshot
from opspilot.runtime.orchestrator import UuidRuntimeIds


class DispatchUnavailable(RuntimeError):
    pass


class RunDispatcher:
    def __init__(
        self, *,
        tasks: TaskRepository,
        run: Callable[[DiagnosisRequest, str], Awaitable[DiagnosisRunResult]],
        max_concurrent: int,
        max_pending: int,
        shutdown_seconds: float,
    ) -> None:
        self._tasks = tasks
        self._run = run
        self._capacity = max_concurrent + max_pending
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._shutdown_seconds = shutdown_seconds
        self._lock = asyncio.Lock()
        self._jobs: set[asyncio.Task[None]] = set()
        self._closing = False
        self.results: dict[str, DiagnosisRunResult] = {}

    async def submit(
        self, request: DiagnosisRequest, idempotency_key: str | None = None,
    ) -> TaskSnapshot:
        async with self._lock:
            if self._closing:
                raise DispatchUnavailable("dispatcher is shutting down")
            candidate = TaskSnapshot(
                task_id=UuidRuntimeIds().new("task"),
                user_query=request.query, namespace=request.namespace,
                mode=request.mode, case_id=request.case_id,
                idempotency_key=idempotency_key,
            )
            if idempotency_key is not None:
                existing = self._tasks.get_by_idempotency_key(idempotency_key)
                if existing is not None:
                    matched, _ = self._tasks.create_or_get_task(candidate)
                    return matched
            if len(self._jobs) >= self._capacity:
                raise DispatchUnavailable("dispatcher capacity is exhausted")
            task, created = self._tasks.create_or_get_task(candidate)
            if created:
                job = asyncio.create_task(self._execute(request, task.task_id))
                self._jobs.add(job)
                job.add_done_callback(self._jobs.discard)
            return task

    async def _execute(self, request: DiagnosisRequest, task_id: str) -> None:
        async with self._semaphore:
            try:
                self.results[task_id] = await self._run(request, task_id)
            except Exception:
                self._tasks.fail_unstarted_task(task_id)

    async def close(self) -> None:
        async with self._lock:
            self._closing = True
        if self._jobs:
            done, pending = await asyncio.wait(self._jobs, timeout=self._shutdown_seconds)
            del done
            for job in pending:
                job.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)


class InlineDispatcher(RunDispatcher):
    """Deterministic test dispatcher with the same admission contract."""

    async def submit(
        self, request: DiagnosisRequest, idempotency_key: str | None = None,
    ) -> TaskSnapshot:
        task = await super().submit(request, idempotency_key)
        if self._jobs:
            await asyncio.gather(*self._jobs)
        return task
