"""Backend-agnostic cron worker execution helper (M8).

The scheduler/storage adapter (any :class:`~agent.runtime_backends.CronBackend`)
owns due-claim leasing and run recording; this helper owns the small execution
path on top of it: claim the due jobs, run each one, route non-silent output to
the optional delivery backend, and record completion/failure through the same
contract. Keeping this logic outside the storage adapter lets local, Compose,
and cloud backends share one worker path.

Secret safety: the worker never stores raw secret values and never records a
raw exception. Failing runs and failed deliveries are sanitized to the
exception type name before being handed to the backend, which applies its own
error-alert semantics.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from agent.runtime_backends import CronBackend, CronLeaseError, DeliveryBackend, _cron_output_is_silent
from agent.runtime_context import RuntimeContext


@dataclass(frozen=True)
class CronWorkerResult:
    """Normalized outcome a runner may return for a single cron firing."""

    output: str | None = None
    next_run_at: float | None = None
    delivery_error: str | None = None


@dataclass(frozen=True)
class CronJobRunResult:
    """Per-job record returned to the caller of :func:`run_due_cron_jobs`."""

    job_id: str
    status: str


Runner = Callable[[Mapping[str, Any]], "str | None | CronWorkerResult"]


def _normalize_runner_result(result: str | None | CronWorkerResult) -> CronWorkerResult:
    if isinstance(result, CronWorkerResult):
        return result
    return CronWorkerResult(output=result)


def _output_is_silent(output: str | None) -> bool:
    return _cron_output_is_silent(output)


def _sanitized_error(exc: BaseException) -> str:
    """Return a non-secret error label for an exception.

    Only the exception type name is surfaced so a runner or delivery failure can
    never leak a raw message (which may embed secrets) into stored run history.
    """

    return type(exc).__name__


def _delivery_message(
    context: RuntimeContext | None,
    job: Mapping[str, Any],
    job_id: str,
    content: str | None,
) -> dict[str, Any]:
    return {
        "kind": "cron_result",
        "job_id": job_id,
        "delivery_ref": getattr(context, "delivery_ref", None),
        "deliver": job.get("deliver"),
        "content": content,
        "binding": job.get("binding"),
    }


def _renew_or_lost(
    context: RuntimeContext | None,
    *,
    backend: CronBackend,
    job_id: str,
    owner: str,
    at: float,
    lease_seconds: float,
) -> bool:
    return backend.renew_lease(context, job_id, owner=owner, now=at, lease_seconds=lease_seconds)


def _run_one_job(
    context: RuntimeContext | None,
    job: Mapping[str, Any],
    *,
    backend: CronBackend,
    owner: str,
    runner: Runner,
    delivery_backend: DeliveryBackend | None,
    clock: Callable[[], float],
    lease_seconds: float,
) -> CronJobRunResult:
    job_id = str(job["id"])
    try:
        result = _normalize_runner_result(runner(job))
    except Exception as exc:  # noqa: BLE001 - sanitized below; siblings must survive
        try:
            entry = backend.fail_run(context, job_id, owner=owner, error=_sanitized_error(exc), now=clock())
        except CronLeaseError:
            return CronJobRunResult(job_id=job_id, status="lease_lost")
        return CronJobRunResult(job_id=job_id, status=str(entry.get("status", "error")))

    output = result.output
    delivery_error = result.delivery_error
    finish_clock = clock()
    if not _renew_or_lost(
        context,
        backend=backend,
        job_id=job_id,
        owner=owner,
        at=finish_clock,
        lease_seconds=lease_seconds,
    ):
        return CronJobRunResult(job_id=job_id, status="lease_lost")
    if delivery_backend is not None and delivery_error is None and not _output_is_silent(output):
        message = _delivery_message(context, job, job_id, output)
        try:
            delivery_backend.deliver(context, message)
        except Exception as exc:  # noqa: BLE001 - sanitized; delivery failure must not crash worker
            delivery_error = _sanitized_error(exc)

    finish_clock = clock()
    if not _renew_or_lost(
        context,
        backend=backend,
        job_id=job_id,
        owner=owner,
        at=finish_clock,
        lease_seconds=lease_seconds,
    ):
        return CronJobRunResult(job_id=job_id, status="lease_lost")
    try:
        entry = backend.complete_run(
            context,
            job_id,
            owner=owner,
            output=output,
            delivery_error=delivery_error,
            now=finish_clock,
            next_run_at=result.next_run_at,
        )
    except CronLeaseError:
        return CronJobRunResult(job_id=job_id, status="lease_lost")
    return CronJobRunResult(job_id=job_id, status=str(entry.get("status", "success")))


def run_due_cron_jobs(
    context: RuntimeContext | None,
    *,
    backend: CronBackend,
    owner: str,
    runner: Runner,
    delivery_backend: DeliveryBackend | None = None,
    now: float | None = None,
    lease_seconds: float = 60.0,
    draining: bool = False,
    limit: int | None = None,
    clock: Callable[[], float] | None = None,
) -> list[CronJobRunResult]:
    """Claim, execute, and record the cron jobs currently due for ``owner``.

    When ``draining`` is set the worker neither claims nor executes new work. A
    runner may return a plain string/None (treated as the run's output) or a
    :class:`CronWorkerResult`. Non-silent output is routed through
    ``delivery_backend`` (if provided) before completion; silent/empty output is
    completed without delivery, preserving the backend's skipped-silent
    semantics. A runner exception fails only that job and never stops siblings.
    """

    if draining:
        return []

    clock_fn = clock or time.time
    claim_clock = clock_fn() if now is None else now
    claimed = backend.claim_due(
        context,
        owner=owner,
        now=claim_clock,
        lease_seconds=lease_seconds,
        limit=limit,
    )

    results: list[CronJobRunResult] = []
    for job in claimed:
        results.append(
            _run_one_job(
                context,
                job,
                backend=backend,
                owner=owner,
                runner=runner,
                delivery_backend=delivery_backend,
                clock=clock_fn,
                lease_seconds=lease_seconds,
            )
        )
    return results
