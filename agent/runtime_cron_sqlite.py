"""Durable SQLite implementation of the provider-neutral CronBackend contract.

``LocalCronBackend`` remains the in-process compatibility backend. This module
adds a small durable local/control-plane-like backend for M8 so cron jobs,
leases, and run history can survive scheduler/worker process restarts while
preserving the same logical RuntimeContext-scoped job model.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from agent.runtime_backends import (
    CronLeaseError,
    _cron_job_binding,
    _cron_output_is_silent,
    _cron_scope_key,
)
from agent.runtime_context import RuntimeContext

_SECRET_KEY_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "permissions_ref",
    "permissionsref",
)
_SECRET_VALUE_MARKERS = (
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "bearer ",
)
_REDACTED = "[REDACTED]"


class SQLiteCronBackend:
    """SQLite-backed ``CronBackend`` with durable jobs, leases, and history.

    The schema stores JSON records behind a provider-neutral contract. Scope keys
    are derived from ``RuntimeContext`` using the same stable cron scope as the
    local in-process backend, so new run/job ids for the same conversation still
    see the same durable jobs while other tenants/conversations remain isolated.
    """

    def __init__(self, path: str | Path, *, timeout: float = 30.0) -> None:
        self._path = Path(path)
        if str(self._path) != ":memory":
            self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, timeout=timeout, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._migrate()

    def _configure(self) -> None:
        with self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    scope_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_key, job_id)
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_leases (
                    scope_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (scope_key, job_id),
                    FOREIGN KEY (scope_key, job_id)
                        REFERENCES cron_jobs(scope_key, job_id)
                        ON DELETE CASCADE
                )
                """
            )
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cron_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    entry_json TEXT NOT NULL,
                    at REAL NOT NULL,
                    FOREIGN KEY (scope_key, job_id)
                        REFERENCES cron_jobs(scope_key, job_id)
                        ON DELETE CASCADE
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_cron_history_job ON cron_history(scope_key, job_id, id)")

    @staticmethod
    def _redact_text(value: str) -> str:
        lowered = value.lower()
        if _REDACTED.lower() in lowered or any(marker in lowered for marker in _SECRET_VALUE_MARKERS):
            return _REDACTED
        return value

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if isinstance(value, Mapping):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key)
                if any(marker in key_text.lower() for marker in _SECRET_KEY_MARKERS):
                    sanitized[key_text] = _REDACTED
                else:
                    sanitized[key_text] = cls._sanitize_value(item)
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
        if isinstance(value, tuple):
            return [cls._sanitize_value(item) for item in value]
        if isinstance(value, str):
            return cls._redact_text(value)
        return copy.deepcopy(value)

    @classmethod
    def _sanitize_job(cls, job: Mapping[str, Any], context: RuntimeContext | None) -> dict[str, Any]:
        sanitized = cls._sanitize_value(dict(job))
        if not isinstance(sanitized, dict):
            sanitized = {}
        # Binding is backend-owned scope metadata; never accept a caller-supplied
        # binding that could smuggle permissions_ref/credential data into durable
        # job records.
        sanitized.pop("binding", None)
        sanitized["binding"] = _cron_job_binding(context)
        return sanitized

    @classmethod
    def _sanitize_error(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return cls._redact_text(str(value))

    @staticmethod
    def _scope(context: RuntimeContext | None) -> str:
        return json.dumps(_cron_scope_key(context), separators=(",", ":"), sort_keys=False)

    @staticmethod
    def _normalize_repeat(job: Mapping[str, Any]) -> dict[str, Any]:
        raw = job.get("repeat")
        if isinstance(raw, Mapping):
            times = raw.get("times")
            completed = raw.get("completed", 0)
        elif isinstance(raw, int):
            times = raw if raw > 0 else None
            completed = 0
        elif job.get("one_shot"):
            times, completed = 1, 0
        else:
            times, completed = None, 0
        return {"times": times, "completed": int(completed or 0)}

    @staticmethod
    def _is_done(record: Mapping[str, Any]) -> bool:
        repeat = record.get("repeat") or {}
        times = repeat.get("times")
        return times is not None and int(repeat.get("completed", 0)) >= int(times)

    @staticmethod
    def _encode(value: Mapping[str, Any]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(raw: str) -> dict[str, Any]:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise RuntimeError("SQLiteCronBackend stored non-object job record")
        return payload

    def _begin_immediate(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def _load_job(self, scope: str, job_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT record_json FROM cron_jobs WHERE scope_key = ? AND job_id = ?",
            (scope, job_id),
        ).fetchone()
        return self._decode(row["record_json"]) if row is not None else None

    def _store_job(self, scope: str, job_id: str, record: Mapping[str, Any], now: float) -> None:
        self._conn.execute(
            """
            UPDATE cron_jobs
               SET record_json = ?, updated_at = ?
             WHERE scope_key = ? AND job_id = ?
            """,
            (self._encode(record), now, scope, job_id),
        )

    @staticmethod
    def _next_run_due_at(next_run_at: Any) -> float:
        if isinstance(next_run_at, (int, float)):
            return float(next_run_at)
        if isinstance(next_run_at, str):
            text = next_run_at.strip()
            if not text:
                return 0.0
            try:
                return float(text)
            except ValueError:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
        return float(next_run_at)

    def _is_due(self, record: dict[str, Any], now: float) -> bool:
        if record.get("paused") or record.get("state") in {"paused", "completed", "needs_schedule"}:
            return False
        if self._is_done(record):
            return False
        next_run_at = record.get("next_run_at")
        if next_run_at is None:
            return True
        try:
            return self._next_run_due_at(next_run_at) <= now
        except (TypeError, ValueError, OverflowError) as exc:
            record["state"] = "needs_schedule"
            record["next_run_at_error"] = self._sanitize_error(str(exc))
            return False

    def create(self, context: RuntimeContext | None, job: Mapping[str, Any]) -> str:
        now = time.time()
        job_id = uuid.uuid4().hex
        sanitized_job = self._sanitize_job(job, context)
        record = {
            **sanitized_job,
            "id": job_id,
            "paused": False,
            "state": "scheduled",
            "repeat": self._normalize_repeat(job),
            "deliver": sanitized_job.get("deliver", "local"),
            "binding": _cron_job_binding(context),
        }
        scope = self._scope(context)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO cron_jobs(scope_key, job_id, record_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (scope, job_id, self._encode(record), now, now),
            )
        return job_id

    def update(self, context: RuntimeContext | None, job_id: str, job: Mapping[str, Any]) -> None:
        scope = self._scope(context)
        now = time.time()
        with self._lock, self._conn:
            self._begin_immediate()
            record = self._load_job(scope, job_id)
            if record is None:
                return
            updates = self._sanitize_job(job, context)
            updates.pop("binding", None)
            record.update(updates)
            record["binding"] = _cron_job_binding(context)
            self._store_job(scope, job_id, record, now)

    def get_job(self, context: RuntimeContext | None, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._load_job(self._scope(context), job_id)
            return copy.deepcopy(record) if record is not None else None

    def pause(self, context: RuntimeContext | None, job_id: str) -> None:
        scope = self._scope(context)
        now = time.time()
        with self._lock, self._conn:
            self._begin_immediate()
            record = self._load_job(scope, job_id)
            if record is None:
                return
            record["paused"] = True
            record["state"] = "paused"
            self._store_job(scope, job_id, record, now)

    def resume(self, context: RuntimeContext | None, job_id: str) -> None:
        scope = self._scope(context)
        now = time.time()
        with self._lock, self._conn:
            self._begin_immediate()
            record = self._load_job(scope, job_id)
            if record is None:
                return
            record["paused"] = False
            if record.get("state") == "paused":
                record["state"] = "scheduled"
            self._store_job(scope, job_id, record, now)

    def remove(self, context: RuntimeContext | None, job_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM cron_jobs WHERE scope_key = ? AND job_id = ?", (self._scope(context), job_id))

    def list_jobs(self, context: RuntimeContext | None) -> list[Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT record_json FROM cron_jobs WHERE scope_key = ? ORDER BY created_at, job_id",
                (self._scope(context),),
            ).fetchall()
            return [self._decode(row["record_json"]) for row in rows]

    def run_history(self, context: RuntimeContext | None, job_id: str) -> list[Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entry_json FROM cron_history WHERE scope_key = ? AND job_id = ? ORDER BY id",
                (self._scope(context), job_id),
            ).fetchall()
            return [json.loads(row["entry_json"]) for row in rows]

    def _lease_is_live(self, scope: str, job_id: str, now: float) -> tuple[str, float] | None:
        row = self._conn.execute(
            "SELECT owner, expires_at FROM cron_leases WHERE scope_key = ? AND job_id = ?",
            (scope, job_id),
        ).fetchone()
        if row is None:
            return None
        lease = (row["owner"], float(row["expires_at"]))
        return lease if lease[1] > now else None

    def claim_due(
        self,
        context: RuntimeContext | None,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
        draining: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        if draining:
            return []
        clock = time.time() if now is None else now
        claimed: list[dict[str, Any]] = []
        scope = self._scope(context)
        with self._lock, self._conn:
            self._begin_immediate()
            rows = self._conn.execute(
                "SELECT job_id, record_json FROM cron_jobs WHERE scope_key = ? ORDER BY created_at, job_id",
                (scope,),
            ).fetchall()
            for row in rows:
                job_id = row["job_id"]
                record = self._decode(row["record_json"])
                due = self._is_due(record, clock)
                # Persist fail-closed state changes caused by malformed schedules.
                if record != self._decode(row["record_json"]):
                    self._store_job(scope, job_id, record, clock)
                if not due:
                    continue
                if self._lease_is_live(scope, job_id, clock) is not None:
                    continue
                lease_expires_at = clock + lease_seconds
                self._conn.execute(
                    """
                    INSERT INTO cron_leases(scope_key, job_id, owner, expires_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(scope_key, job_id) DO UPDATE SET
                        owner = excluded.owner,
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (scope, job_id, owner, lease_expires_at, clock),
                )
                record["state"] = "running"
                self._store_job(scope, job_id, record, clock)
                claim = copy.deepcopy(record)
                claim["owner"] = owner
                claim["lease_expires_at"] = lease_expires_at
                claimed.append(claim)
                if limit is not None and len(claimed) >= limit:
                    break
        return claimed

    def _require_lease(self, scope: str, job_id: str, owner: str, now: float) -> None:
        row = self._conn.execute(
            "SELECT owner, expires_at FROM cron_leases WHERE scope_key = ? AND job_id = ?",
            (scope, job_id),
        ).fetchone()
        if row is None or row["owner"] != owner or float(row["expires_at"]) <= now:
            raise CronLeaseError(f"worker {owner!r} does not hold the lease for cron job {job_id!r}")

    def renew_lease(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        now: float | None = None,
        lease_seconds: float = 60.0,
    ) -> bool:
        clock = time.time() if now is None else now
        scope = self._scope(context)
        with self._lock, self._conn:
            self._begin_immediate()
            row = self._conn.execute(
                "SELECT owner, expires_at FROM cron_leases WHERE scope_key = ? AND job_id = ?",
                (scope, job_id),
            ).fetchone()
            if row is None or row["owner"] != owner or float(row["expires_at"]) <= clock:
                return False
            self._conn.execute(
                "UPDATE cron_leases SET expires_at = ?, updated_at = ? WHERE scope_key = ? AND job_id = ?",
                (clock + lease_seconds, clock, scope, job_id),
            )
            return True

    def release_lease(self, context: RuntimeContext | None, job_id: str, *, owner: str) -> None:
        scope = self._scope(context)
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM cron_leases WHERE scope_key = ? AND job_id = ? AND owner = ?",
                (scope, job_id, owner),
            )

    def _finish_run(
        self,
        scope: str,
        job_id: str,
        owner: str,
        entry: Mapping[str, Any],
        next_run_at: float | None,
        now: float,
    ) -> dict[str, Any]:
        self._require_lease(scope, job_id, owner, now)
        record = self._load_job(scope, job_id)
        if record is not None:
            repeat = record.setdefault("repeat", {"times": None, "completed": 0})
            repeat["completed"] = int(repeat.get("completed", 0)) + 1
            if next_run_at is not None:
                record["next_run_at"] = next_run_at
            if self._is_done(record):
                record["state"] = "completed"
            elif next_run_at is None:
                record["state"] = "needs_schedule"
            elif not record.get("paused"):
                record["state"] = "scheduled"
            self._store_job(scope, job_id, record, now)
        stored = copy.deepcopy(dict(entry))
        self._conn.execute(
            "INSERT INTO cron_history(scope_key, job_id, entry_json, at) VALUES (?, ?, ?, ?)",
            (scope, job_id, self._encode(stored), now),
        )
        self._conn.execute(
            "DELETE FROM cron_leases WHERE scope_key = ? AND job_id = ? AND owner = ?",
            (scope, job_id, owner),
        )
        return copy.deepcopy(stored)

    def complete_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        output: str | None = None,
        delivery_error: str | None = None,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time() if now is None else now
        silent = _cron_output_is_silent(output)
        if delivery_error:
            status, delivery, error = "delivery_error", "error", self._sanitize_error(delivery_error)
        elif silent:
            status, delivery, error = "success", "skipped_silent", None
        else:
            status, delivery, error = "success", "delivered", None
        entry = {
            "status": status,
            "owner": owner,
            "output_empty": output is None or not str(output).strip(),
            "silent": silent,
            "delivery": delivery,
            "error": error,
            "at": clock,
        }
        with self._lock, self._conn:
            self._begin_immediate()
            return self._finish_run(self._scope(context), job_id, owner, entry, next_run_at, clock)

    def fail_run(
        self,
        context: RuntimeContext | None,
        job_id: str,
        *,
        owner: str,
        error: str,
        now: float | None = None,
        next_run_at: float | None = None,
    ) -> dict[str, Any]:
        clock = time.time() if now is None else now
        entry = {
            "status": "error",
            "owner": owner,
            "output_empty": True,
            "silent": False,
            "delivery": "error_alert",
            "error": self._sanitize_error(error),
            "at": clock,
        }
        with self._lock, self._conn:
            self._begin_immediate()
            return self._finish_run(self._scope(context), job_id, owner, entry, next_run_at, clock)
