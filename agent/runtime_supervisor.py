"""Local multi-run supervisor baseline (M3).

This module proves that local mode can execute multiple scoped Hermes runs
concurrently before any remote adapter exists. Each submitted run callable is
executed with its :class:`~agent.runtime_context.RuntimeContext` bound via
``use_runtime_context`` so native surfaces observe the correct scope, and run
lifecycle events are recorded through the registry's audit backend when one is
available.

Design notes:

* Defaults preserve existing single-user local behavior: ``max_concurrent_runs``
  defaults to ``1`` and :meth:`LocalRunSupervisor.run_sync` runs inline on the
  calling thread, exactly like today's local path.
* Concurrency uses a bounded thread pool. One run raising never corrupts another
  run's state: failures are captured into a :class:`RunResult` and the audit
  trail records ``failed`` instead of propagating.
* No provider/service names appear here; the supervisor only depends on the
  generic backend registry and runtime-context contracts.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from agent.runtime_backends import BackendCapability, RuntimeBackendRegistry
from agent.runtime_context import RuntimeContext, use_runtime_context

_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|credential)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]+"),
)


class RunStatus(str, Enum):
    """Lifecycle status of a supervised run."""

    PENDING = "pending"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of a supervised run, always scoped to its RuntimeContext."""

    status: RunStatus
    context: RuntimeContext
    value: Any = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunHandle:
    """Handle to an in-flight run; mirrors a future returning a RunResult."""

    context: RuntimeContext
    _future: Future = field(repr=False)

    def result(self, timeout: float | None = None) -> RunResult:
        return self._future.result(timeout=timeout)

    def done(self) -> bool:
        return self._future.done()


def _sanitize_error_for_audit(message: str) -> str:
    """Return exception text safe enough for local audit metadata."""

    sanitized = message
    for pattern in _SECRET_VALUE_PATTERNS:
        sanitized = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", sanitized)
    return sanitized


def _record_audit(
    registry: RuntimeBackendRegistry | None,
    context: RuntimeContext,
    worker_id: str,
    status: RunStatus,
    *,
    error: str | None = None,
) -> None:
    if registry is None:
        return
    try:
        audit = registry.get(BackendCapability.AUDIT, context)
    except Exception:
        return
    event: dict[str, Any] = {
        "status": status.value,
        "worker_id": worker_id,
        "run_id": context.run_id,
        "run_type": context.run_type,
    }
    if error is not None:
        event["error"] = error
    try:
        audit.record(context, event)
    except Exception:
        pass


def _execute_run(
    registry: RuntimeBackendRegistry | None,
    context: RuntimeContext,
    worker_id: str,
    fn: Callable[[], Any],
) -> RunResult:
    _record_audit(registry, context, worker_id, RunStatus.STARTED)
    with use_runtime_context(context):
        try:
            value = fn()
        except Exception as exc:  # one run crashing must not corrupt others
            error = f"{type(exc).__name__}: {exc}"
            audit_error = f"{type(exc).__name__}: {_sanitize_error_for_audit(str(exc))}"
            _record_audit(registry, context, worker_id, RunStatus.FAILED, error=audit_error)
            return RunResult(status=RunStatus.FAILED, context=context, error=error)
    _record_audit(registry, context, worker_id, RunStatus.SUCCEEDED)
    return RunResult(status=RunStatus.SUCCEEDED, context=context, value=value)


class LocalRunSupervisor:
    """Runs scoped Hermes run callables locally, inline or concurrently."""

    def __init__(
        self,
        *,
        worker_id: str = "local",
        max_concurrent_runs: int = 1,
        registry: RuntimeBackendRegistry | None = None,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be >= 1")
        self.worker_id = worker_id
        self.max_concurrent_runs = max_concurrent_runs
        self._registry = registry
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._register_worker()

    def _register_worker(self) -> None:
        if self._registry is None:
            return
        try:
            workers = self._registry.get(BackendCapability.WORKER_REGISTRY, None)
            workers.register(None, {"id": self.worker_id, "max_concurrent_runs": self.max_concurrent_runs})
        except Exception:
            pass

    def _ensure_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self.max_concurrent_runs,
                    thread_name_prefix=f"hermes-run-{self.worker_id}",
                )
            return self._executor

    def submit(self, context: RuntimeContext, fn: Callable[[], Any]) -> RunHandle:
        """Schedule a scoped run on the bounded pool and return a handle."""

        executor = self._ensure_executor()
        future = executor.submit(_execute_run, self._registry, context, self.worker_id, fn)
        return RunHandle(context=context, _future=future)

    def run_sync(self, context: RuntimeContext, fn: Callable[[], Any]) -> RunResult:
        """Run a scoped callable inline on the calling thread (local default)."""

        return _execute_run(self._registry, context, self.worker_id, fn)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=wait)
