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
import time
import uuid
from collections import deque
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Mapping

from agent.runtime_backends import BackendCapability, CronLeaseError, RuntimeBackendRegistry, _cron_output_is_silent
from agent.runtime_context import RuntimeContext, use_runtime_context

_CRON_BINDING_FIELDS = frozenset(
    {
        "mode",
        "org_id",
        "workspace_id",
        "user_id",
        "conversation_id",
        "agent_profile_id",
        "project_id",
        "run_type",
        "delivery_ref",
    }
)

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
    CANCELLED = "cancelled"


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


def _execute_run_from_context_data(
    registry: RuntimeBackendRegistry | None,
    context_data: Mapping[str, Any],
    worker_id: str,
    fn: Callable[[], Any],
) -> RunResult:
    """Process-pool friendly run executor using serialized context data."""

    return _execute_run(registry, RuntimeContext(**dict(context_data)), worker_id, fn)


TurnHandler = Callable[[list[Any]], Mapping[str, Any] | Iterable[Mapping[str, Any]] | None]

CronJobHandler = Callable[[Mapping[str, Any]], str | None]


def _cron_run_context(context: RuntimeContext, job: Mapping[str, Any]) -> RuntimeContext:
    """Return a per-firing run context bound to a claimed cron job.

    The durable cron job's sanitized binding restores tenant/user/project/
    conversation/delivery scope, while the polling context supplies the firing
    ``run_id`` prefix. ``job_id`` and the final firing-specific ``run_id`` are
    bound so native surfaces and audit observe the individual occurrence rather
    than a shared worker-wide identity.
    """

    job_id = str(job.get("id"))
    data = context.to_dict()
    binding = job.get("binding")
    if isinstance(binding, Mapping):
        data.update({key: value for key, value in binding.items() if key in _CRON_BINDING_FIELDS})
    data["job_id"] = job_id
    data["run_type"] = "cron"
    base_run_id = context.run_id or data.get("run_id") or "cron"
    data["run_id"] = f"{base_run_id}:{job_id}"
    return RuntimeContext(**data)


def _run_lease_context(context: RuntimeContext, lease_key: str) -> RuntimeContext:
    """Return the scope used for run-to-completion leases.

    Durable job leases intentionally exclude per-execution ``run_id`` so a
    replacement run for the same job/firing cannot bypass the live owner. Runs
    without a durable ``job_id`` keep their run-scoped lease behavior.
    """

    if context.job_id and lease_key == context.job_id:
        data = context.to_dict()
        data["run_id"] = None
        return RuntimeContext(**data)
    return context


def _run_control_key(context: RuntimeContext, lease_key: str) -> tuple[Any, ...]:
    """Return the durable in-process control key for cancellation state."""

    lease_context = _run_lease_context(context, lease_key)
    return (
        lease_context.mode,
        lease_context.org_id,
        lease_context.workspace_id,
        lease_context.user_id,
        lease_context.conversation_id,
        lease_context.agent_profile_id,
        lease_context.project_id,
        lease_context.run_id,
        lease_context.job_id,
        lease_context.backend_profile,
        lease_key,
    )


def _cron_delivery_binding(context: RuntimeContext) -> dict[str, Any]:
    data = context.to_dict()
    return {key: data.get(key) for key in _CRON_BINDING_FIELDS if data.get(key) is not None}


class _LeaseRenewer:
    """Best-effort background lease renewal while a callable is executing."""

    def __init__(
        self,
        renew: Callable[[float], bool],
        *,
        clock: Callable[[], float],
        lease_seconds: float | None,
    ) -> None:
        self._renew = renew
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LeaseRenewer":
        if self._lease_seconds is None or self._lease_seconds <= 0:
            return self
        interval = max(min(self._lease_seconds / 2, 30.0), 0.1)

        def loop() -> None:
            while not self._stop.wait(interval):
                try:
                    if not self._renew(self._clock()):
                        return
                except Exception:
                    return

        self._thread = threading.Thread(target=loop, name="hermes-run-lease-renewer", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)


@dataclass(slots=True)
class _QueuedTurn:
    queue_key: tuple[str | None, ...]
    context: RuntimeContext
    message: Mapping[str, Any]
    handler: TurnHandler
    lock_ttl_seconds: float
    future: Future
    executor_future: Future | None = None


@dataclass(slots=True)
class _WarmConversation:
    context: RuntimeContext
    expires_at: float


def _turn_queue_key(context: RuntimeContext) -> tuple[str | None, ...]:
    """Return the worker-local sequential-turn queue key for a context."""

    return (
        context.mode,
        context.org_id,
        context.workspace_id,
        context.user_id,
        context.agent_profile_id,
        context.project_id,
        context.conversation_id or context.run_id,
    )


def _warm_conversation_key(context: RuntimeContext) -> tuple[str | None, ...]:
    """Return the worker-local warm-run key, including security/backend policy."""

    return (
        *_turn_queue_key(context),
        context.workspace_type,
        context.external_channel_id,
        context.external_thread_id,
        context.permissions_ref,
        context.backend_profile,
        context.delivery_ref,
        repr(context.metadata),
    )


def _normalize_outbound(outbound: Any) -> list[dict[str, Any]]:
    """Coerce a handler result into a list of message mappings to append."""

    if outbound is None:
        return []
    if isinstance(outbound, Mapping):
        return [_session_message(outbound)]
    return [_session_message(message) for message in outbound]


def _session_message(message: Mapping[str, Any]) -> dict[str, Any]:
    """Return a SessionBackend-safe message bound to the active context.

    Turn payloads may come from external requests and must not be able to steer
    writes away from the scoped conversation selected by RuntimeContext.
    """

    sanitized = dict(message)
    sanitized.pop("session_id", None)
    return sanitized


def _execute_turn(
    registry: RuntimeBackendRegistry | None,
    context: RuntimeContext,
    worker_id: str,
    message: Mapping[str, Any],
    handler: TurnHandler,
    *,
    lock_ttl_seconds: float = 300.0,
) -> RunResult:
    if registry is None:
        return RunResult(
            status=RunStatus.FAILED,
            context=context,
            error="process_turn requires a configured backend registry",
        )
    if context.mode == "agentops" and context.run_type == "conversation" and not context.conversation_id:
        return RunResult(
            status=RunStatus.FAILED,
            context=context,
            error="agentops conversation turns require RuntimeContext.conversation_id",
        )
    _record_audit(registry, context, worker_id, RunStatus.STARTED)
    claimed = False
    sessions: Any = None
    lock_owner = f"{worker_id}:{uuid.uuid4().hex}"
    try:
        sessions = registry.get(BackendCapability.SESSION, context)
        if not sessions.claim_turn_lock(context, owner=lock_owner, ttl_seconds=lock_ttl_seconds):
            raise RuntimeError("conversation turn lock is held by another worker")
        claimed = True
        # Scope the session to the active conversation, never to a payload id.
        session_id = context.conversation_id if context.conversation_id else None
        sessions.create_session(context, session_id=session_id)
        sessions.append_message(context, _session_message(message))
        transcript = sessions.read_messages(context)
        with use_runtime_context(context):
            outbound = handler(transcript)
        appended = _normalize_outbound(outbound)
        if not sessions.renew_turn_lock(context, owner=lock_owner, ttl_seconds=lock_ttl_seconds):
            raise RuntimeError("conversation turn lock expired or was lost before outbound write")
        for outbound_message in appended:
            sessions.append_message(context, outbound_message)
    except Exception as exc:  # a turn crashing must not corrupt others
        error = f"{type(exc).__name__}: {exc}"
        audit_error = f"{type(exc).__name__}: {_sanitize_error_for_audit(str(exc))}"
        _record_audit(registry, context, worker_id, RunStatus.FAILED, error=audit_error)
        return RunResult(status=RunStatus.FAILED, context=context, error=error)
    finally:
        if claimed:
            try:
                sessions.release_turn_lock(context, owner=lock_owner)
            except Exception:
                pass
    _record_audit(registry, context, worker_id, RunStatus.SUCCEEDED)
    return RunResult(status=RunStatus.SUCCEEDED, context=context, value=appended)


class LocalRunSupervisor:
    """Runs scoped Hermes run callables locally, inline or concurrently."""

    def __init__(
        self,
        *,
        worker_id: str = "local",
        max_concurrent_runs: int = 1,
        registry: RuntimeBackendRegistry | None = None,
        capabilities: Iterable[str] | None = None,
        max_queued_turns_per_conversation: int = 64,
        run_isolation: str = "thread",
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be >= 1")
        if max_queued_turns_per_conversation < 1:
            raise ValueError("max_queued_turns_per_conversation must be >= 1")
        if run_isolation not in {"thread", "process"}:
            raise ValueError("run_isolation must be 'thread' or 'process'")
        self.worker_id = worker_id
        self.max_concurrent_runs = max_concurrent_runs
        self.max_queued_turns_per_conversation = max_queued_turns_per_conversation
        self.run_isolation = run_isolation
        self.capabilities = tuple(capabilities or ("conversation", "event", "cron", "manual"))
        self._registry = registry
        self._lock = threading.RLock()
        self._executor: ThreadPoolExecutor | None = None
        self._process_executor: ProcessPoolExecutor | None = None
        self._draining = False
        self._shutting_down = False
        self._turn_queues: dict[tuple[str | None, ...], deque[_QueuedTurn]] = {}
        self._warm_conversations: dict[tuple[str | None, ...], _WarmConversation] = {}
        self._cancelled_runs: set[tuple[Any, ...]] = set()
        self._register_worker()

    @property
    def draining(self) -> bool:
        """Whether this worker is refusing new run claims/submissions."""

        return self._draining

    def _register_worker(self) -> None:
        if self._registry is None:
            return
        try:
            workers = self._registry.get(BackendCapability.WORKER_REGISTRY, None)
            workers.register(
                None,
                {
                    "id": self.worker_id,
                    "capacity": self.max_concurrent_runs,
                    "max_concurrent_runs": self.max_concurrent_runs,
                    "capabilities": list(self.capabilities),
                    "slots": {"active": 0, "available": self.max_concurrent_runs},
                },
            )
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

    def _ensure_process_executor(self) -> ProcessPoolExecutor:
        with self._lock:
            if self._process_executor is None:
                self._process_executor = ProcessPoolExecutor(max_workers=self.max_concurrent_runs)
            return self._process_executor

    def submit(self, context: RuntimeContext, fn: Callable[[], Any]) -> RunHandle:
        """Schedule a scoped run on the bounded pool and return a handle."""

        with self._lock:
            if self._draining:
                return self._failed_handle(context, "worker is draining and cannot accept new runs")
            if self._shutting_down:
                return self._failed_handle(context, "worker is shutting down and cannot accept new runs")
            executor = self._ensure_process_executor() if self.run_isolation == "process" else self._ensure_executor()
            if self.run_isolation == "process":
                # RuntimeBackendRegistry instances are mutable in-process
                # selectors and may contain locks or local backends that are
                # not pickle-safe. The child process receives the bound
                # RuntimeContext and can resolve its own backends if needed.
                future = executor.submit(
                    _execute_run_from_context_data,
                    None,
                    context.to_dict(),
                    self.worker_id,
                    fn,
                )
            else:
                future = executor.submit(_execute_run, self._registry, context, self.worker_id, fn)
        return RunHandle(context=context, _future=future)

    def _failed_handle(self, context: RuntimeContext, error: str) -> RunHandle:
        future: Future = Future()
        future.set_result(RunResult(status=RunStatus.FAILED, context=context, error=error))
        return RunHandle(context=context, _future=future)

    def run_sync(self, context: RuntimeContext, fn: Callable[[], Any]) -> RunResult:
        """Run a scoped callable inline on the calling thread (local default)."""

        with self._lock:
            if self._draining:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is draining and cannot accept new runs")
            if self._shutting_down:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is shutting down and cannot accept new runs")
        return _execute_run(self._registry, context, self.worker_id, fn)

    def cancel_run(self, context: RuntimeContext, *, lease_key: str | None = None) -> RunResult:
        """Request cooperative cancellation for a leased run-to-completion job.

        Threads cannot be forcibly killed safely, so cancellation is recorded
        against the durable run/job key. A run that has not entered user code is
        refused before execution; a currently active run observes the request at
        the next lifecycle boundary and reports ``CANCELLED`` instead of
        successful completion.
        """

        key = lease_key or context.job_id or context.run_id or "run"
        with self._lock:
            self._cancelled_runs.add(_run_control_key(context, key))
        _record_audit(self._registry, context, self.worker_id, RunStatus.CANCELLED, error="run cancellation requested")
        return RunResult(status=RunStatus.CANCELLED, context=context, error="run cancellation requested")

    def run_to_completion(
        self,
        context: RuntimeContext,
        fn: Callable[[], Any],
        *,
        lease_key: str | None = None,
        lease_seconds: float = 300.0,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        max_runtime_seconds: float | None = None,
    ) -> RunResult:
        """Run an event/manual/cron callable once under a per-run/job lease.

        A :class:`RunLeaseBackend` lease keyed by ``lease_key`` (defaulting to the
        context's ``job_id``/``run_id`` identity) is claimed before the callable
        executes, so a single run/firing has exactly one live owner and a
        duplicate claim is refused while the lease is held. The lease is released
        whether the callable succeeds or fails, and lifecycle status is recorded
        through the audit backend exactly like :meth:`run_sync`.
        """

        with self._lock:
            if self._draining:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is draining and cannot accept new runs")
            if self._shutting_down:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is shutting down and cannot accept new runs")
        return self._run_to_completion(
            context,
            fn,
            lease_key=lease_key,
            lease_seconds=lease_seconds,
            now=now,
            clock=clock,
            max_runtime_seconds=max_runtime_seconds,
        )

    def submit_run_to_completion(
        self,
        context: RuntimeContext,
        fn: Callable[[], Any],
        *,
        lease_key: str | None = None,
        lease_seconds: float = 300.0,
        now: float | None = None,
        clock: Callable[[], float] | None = None,
        max_runtime_seconds: float | None = None,
    ) -> RunHandle:
        """Schedule a leased run-to-completion callable on the bounded pool."""

        with self._lock:
            if self._draining:
                return self._failed_handle(context, "worker is draining and cannot accept new runs")
            if self._shutting_down:
                return self._failed_handle(context, "worker is shutting down and cannot accept new runs")
            executor = self._ensure_executor()
            future = executor.submit(
                self._run_to_completion,
                context,
                fn,
                lease_key=lease_key,
                lease_seconds=lease_seconds,
                now=now,
                clock=clock,
                max_runtime_seconds=max_runtime_seconds,
            )
        return RunHandle(context=context, _future=future)

    def _run_to_completion(
        self,
        context: RuntimeContext,
        fn: Callable[[], Any],
        *,
        lease_key: str | None,
        lease_seconds: float,
        now: float | None,
        clock: Callable[[], float] | None,
        max_runtime_seconds: float | None,
    ) -> RunResult:
        if self._registry is None:
            return RunResult(
                status=RunStatus.FAILED,
                context=context,
                error="run_to_completion requires a configured backend registry",
            )
        key = lease_key or context.job_id or context.run_id or "run"
        cancel_key = _run_control_key(context, key)
        lease_context = _run_lease_context(context, key)
        owner = f"{self.worker_id}:{uuid.uuid4().hex}"
        lease = self._registry.get(BackendCapability.RUN_LEASE, lease_context)
        lease_clock = clock or time.time
        claim_now = now if now is not None else lease_clock()
        if not lease.claim(lease_context, key, owner=owner, now=claim_now, lease_seconds=lease_seconds):
            return RunResult(
                status=RunStatus.FAILED,
                context=context,
                error=f"run lease {key!r} is held by another owner",
            )
        with self._lock:
            if cancel_key in self._cancelled_runs:
                self._cancelled_runs.discard(cancel_key)
                try:
                    lease.release(lease_context, key, owner=owner)
                except Exception:
                    pass
                _record_audit(self._registry, context, self.worker_id, RunStatus.CANCELLED, error="run was cancelled")
                return RunResult(status=RunStatus.CANCELLED, context=context, error="run was cancelled")
        start_time = claim_now
        try:
            _record_audit(self._registry, context, self.worker_id, RunStatus.STARTED)
            with _LeaseRenewer(
                lambda at: lease.renew(lease_context, key, owner=owner, now=at, lease_seconds=lease_seconds),
                clock=lease_clock,
                lease_seconds=lease_seconds,
            ):
                with use_runtime_context(context):
                    try:
                        value = fn()
                    except Exception as exc:  # one run crashing must not corrupt others
                        with self._lock:
                            self._cancelled_runs.discard(cancel_key)
                        error = f"{type(exc).__name__}: {exc}"
                        audit_error = f"{type(exc).__name__}: {_sanitize_error_for_audit(str(exc))}"
                        _record_audit(self._registry, context, self.worker_id, RunStatus.FAILED, error=audit_error)
                        return RunResult(status=RunStatus.FAILED, context=context, error=error)
            with self._lock:
                was_cancelled = cancel_key in self._cancelled_runs
                self._cancelled_runs.discard(cancel_key)
            if was_cancelled:
                _record_audit(self._registry, context, self.worker_id, RunStatus.CANCELLED, error="run was cancelled")
                return RunResult(status=RunStatus.CANCELLED, context=context, error="run was cancelled")
            finish_time = lease_clock()
            if max_runtime_seconds is not None and finish_time - start_time > max_runtime_seconds:
                error = f"run exceeded max runtime of {max_runtime_seconds} seconds"
                _record_audit(self._registry, context, self.worker_id, RunStatus.FAILED, error=error)
                return RunResult(status=RunStatus.FAILED, context=context, error=error)
            if not lease.renew(lease_context, key, owner=owner, now=finish_time, lease_seconds=lease_seconds):
                error = f"run lease {key!r} was lost before completion"
                _record_audit(self._registry, context, self.worker_id, RunStatus.FAILED, error=error)
                return RunResult(status=RunStatus.FAILED, context=context, error=error)
            _record_audit(self._registry, context, self.worker_id, RunStatus.SUCCEEDED)
            return RunResult(status=RunStatus.SUCCEEDED, context=context, value=value)
        finally:
            try:
                lease.release(lease_context, key, owner=owner)
            except Exception:
                pass

    def run_due_cron_once(
        self,
        context: RuntimeContext,
        handler: CronJobHandler,
        *,
        now: float | None = None,
        lease_seconds: float = 60.0,
        limit: int | None = None,
        next_run_at: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> list[RunResult]:
        """Claim and run every currently-due cron job under per-job leases.

        Each due firing is claimed through :class:`CronBackend` ``claim_due``,
        which leases jobs per job/firing rather than under one global lock, so a
        job whose lease is already held (for example a long autonomous builder
        run) is skipped while unrelated jobs that become due are still claimed,
        executed, and recorded here. ``complete_run``/``fail_run`` advance the
        durable run history for each firing. While draining or shutting down no
        new firings are claimed.
        """

        with self._lock:
            draining = self._draining or self._shutting_down
        if self._registry is None or draining:
            return []
        cron = self._registry.get(BackendCapability.CRON, context)
        owner = f"{self.worker_id}:{uuid.uuid4().hex}"
        run_clock = clock or time.time
        effective_limit = self.max_concurrent_runs if limit is None else min(limit, self.max_concurrent_runs)
        claimed = cron.claim_due(
            context,
            owner=owner,
            now=now,
            lease_seconds=lease_seconds,
            draining=draining,
            limit=effective_limit,
        )
        if not claimed:
            return []
        with ThreadPoolExecutor(max_workers=len(claimed), thread_name_prefix="hermes-cron-once") as executor:
            futures = [
                executor.submit(
                    self._run_claimed_cron_job,
                    context,
                    cron,
                    owner,
                    handler,
                    job,
                    clock=run_clock,
                    now=now,
                    next_run_at=next_run_at,
                    lease_seconds=lease_seconds,
                )
                for job in claimed
            ]
            return [future.result() for future in futures]

    def _run_claimed_cron_job(
        self,
        context: RuntimeContext,
        cron: Any,
        owner: str,
        handler: CronJobHandler,
        job: Mapping[str, Any],
        *,
        clock: Callable[[], float],
        now: float | None,
        next_run_at: float | None,
        lease_seconds: float,
    ) -> RunResult:
        job_id = str(job.get("id"))
        run_context = _cron_run_context(context, job)
        _record_audit(self._registry, run_context, self.worker_id, RunStatus.STARTED)
        output: str | None
        delivery_error: str | None = None
        try:
            with _LeaseRenewer(
                lambda at: cron.renew_lease(context, job_id, owner=owner, now=at, lease_seconds=lease_seconds),
                clock=clock,
                lease_seconds=lease_seconds,
            ):
                try:
                    first_boundary = clock()
                    if not cron.renew_lease(context, job_id, owner=owner, now=first_boundary, lease_seconds=lease_seconds):
                        raise CronLeaseError("cron lease lost before handler")
                    with use_runtime_context(run_context):
                        output = handler(job)
                except Exception as exc:  # one firing crashing must not corrupt others
                    if isinstance(exc, CronLeaseError):
                        raise
                    error = f"{type(exc).__name__}: {exc}"
                    audit_error = type(exc).__name__
                    _record_audit(self._registry, run_context, self.worker_id, RunStatus.FAILED, error=audit_error)
                    try:
                        cron.fail_run(context, job_id, owner=owner, error=audit_error, now=clock(), next_run_at=next_run_at)
                    except Exception:
                        pass
                    return RunResult(status=RunStatus.FAILED, context=run_context, error=error)
                pre_delivery_boundary = clock()
                if not cron.renew_lease(
                    context, job_id, owner=owner, now=pre_delivery_boundary, lease_seconds=lease_seconds
                ):
                    raise CronLeaseError("cron lease lost before delivery")
                if not _cron_output_is_silent(output):
                    try:
                        delivery = self._registry.get(BackendCapability.DELIVERY, run_context)
                        delivery.deliver(
                            run_context,
                            {
                                "kind": "cron_result",
                                "job_id": job_id,
                                "delivery_ref": run_context.delivery_ref,
                                "deliver": job.get("deliver"),
                                "content": output,
                                "binding": _cron_delivery_binding(run_context),
                            },
                        )
                    except Exception as exc:  # noqa: BLE001 - record sanitized delivery failure via cron history
                        delivery_error = type(exc).__name__
                finish_boundary = clock()
                if not cron.renew_lease(context, job_id, owner=owner, now=finish_boundary, lease_seconds=lease_seconds):
                    raise CronLeaseError("cron lease lost before completion")
                entry = cron.complete_run(
                    context,
                    job_id,
                    owner=owner,
                    output=output,
                    delivery_error=delivery_error,
                    now=finish_boundary,
                    next_run_at=next_run_at,
                )
        except CronLeaseError as exc:
            error = f"{type(exc).__name__}: {exc}"
            _record_audit(self._registry, run_context, self.worker_id, RunStatus.FAILED, error=error)
            return RunResult(status=RunStatus.FAILED, context=run_context, error=error)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _record_audit(
                self._registry, run_context, self.worker_id, RunStatus.FAILED, error=_sanitize_error_for_audit(error)
            )
            return RunResult(status=RunStatus.FAILED, context=run_context, error=error)
        if delivery_error is not None or entry.get("status") == "delivery_error":
            error = delivery_error or str(entry.get("error") or "cron delivery failed")
            _record_audit(self._registry, run_context, self.worker_id, RunStatus.FAILED, error=error)
            return RunResult(status=RunStatus.FAILED, context=run_context, value=output, error=error)
        _record_audit(self._registry, run_context, self.worker_id, RunStatus.SUCCEEDED)
        return RunResult(status=RunStatus.SUCCEEDED, context=run_context, value=output)

    def process_turn(
        self,
        context: RuntimeContext,
        message: Mapping[str, Any],
        handler: TurnHandler,
        *,
        lock_ttl_seconds: float = 300.0,
    ) -> RunResult:
        """Run one scoped conversation turn against the selected SessionBackend.

        The inbound ``message`` is persisted to the registry-selected
        :class:`SessionBackend` under a per-conversation turn lock owned by this
        worker, the scoped transcript is handed to ``handler``, and any returned
        outbound/tool message(s) are appended to the same backend before the lock
        is released. With no registry configured this fails cleanly with a
        ``FAILED`` :class:`RunResult` and mutates no state.
        """

        with self._lock:
            if self._draining:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is draining and cannot accept new runs")
            if self._shutting_down:
                return RunResult(status=RunStatus.FAILED, context=context, error="worker is shutting down and cannot accept new runs")
        return _execute_turn(
            self._registry,
            context,
            self.worker_id,
            message,
            handler,
            lock_ttl_seconds=lock_ttl_seconds,
        )

    def submit_turn(
        self,
        context: RuntimeContext,
        message: Mapping[str, Any],
        handler: TurnHandler,
        *,
        lock_ttl_seconds: float = 300.0,
    ) -> RunHandle:
        """Queue one scoped conversation turn on the worker pool.

        Unlike :meth:`process_turn`, which is an immediate/synchronous helper,
        this method serializes turns for the same conversation before scheduling
        the next same-conversation turn onto the bounded executor. A hot
        conversation backlog therefore cannot consume all worker slots while it
        waits, and different conversations can still run independently.
        """

        future: Future = Future()
        with self._lock:
            if self._draining:
                future.set_result(RunResult(status=RunStatus.FAILED, context=context, error="worker is draining and cannot accept new runs"))
                return RunHandle(context=context, _future=future)
            if self._shutting_down:
                future.set_result(RunResult(status=RunStatus.FAILED, context=context, error="worker is shutting down and cannot accept new runs"))
                return RunHandle(context=context, _future=future)
            queue_key = _turn_queue_key(context)
            queue = self._turn_queues.setdefault(queue_key, deque())
            if len(queue) >= self.max_queued_turns_per_conversation:
                future.set_result(RunResult(status=RunStatus.FAILED, context=context, error="conversation turn queue is full"))
                return RunHandle(context=context, _future=future)
            queued = _QueuedTurn(queue_key, context, dict(message), handler, lock_ttl_seconds, future)
            should_schedule = not queue
            queue.append(queued)
            if should_schedule:
                self._schedule_queued_turn(queued)
        return RunHandle(context=context, _future=future)

    def submit_warm_turn(
        self,
        context: RuntimeContext,
        message: Mapping[str, Any],
        handler: TurnHandler,
        *,
        idle_timeout_seconds: float,
        now: float | None = None,
        lock_ttl_seconds: float = 300.0,
    ) -> RunHandle:
        """Queue a conversation turn on an active warm run when possible.

        The first turn for a conversation records its ``RuntimeContext`` as the
        active warm run. Follow-up turns arriving before ``idle_timeout_seconds``
        expires are processed with that active context, so native session,
        audit, and tool paths observe the same run owner. Once idle, a later
        turn starts a fresh active run using the incoming context while durable
        session state remains scoped to the same conversation.
        """

        if idle_timeout_seconds <= 0:
            raise ValueError("idle_timeout_seconds must be > 0")
        if context.run_type != "conversation":
            return self._failed_handle(context, "warm turns require RuntimeContext.run_type='conversation'")
        current_time = time.time() if now is None else now
        queue_key = _warm_conversation_key(context)
        with self._lock:
            if self._draining:
                return self._failed_handle(context, "worker is draining and cannot accept new runs")
            if self._shutting_down:
                return self._failed_handle(context, "worker is shutting down and cannot accept new runs")
            active = self._warm_conversations.get(queue_key)
            active_turns_pending = active is not None and _turn_queue_key(active.context) in self._turn_queues
            if active is None or (active.expires_at <= current_time and not active_turns_pending):
                active = _WarmConversation(context=context, expires_at=current_time + idle_timeout_seconds)
                self._warm_conversations[queue_key] = active
            else:
                active.expires_at = current_time + idle_timeout_seconds
            active_context = active.context
        return self.submit_turn(
            active_context,
            message,
            handler,
            lock_ttl_seconds=lock_ttl_seconds,
        )

    def reap_idle_conversations(self, *, now: float | None = None) -> int:
        """Drop locally cached warm-run records whose idle timeout expired."""

        current_time = time.time() if now is None else now
        with self._lock:
            expired = [
                key
                for key, warm in self._warm_conversations.items()
                if warm.expires_at <= current_time and _turn_queue_key(warm.context) not in self._turn_queues
            ]
            for key in expired:
                self._warm_conversations.pop(key, None)
        return len(expired)

    def _schedule_queued_turn(self, queued: _QueuedTurn) -> None:
        if self._draining or self._shutting_down:
            queued.future.set_result(
                RunResult(status=RunStatus.FAILED, context=queued.context, error="worker is draining and cannot accept queued turn")
            )
            self._finish_queued_turn(queued)
            return
        executor = self._ensure_executor()
        queued.executor_future = executor.submit(self._execute_queued_turn, queued)

    def _execute_queued_turn(self, queued: _QueuedTurn) -> None:
        result = _execute_turn(
            self._registry,
            queued.context,
            self.worker_id,
            queued.message,
            queued.handler,
            lock_ttl_seconds=queued.lock_ttl_seconds,
        )
        self._finish_queued_turn(queued)
        queued.future.set_result(result)

    def _finish_queued_turn(self, queued: _QueuedTurn) -> None:
        with self._lock:
            queue = self._turn_queues.get(queued.queue_key)
            if not queue:
                return
            completed = queue.popleft()
            if completed is not queued:
                completed.future.set_result(
                    RunResult(
                        status=RunStatus.FAILED,
                        context=completed.context,
                        error="conversation turn queue ordering was corrupted",
                    )
                )
                self._turn_queues.pop(queued.queue_key, None)
                return
            if queue:
                self._schedule_queued_turn(queue[0])
            else:
                self._turn_queues.pop(queued.queue_key, None)

    def _fail_pending_queued_turns_locked(self, error: str) -> None:
        """Fail queued-but-not-active turns while preserving active heads."""

        for queue_key, queue in list(self._turn_queues.items()):
            if len(queue) <= 1:
                continue
            active = queue.popleft()
            while queue:
                pending = queue.popleft()
                pending.future.set_result(RunResult(status=RunStatus.FAILED, context=pending.context, error=error))
            self._turn_queues[queue_key] = deque([active])

    def _cancel_unstarted_queued_heads_locked(self, error: str) -> None:
        """Cancel queued head tasks that are scheduled but not yet running."""

        for queue_key, queue in list(self._turn_queues.items()):
            if not queue:
                self._turn_queues.pop(queue_key, None)
                continue
            queued = queue[0]
            executor_future = queued.executor_future
            if executor_future is not None and executor_future.cancel():
                queued.future.set_result(RunResult(status=RunStatus.FAILED, context=queued.context, error=error))
                self._turn_queues.pop(queue_key, None)

    def request_drain(self) -> RunResult:
        """Stop accepting new work and mark the worker draining in the registry.

        In-flight futures are left alone so they can finish or be shut down by
        the caller. This mirrors distributed worker drain semantics: the worker
        stops new claims first, then releases active slots through normal
        completion/shutdown paths.
        """

        with self._lock:
            self._draining = True
            self._warm_conversations.clear()
            self._fail_pending_queued_turns_locked("worker is draining and cannot accept queued turn")
            self._cancel_unstarted_queued_heads_locked("worker is draining and cannot accept queued turn")
            if self._registry is not None:
                try:
                    workers = self._registry.get(BackendCapability.WORKER_REGISTRY, None)
                    workers.mark_draining(None, self.worker_id)
                except Exception as exc:
                    return RunResult(
                        status=RunStatus.FAILED,
                        context=RuntimeContext(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
        return RunResult(status=RunStatus.SUCCEEDED, context=RuntimeContext())

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._shutting_down = True
            self._warm_conversations.clear()
            self._fail_pending_queued_turns_locked("worker is shutting down and cannot accept queued turn")
            self._cancel_unstarted_queued_heads_locked("worker is shutting down and cannot accept queued turn")
            executor, self._executor = self._executor, None
            process_executor, self._process_executor = self._process_executor, None
        if executor is not None:
            executor.shutdown(wait=wait)
        if process_executor is not None:
            process_executor.shutdown(wait=wait)
