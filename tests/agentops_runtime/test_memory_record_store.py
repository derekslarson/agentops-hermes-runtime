"""TDD tests for RelationalMemoryRecordBackend (M5B durable store slice).

Contract: upsert/get, scope isolation, idempotency, durability, safe errors.
All tests written before implementation — run RED first, then GREEN.
"""
from __future__ import annotations

import sys
import traceback
import re

import pytest
from agent.runtime_context import RuntimeContext


def _ctx(**kwargs) -> RuntimeContext:
    base = {
        "mode": "agentops",
        "org_id": "org1",
        "workspace_id": "ws1",
        "project_id": "proj1",
        "agent_profile_id": "bot",
        "conversation_id": "conv1",
        "user_id": "alice",
    }
    base.update(kwargs)
    return RuntimeContext(**base)


_CTX_A = _ctx(user_id="alice", conversation_id="conv-alice")
_CTX_B = _ctx(user_id="bob", conversation_id="conv-bob")


@pytest.fixture
def backend(tmp_path):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    return RelationalMemoryRecordBackend(f"sqlite:///{tmp_path}/test.db")


# ---------------------------------------------------------------------------
# upsert / get
# ---------------------------------------------------------------------------


def test_upsert_returns_record_with_generated_id(backend):
    record = backend.upsert_record(_CTX_A, text="hello world")
    assert record.id
    assert record.id.startswith("mem_")
    assert record.text == "hello world"


def test_get_round_trip(backend):
    record = backend.upsert_record(_CTX_A, text="round trip text", record_id="mem_rt_001")
    fetched = backend.get_record(_CTX_A, "mem_rt_001")
    assert fetched is not None
    assert fetched.id == "mem_rt_001"
    assert fetched.text == "round trip text"


def test_get_returns_none_for_unknown_id(backend):
    assert backend.get_record(_CTX_A, "mem_does_not_exist") is None


def test_upsert_preserves_explicit_record_id(backend):
    record = backend.upsert_record(_CTX_A, text="explicit id", record_id="mem_explicit_001")
    assert record.id == "mem_explicit_001"
    assert backend.get_record(_CTX_A, "mem_explicit_001") is not None


def test_upsert_preserves_source_and_provenance(backend):
    record = backend.upsert_record(
        _CTX_A,
        text="provenance test",
        record_id="mem_prov_001",
        source="test_source",
        source_uri="https://example.com/doc",
        provenance={"run_id": "r123"},
        record_kind="verbatim",
    )
    fetched = backend.get_record(_CTX_A, "mem_prov_001")
    assert fetched is not None
    assert fetched.source == "test_source"
    assert fetched.source_uri == "https://example.com/doc"
    assert fetched.provenance == {"run_id": "r123"}
    assert fetched.record_kind == "verbatim"


# ---------------------------------------------------------------------------
# Idempotent upsert
# ---------------------------------------------------------------------------


def test_idempotent_upsert_updates_text(backend):
    backend.upsert_record(_CTX_A, text="original", record_id="mem_idem_001")
    backend.upsert_record(_CTX_A, text="updated", record_id="mem_idem_001")
    fetched = backend.get_record(_CTX_A, "mem_idem_001")
    assert fetched is not None
    assert fetched.text == "updated"
    assert fetched.id == "mem_idem_001"


def test_idempotent_upsert_only_one_record_stored(backend):
    backend.upsert_record(_CTX_A, text="first", record_id="mem_idem_002")
    backend.upsert_record(_CTX_A, text="second", record_id="mem_idem_002")
    backend.upsert_record(_CTX_A, text="third", record_id="mem_idem_002")
    results = backend.search(_CTX_A, "second third first")
    ids = [r.id for r in results]
    assert ids.count("mem_idem_002") == 1


# ---------------------------------------------------------------------------
# Scope isolation — same record_id in two scopes
# ---------------------------------------------------------------------------


def test_same_record_id_in_different_scopes_no_collision(backend):
    backend.upsert_record(_CTX_A, text="alice's content", record_id="mem_shared")
    backend.upsert_record(_CTX_B, text="bob's content", record_id="mem_shared")

    fetched_a = backend.get_record(_CTX_A, "mem_shared")
    fetched_b = backend.get_record(_CTX_B, "mem_shared")

    assert fetched_a is not None
    assert fetched_b is not None
    assert fetched_a.text == "alice's content"
    assert fetched_b.text == "bob's content"


def test_get_record_cross_scope_returns_none(backend):
    backend.upsert_record(_CTX_A, text="alice only", record_id="mem_alice_only")
    assert backend.get_record(_CTX_B, "mem_alice_only") is None


# ---------------------------------------------------------------------------
# get_many
# ---------------------------------------------------------------------------


def test_get_many_basic(backend):
    backend.upsert_record(_CTX_A, text="record one", record_id="mem_gm1")
    backend.upsert_record(_CTX_A, text="record two", record_id="mem_gm2")
    results = backend.get_many(_CTX_A, ["mem_gm1", "mem_gm2"])
    ids = {r.id for r in results}
    assert ids == {"mem_gm1", "mem_gm2"}


def test_get_many_omits_cross_scope_ids(backend):
    backend.upsert_record(_CTX_A, text="alice", record_id="mem_scope_a")
    backend.upsert_record(_CTX_B, text="bob", record_id="mem_scope_b")
    results = backend.get_many(_CTX_A, ["mem_scope_a", "mem_scope_b"])
    ids = {r.id for r in results}
    assert "mem_scope_a" in ids
    assert "mem_scope_b" not in ids


def test_get_many_omits_missing_ids(backend):
    backend.upsert_record(_CTX_A, text="real", record_id="mem_real")
    results = backend.get_many(_CTX_A, ["mem_real", "mem_missing"])
    ids = {r.id for r in results}
    assert "mem_real" in ids
    assert "mem_missing" not in ids


def test_get_many_empty_returns_empty(backend):
    assert backend.get_many(_CTX_A, []) == []


def test_get_many_preserves_requested_id_order(backend):
    for record_id in ["mem_order_1", "mem_order_2", "mem_order_3"]:
        backend.upsert_record(_CTX_A, text=record_id, record_id=record_id)

    records = backend.get_many(_CTX_A, ["mem_order_3", "mem_order_1", "missing", "mem_order_2"])

    assert [record.id for record in records] == ["mem_order_3", "mem_order_1", "mem_order_2"]


# ---------------------------------------------------------------------------
# Keyword search — scoped, bounded excerpts
# ---------------------------------------------------------------------------


def test_search_returns_scoped_results_only(backend):
    backend.upsert_record(_CTX_A, text="alice deep learning notes", record_id="mem_search_a")
    backend.upsert_record(_CTX_B, text="bob deep learning notes", record_id="mem_search_b")

    results = backend.search(_CTX_A, "deep learning")
    ids = [r.id for r in results]
    assert "mem_search_a" in ids
    assert "mem_search_b" not in ids


def test_search_returns_empty_for_no_match(backend):
    backend.upsert_record(_CTX_A, text="hello world content", record_id="mem_no_match")
    assert backend.search(_CTX_A, "quantum cryptography") == []


def test_search_excerpt_is_bounded(backend):
    long_text = "keyword " + ("x " * 600)
    backend.upsert_record(_CTX_A, text=long_text, record_id="mem_long_text")
    results = backend.search(_CTX_A, "keyword")
    assert len(results) >= 1
    r = results[0]
    # excerpt must be bounded — not the full 1200+ char text
    assert len(r.excerpt) <= 515  # 512 + "..." = 515


def test_search_text_field_is_bounded_not_full_verbatim(backend):
    long_text = "keyword " + ("y " * 600)
    backend.upsert_record(_CTX_A, text=long_text, record_id="mem_long_text2")
    results = backend.search(_CTX_A, "keyword")
    assert len(results) >= 1
    assert len(results[0].text) <= 515


def test_search_result_has_valid_fields(backend):
    backend.upsert_record(
        _CTX_A,
        text="test result fields",
        record_id="mem_fields",
        source="my_source",
        record_kind="verbatim",
    )
    results = backend.search(_CTX_A, "test result")
    assert len(results) >= 1
    r = results[0]
    assert r.id == "mem_fields"
    assert r.score > 0
    assert r.rank == 0
    assert r.source == "my_source"
    assert r.record_kind == "verbatim"


def test_search_respects_limit(backend):
    for i in range(10):
        backend.upsert_record(_CTX_A, text=f"common keyword record {i}", record_id=f"mem_limit_{i}")
    results = backend.search(_CTX_A, "common keyword", limit=3)
    assert len(results) <= 3


def test_search_clamps_negative_limit_to_one(backend):
    for i in range(3):
        backend.upsert_record(_CTX_A, text=f"negative limit keyword {i}", record_id=f"mem_neg_limit_{i}")
    results = backend.search(_CTX_A, "negative keyword", limit=-1)
    assert len(results) == 1


def test_search_applies_metadata_filters(backend):
    backend.upsert_record(
        _CTX_A,
        text="filtered keyword keep",
        record_id="mem_filter_keep",
        metadata={"kind": "keep"},
    )
    backend.upsert_record(
        _CTX_A,
        text="filtered keyword drop",
        record_id="mem_filter_drop",
        metadata={"kind": "drop"},
    )

    results = backend.search(_CTX_A, "filtered keyword", filters={"kind": "keep"})
    assert [r.id for r in results] == ["mem_filter_keep"]


def test_search_rejects_unknown_candidate_strategy(backend):
    backend.upsert_record(_CTX_A, text="strategy keyword", record_id="mem_strategy")
    with pytest.raises(ValueError):
        backend.search(_CTX_A, "strategy", candidate_strategy="unsupported")


@pytest.mark.parametrize("candidate_strategy", ["vector", "bm25"])
def test_search_rejects_declared_unimplemented_candidate_strategies(backend, candidate_strategy):
    backend.upsert_record(_CTX_A, text="strategy keyword", record_id=f"mem_strategy_{candidate_strategy}")
    with pytest.raises(ValueError):
        backend.search(_CTX_A, "strategy", candidate_strategy=candidate_strategy)


def test_search_excerpts_are_bounded(backend):
    long_text = "bounded " + ("x" * 700)
    backend.upsert_record(_CTX_A, text=long_text, record_id="mem_bounded_excerpt")

    result = backend.search(_CTX_A, "bounded")[0]

    assert result.id == "mem_bounded_excerpt"
    assert len(result.excerpt) <= 512
    assert result.text == result.excerpt


# ---------------------------------------------------------------------------
# Durability — fresh backend instance reads same file
# ---------------------------------------------------------------------------


def test_durability_across_fresh_instances(tmp_path):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    db_file = str(tmp_path / "durable.db")
    b1 = RelationalMemoryRecordBackend(f"sqlite:///{db_file}")
    b1.upsert_record(_CTX_A, text="durable content", record_id="mem_durable_001")

    b2 = RelationalMemoryRecordBackend(f"sqlite:///{db_file}")
    fetched = b2.get_record(_CTX_A, "mem_durable_001")
    assert fetched is not None
    assert fetched.text == "durable content"
    assert fetched.id == "mem_durable_001"


def test_durability_scope_isolation_across_fresh_instances(tmp_path):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    db_file = str(tmp_path / "durable_scope.db")
    b1 = RelationalMemoryRecordBackend(f"sqlite:///{db_file}")
    b1.upsert_record(_CTX_A, text="alice durable", record_id="mem_dur_scope")

    b2 = RelationalMemoryRecordBackend(f"sqlite:///{db_file}")
    assert b2.get_record(_CTX_B, "mem_dur_scope") is None
    assert b2.get_record(_CTX_A, "mem_dur_scope") is not None


# ---------------------------------------------------------------------------
# Safe initialization errors
# ---------------------------------------------------------------------------


def test_postgres_scaffold_fails_closed(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    sentinel_password = "LEAKSENTINEL123"
    monkeypatch.setitem(sys.modules, "psycopg2", _FailingPsycopg2("synthetic postgres unavailable"))
    with pytest.raises(Exception) as exc_info:
        RelationalMemoryRecordBackend(f"postgresql://user:***@localhost:5432/db")
    error_msg = str(exc_info.value)
    assert sentinel_password not in error_msg


def test_postgres_url_error_message_is_informative(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    monkeypatch.setitem(sys.modules, "psycopg2", _FailingPsycopg2("synthetic postgres unavailable"))
    with pytest.raises(Exception) as exc_info:
        RelationalMemoryRecordBackend("postgresql://host/db")
    error_msg = str(exc_info.value).lower()
    assert "postgresql" in error_msg or "postgres" in error_msg or "not" in error_msg


def test_unsupported_database_url_scheme_fails_closed():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    with pytest.raises(ValueError):
        RelationalMemoryRecordBackend("mysql://localhost/deepmemory")


def test_malformed_database_url_scheme_fails_closed():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    with pytest.raises(ValueError):
        RelationalMemoryRecordBackend("mysql:/records.db")


def test_empty_sqlite_url_fails_closed():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    with pytest.raises(ValueError):
        RelationalMemoryRecordBackend("sqlite://")


def test_in_memory_sqlite_path_fails_closed():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    with pytest.raises(ValueError):
        RelationalMemoryRecordBackend(":memory:")


# ---------------------------------------------------------------------------
# Live Postgres selection with fake psycopg2 (M5B live adapter slice)
# ---------------------------------------------------------------------------


class _FakePostgresCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchone = None
        self._fetchall = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        lowered = " ".join(sql.lower().split())
        if "record_id = %s" in lowered and "select *" in lowered:
            self._fetchone = self.connection.rows_by_id.get(params[-1])
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        elif "record_id = any" in lowered:
            requested = list(params[-1])
            self._fetchall = [self.connection.rows_by_id[rid] for rid in requested if rid in self.connection.rows_by_id]
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        elif "from ranked" in lowered:
            self._fetchall = list(self.connection.search_rows)
            self.description = [(col,) for col in (*_PG_ROW_COLUMNS, "score")]
        elif "on conflict" in lowered and "returning" in lowered:
            self._fetchone = self.connection.upsert_row
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        else:
            self._fetchone = None
            self._fetchall = []
            self.description = None

    def fetchone(self):
        return self._fetchone

    def fetchall(self):
        return self._fetchall


class _FakePostgresConnection:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.rows_by_id = {}
        self.search_rows = []
        self.upsert_row = None  # type: ignore[var-annotated]

    def cursor(self):
        return _FakePostgresCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FakePsycopg2:
    def __init__(self, connection):
        self.connection = connection
        self.connected_urls = []

    def connect(self, db_url):
        self.connected_urls.append(db_url)
        return self.connection


def _install_fake_psycopg2(monkeypatch, connection):
    fake = _FakePsycopg2(connection)
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    return fake


class _FailingPsycopg2:
    def __init__(self, message):
        self.message = message

    def connect(self, _db_url):
        raise RuntimeError(self.message)


class _ReturningPsycopg2:
    def __init__(self, connection):
        self.connection = connection

    def connect(self, _db_url):
        return self.connection


class _RollbackLeakingConnection:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, _sql, _params=None):
        raise RuntimeError("schema failure LEAKSENTINEL_EXECUTE")

    def rollback(self):
        raise RuntimeError("rollback failure LEAKSENTINEL_ROLLBACK")


def test_postgres_connection_failure_strips_secret_exception_cause(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    sentinel_password = "LEAKSENTINEL_CAUSE_PASSWORD"
    monkeypatch.setitem(sys.modules, "psycopg2", _FailingPsycopg2(f"could not auth with {sentinel_password}"))

    with pytest.raises(RuntimeError) as exc_info:
        RelationalMemoryRecordBackend(f"postgresql://alice:***@db.example/deepmem")

    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None or getattr(exc_info.value, "__suppress_context__", False)
    assert sentinel_password not in str(exc_info.value)
    assert sentinel_password not in "".join(traceback.format_exception(exc_info.value))


def test_postgres_schema_failure_suppresses_rollback_secret_context(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    monkeypatch.setitem(sys.modules, "psycopg2", _ReturningPsycopg2(_RollbackLeakingConnection()))

    with pytest.raises(RuntimeError) as exc_info:
        RelationalMemoryRecordBackend("postgresql://alice:***@db.example/deepmem")

    formatted = "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert "LEAKSENTINEL_EXECUTE" not in formatted
    assert "LEAKSENTINEL_ROLLBACK" not in formatted


def test_postgres_read_paths_commit_successful_select_transactions(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.rows_by_id["mem_pg_001"] = _pg_row()
    connection.search_rows = [_pg_row(record_id="mem_pg_001", text="postgres search hit", score=0.7)]
    backend = RelationalMemoryRecordBackend("postgresql://user:***@db.example/deepmem")
    commits_after_schema = connection.commits

    backend.get_record(_CTX_A, "mem_pg_001")
    backend.get_many(_CTX_A, ["mem_pg_001"])
    backend.search(_CTX_A, "postgres", limit=1)

    assert connection.commits >= commits_after_schema + 3
    assert connection.rollbacks == 0


def test_postgres_upsert_returns_database_stored_timestamps_on_conflict(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_pg_existing", text="updated", created_at=100.0, updated_at=200.0)
    backend = RelationalMemoryRecordBackend("postgresql://user:***@db.example/deepmem")

    record = backend.upsert_record(_CTX_A, text="updated", record_id="mem_pg_existing")

    assert record.id == "mem_pg_existing"
    assert record.created_at == 100.0
    assert record.updated_at == 200.0


_PG_ROW_COLUMNS = (
    "record_id",
    "text",
    "text_hash",
    "metadata",
    "provenance",
    "source",
    "source_uri",
    "ts",
    "created_at",
    "updated_at",
    "record_kind",
    "parent_id",
)


def _pg_row(record_id="mem_pg_001", text="postgres record", score=1.25, created_at=None, updated_at=None):
    now = 1234.5
    return {
        "record_id": record_id,
        "text": text,
        "text_hash": "hash-" + record_id,
        "metadata": {"kind": "pg"},
        "provenance": {"run": "r1"},
        "source": "pg-test",
        "source_uri": "",
        "ts": now,
        "created_at": now if created_at is None else created_at,
        "updated_at": now if updated_at is None else updated_at,
        "record_kind": "verbatim",
        "parent_id": None,
        "score": score,
    }


def _pg_tuple_row(record_id="mem_pg_tuple", text="tuple row", score=0.9):
    row = _pg_row(record_id=record_id, text=text, score=score)
    return tuple(row[col] for col in _PG_ROW_COLUMNS)


def test_postgres_backend_initializes_schema_with_fake_psycopg(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    fake = _install_fake_psycopg2(monkeypatch, connection)

    backend = RelationalMemoryRecordBackend("postgresql://user:pass@db.example/deepmem")

    assert backend is not None
    assert fake.connected_urls == ["postgresql://user:pass@db.example/deepmem"]
    assert any("CREATE TABLE IF NOT EXISTS memory_records" in sql for sql, _ in connection.executed)
    assert connection.commits >= 1


def test_postgres_backend_upsert_get_many_and_search_use_bound_scope_params(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.rows_by_id["mem_pg_001"] = _pg_row()
    connection.search_rows = [_pg_row(record_id="mem_pg_001", text="postgres search hit", score=0.7)]
    backend = RelationalMemoryRecordBackend("postgres://user:pass@db.example/deepmem")

    backend.upsert_record(
        _CTX_A,
        text="postgres record",
        record_id="mem_pg_001",
        metadata={"kind": "pg"},
        provenance={"run": "r1"},
        source="pg-test",
    )
    fetched = backend.get_record(_CTX_A, "mem_pg_001")
    many = backend.get_many(_CTX_A, ["missing", "mem_pg_001"])
    results = backend.search(_CTX_A, "postgres memory", filters={"kind": "pg"}, limit=3)

    assert fetched is not None
    assert fetched.id == "mem_pg_001"
    assert [record.id for record in many] == ["mem_pg_001"]
    assert [result.id for result in results] == ["mem_pg_001"]
    upsert_sql, upsert_params = next((sql, params) for sql, params in connection.executed if "ON CONFLICT" in sql)
    assert "ON CONFLICT" in upsert_sql
    assert upsert_params[:7] == _scope_values_for_test(_CTX_A)
    assert len(upsert_params) == 20
    search_sql, search_params = next((sql, params) for sql, params in connection.executed if "FROM ranked" in sql)
    assert "jsonb_each(%s::jsonb)" in search_sql
    assert "metadata -> filter.k = filter.v" in search_sql
    assert "websearch_to_tsquery" in search_sql
    assert search_params[:7] == _scope_values_for_test(_CTX_A)
    assert search_params[7] == '{"kind": "pg"}'
    assert search_params[8] == '{"kind": "pg"}'
    assert search_params[10] == "postgres OR memory"
    assert search_params[12] == "postgres OR memory"
    from agentops_runtime.memory_record_store import _pg_overfetch_limit
    assert search_params[-1] == _pg_overfetch_limit(3)


def test_postgres_backend_maps_default_tuple_cursor_rows(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.rows_by_id["mem_pg_tuple"] = _pg_tuple_row()
    connection.search_rows = [_pg_tuple_row(text="tuple search hit") + (0.9,)]
    backend = RelationalMemoryRecordBackend("postgresql://user:***@db.example/deepmem")

    fetched = backend.get_record(_CTX_A, "mem_pg_tuple")
    results = backend.search(_CTX_A, "tuple", limit=1)

    assert fetched is not None
    assert fetched.id == "mem_pg_tuple"
    assert fetched.metadata == {"kind": "pg"}
    assert [result.id for result in results] == ["mem_pg_tuple"]


def test_postgres_missing_dependency_error_redacts_query_password(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    class _ImportBlocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "psycopg2":
                raise ImportError("blocked fake missing dependency")
            return None

    monkeypatch.delitem(sys.modules, "psycopg2", raising=False)
    blocker = _ImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        with pytest.raises(RuntimeError) as exc_info:
            RelationalMemoryRecordBackend(
                "postgresql://db.example/deepmem?user=alice&password=LEAKSENTINEL_QUERY_PASSWORD"
            )
    finally:
        sys.meta_path.remove(blocker)

    error_msg = str(exc_info.value)
    assert "LEAKSENTINEL_QUERY_PASSWORD" not in error_msg
    assert "password=" not in error_msg


def _scope_values_for_test(context):
    return tuple(
        str(getattr(context, field) or "")
        for field in (
            "mode",
            "org_id",
            "workspace_id",
            "project_id",
            "agent_profile_id",
            "conversation_id",
            "user_id",
        )
    )


# ---------------------------------------------------------------------------
# BM25 keyword ranking (M5B fifth slice)
# ---------------------------------------------------------------------------


def test_bm25_length_normalization_short_outranks_long(backend):
    """Short doc with one query hit must rank above a longer doc with one hit.

    BM25 length normalisation (b=0.75) penalises the longer document.
    The long doc is inserted first so insertion order cannot explain a win.
    Under token-presence scoring both get equal score 1/1 = 1.0, so this
    verifies that BM25 replaces the old scorer.
    """
    long_text = "rocket " + ("padding " * 60)
    short_text = "rocket launch"
    backend.upsert_record(_CTX_A, text=long_text, record_id="mem_bm25_long")
    backend.upsert_record(_CTX_A, text=short_text, record_id="mem_bm25_short")

    results = backend.search(_CTX_A, "rocket")
    assert len(results) >= 2
    ids = [r.id for r in results]
    assert ids.index("mem_bm25_short") < ids.index("mem_bm25_long"), (
        f"short doc should rank above long doc but got order {ids}"
    )


def test_bm25_term_frequency_high_tf_outranks_low_tf(backend):
    """A doc with the query term repeated must rank above a single-occurrence doc.

    Both docs have the same token count to isolate the TF effect.
    Under token-presence scoring both match the query once, giving equal score 1/1.
    The low-TF doc is given a record_id that sorts BEFORE high-TF alphabetically
    so that neither insertion order nor B-tree order can explain a high-TF win.
    """
    low_tf_text = "python java ruby go"
    high_tf_text = "python python python go"
    # aaa < zzz: SQLite B-tree returns low_tf first for tied token-presence scores
    backend.upsert_record(_CTX_A, text=low_tf_text, record_id="mem_bm25_tf_aaa")
    backend.upsert_record(_CTX_A, text=high_tf_text, record_id="mem_bm25_tf_zzz")

    results = backend.search(_CTX_A, "python")
    assert len(results) >= 2
    ids = [r.id for r in results]
    assert ids.index("mem_bm25_tf_zzz") < ids.index("mem_bm25_tf_aaa"), (
        f"high-TF doc should rank above low-TF doc but got {ids}"
    )


def test_bm25_matched_via_and_score_fields(backend):
    """Top search result must report matched_via='bm25' and bm25_score > 0."""
    backend.upsert_record(_CTX_A, text="neural network training", record_id="mem_bm25_fields")
    results = backend.search(_CTX_A, "neural network")
    assert len(results) >= 1
    r = results[0]
    assert r.matched_via == "bm25", f"expected 'bm25', got {r.matched_via!r}"
    assert r.bm25_score > 0, f"expected bm25_score > 0, got {r.bm25_score}"


def test_bm25_scope_isolation_high_score_other_context_excluded(backend):
    """A very high-scoring record from a different RuntimeContext must not appear
    in results for another scope, and results must carry matched_via='bm25'.
    """
    backend.upsert_record(
        _CTX_B,
        text="satellite satellite satellite satellite orbit tracking",
        record_id="mem_bm25_scope_b",
    )
    backend.upsert_record(_CTX_A, text="satellite orbit", record_id="mem_bm25_scope_a")

    results = backend.search(_CTX_A, "satellite")
    ids = [r.id for r in results]
    assert "mem_bm25_scope_b" not in ids
    assert "mem_bm25_scope_a" in ids
    assert results[0].matched_via == "bm25", f"expected 'bm25', got {results[0].matched_via!r}"


def test_bm25_score_values_reflect_length_normalization(backend):
    """The bm25_score field must reflect Okapi BM25 values, not token-presence.

    A short doc containing the query term once must have a strictly higher
    bm25_score than a long doc containing it once.  Under token-presence both
    receive bm25_score = 1.0 (hits/n_tokens), so this test is RED until BM25 is
    applied.  The long doc gets a lower record_id so B-tree order cannot explain
    the correct answer via a tied secondary sort.
    """
    long_text = "cluster " + ("padding " * 80)
    short_text = "cluster nodes"
    # aaa < zzz: B-tree returns long doc first for tied token-presence scores
    backend.upsert_record(_CTX_A, text=long_text, record_id="mem_bm25_score_aaa")
    backend.upsert_record(_CTX_A, text=short_text, record_id="mem_bm25_score_zzz")

    results = backend.search(_CTX_A, "cluster")
    by_id = {r.id: r for r in results}
    assert "mem_bm25_score_aaa" in by_id
    assert "mem_bm25_score_zzz" in by_id
    assert by_id["mem_bm25_score_zzz"].bm25_score > by_id["mem_bm25_score_aaa"].bm25_score, (
        f"short doc bm25_score {by_id['mem_bm25_score_zzz'].bm25_score} should exceed "
        f"long doc bm25_score {by_id['mem_bm25_score_aaa'].bm25_score}"
    )


# ---------------------------------------------------------------------------
# Postgres SQL contract scaffold (M5B sixth slice)
# ---------------------------------------------------------------------------
# These tests verify the static SQL contract returned by the three helpers
# _postgres_schema_sql(), _postgres_upsert_sql(), _postgres_search_sql().
# No Postgres connection is required — all assertions are pure string checks.
# ---------------------------------------------------------------------------


def _schema_sql() -> str:
    from agentops_runtime.memory_record_store import _postgres_schema_sql

    return _postgres_schema_sql()


def _upsert_sql() -> str:
    from agentops_runtime.memory_record_store import _postgres_upsert_sql

    return _postgres_upsert_sql()


def _search_sql() -> str:
    from agentops_runtime.memory_record_store import _postgres_search_sql

    return _postgres_search_sql()


def _scope_cols() -> tuple[str, ...]:
    return (
        "scope_mode",
        "scope_org_id",
        "scope_workspace_id",
        "scope_project_id",
        "scope_agent_profile_id",
        "scope_conversation_id",
        "scope_user_id",
    )


def _single_spaced(sql: str) -> str:
    return " ".join(sql.lower().split())


# --- schema SQL ---


def test_postgres_schema_sql_creates_vector_extension():
    sql = _schema_sql().lower()
    assert "create extension" in sql
    assert "vector" in sql


def test_postgres_schema_sql_has_vector_column_with_named_dimension():
    from agentops_runtime.memory_record_store import EMBEDDING_DIM

    sql = _schema_sql().lower()
    assert f"embedding vector({EMBEDDING_DIM})" in sql


def test_postgres_schema_sql_has_jsonb_metadata_and_provenance():
    sql = _schema_sql().lower()
    assert "jsonb" in sql
    assert "metadata" in sql
    assert "provenance" in sql


def test_postgres_schema_sql_has_all_seven_scope_columns():
    sql = _schema_sql().lower()
    for col in _scope_cols():
        assert col in sql, f"expected scope column {col!r} in schema SQL"


def test_postgres_schema_sql_has_record_id_in_table():
    sql = _schema_sql().lower()
    assert "record_id" in sql


def test_postgres_schema_sql_has_composite_pk_or_unique_with_scope_and_record_id():
    sql = _schema_sql().lower()
    match = re.search(r"(?:primary key|unique)\s*\(([^)]*)\)", sql)
    assert match, "schema must declare a primary key or unique constraint"
    constraint_cols = [col.strip() for col in match.group(1).split(",")]
    assert constraint_cols == [*_scope_cols(), "record_id"]


def test_postgres_schema_sql_has_fts_tsvector():
    sql = _schema_sql().lower()
    assert "tsvector" in sql


def test_postgres_schema_sql_has_vector_index():
    sql = _single_spaced(_schema_sql())
    assert (
        "create index if not exists memory_records_embedding_idx "
        "on memory_records using hnsw (embedding vector_cosine_ops)"
    ) in sql


def test_postgres_schema_sql_has_gin_indexes_for_fts_and_jsonb():
    sql = _single_spaced(_schema_sql())
    assert "on memory_records using gin (ts_vec)" in sql
    assert "on memory_records using gin (metadata)" in sql
    assert "on memory_records using gin (provenance)" in sql


def test_postgres_schema_sql_has_scope_index():
    sql = _single_spaced(_schema_sql())
    assert f"on memory_records ({', '.join(_scope_cols())})" in sql


def test_postgres_schema_sql_has_text_hash_source_source_uri_kind_parent_fields():
    sql = _schema_sql().lower()
    for field in ("text_hash", "source_uri", "record_kind", "parent_id"):
        assert field in sql, f"expected field {field!r} in schema SQL"


# --- upsert SQL ---


def test_postgres_upsert_sql_is_on_conflict_do_update():
    sql = _upsert_sql().lower()
    assert "on conflict" in sql
    assert "do update" in sql


def test_postgres_upsert_sql_conflict_target_includes_all_scope_fields_and_record_id():
    sql = _upsert_sql().lower()
    match = re.search(r"on conflict\s*\(([^)]*)\)", sql)
    assert match, "upsert SQL must declare an ON CONFLICT target"
    conflict_target = [col.strip() for col in match.group(1).split(",")]
    assert conflict_target == [*_scope_cols(), "record_id"]


def test_postgres_upsert_sql_uses_placeholders_not_string_interpolation():
    sql = _upsert_sql()
    # 7 scope cols + 13 record payload cols, all bound as psycopg2 parameters.
    assert sql.count("%s") == 20
    assert "VALUES (" + ", ".join("%s" for _ in range(20)) + ")" in sql
    # must NOT contain Python f-string markers or format placeholders that imply interpolation
    assert "{" not in sql


def test_postgres_upsert_sql_preserves_created_at_on_conflict():
    sql = _upsert_sql().lower()
    # created_at must not appear in the DO UPDATE SET clause after "do update set".
    # The RETURNING clause may include created_at so callers can report the stored
    # value without lying on idempotent updates.
    do_update_idx = sql.find("do update set")
    assert do_update_idx != -1, "upsert SQL must contain DO UPDATE SET"
    update_clause = sql[do_update_idx:].split("returning", 1)[0]
    # created_at must not be assigned in the update portion
    # (it is in INSERT columns but must be excluded from SET)
    assert "created_at" not in update_clause, (
        "created_at must not appear in DO UPDATE SET — preserve original value"
    )
    assert "returning" in sql
    assert "created_at" in sql.split("returning", 1)[1]


def test_postgres_upsert_sql_no_sentinel_value_in_sql_string():
    sentinel = "LEAKSENTINEL123"
    sql = _upsert_sql()
    assert sentinel not in sql
    assert "literal_secret" not in sql.lower()


# --- search SQL ---


def test_postgres_search_sql_has_scope_filter_before_ranking():
    sql = _single_spaced(_search_sql())
    scope_cte_idx = sql.find("with scope_filtered as")
    ranked_cte_idx = sql.find("ranked as")
    assert scope_cte_idx != -1
    assert ranked_cte_idx != -1
    assert scope_cte_idx < ranked_cte_idx
    scope_cte = sql[scope_cte_idx:ranked_cte_idx]
    for col in _scope_cols():
        assert f"{col} = %s" in scope_cte, f"expected {col!r} predicate before ranking"
    assert "from scope_filtered" in sql[ranked_cte_idx:]


def test_postgres_search_sql_has_metadata_jsonb_top_level_exact_filter_param():
    sql = _single_spaced(_search_sql())
    assert "%s::jsonb is null" in sql
    assert "jsonb_each(%s::jsonb)" in sql
    assert "metadata ? filter.k" in sql
    assert "metadata -> filter.k = filter.v" in sql


def test_postgres_search_sql_has_vector_rank_component():
    sql = _single_spaced(_search_sql())
    assert "1.0 - (embedding <=> %s::vector)" in sql


def test_postgres_search_sql_has_text_rank_component():
    sql = _single_spaced(_search_sql())
    assert "ts_rank(ts_vec, websearch_to_tsquery('english', %s))" in sql


def test_postgres_search_sql_uses_fts_predicate_or_positive_score_filter():
    sql = _single_spaced(_search_sql())
    ranked_idx = sql.find("ranked as")
    select_idx = sql.find("select * from ranked")
    assert ranked_idx != -1
    assert select_idx != -1
    ranked_cte = sql[ranked_idx:select_idx]
    assert "ts_vec @@ websearch_to_tsquery('english', %s)" in ranked_cte
    assert "where score > 0" in sql[select_idx:]


def test_postgres_search_sql_has_order_by_score_desc_record_id_asc():
    sql = _single_spaced(_search_sql())
    assert "order by score desc, record_id asc" in sql


def test_postgres_search_sql_has_exact_placeholder_count_and_limit_placeholder():
    sql = _search_sql()
    # 7 scope + 2 metadata + vector rank + text rank + vector candidate + FTS candidate + limit.
    assert sql.count("%s") == 14
    assert re.search(r"LIMIT\s+%s\s*;", sql)


# ---------------------------------------------------------------------------
# Vector embedding population (M5B vector-binding slice)
# ---------------------------------------------------------------------------


def _make_fake_embed_fn(dim: int = 384):
    """Deterministic fake embed_fn returning dim-dimensional unit vectors."""
    def embed_fn(texts):
        return [[float(i % 10) / 10.0 for i in range(dim)] for _ in texts]
    return embed_fn


def test_pgvector_literal_formats_384_dim_vector():
    from agentops_runtime.memory_record_store import _pgvector_literal

    vec = [float(i) / 384.0 for i in range(384)]
    result = _pgvector_literal(vec)
    assert result.startswith("[")
    assert result.endswith("]")
    assert result.count(",") == 383


def test_pgvector_literal_rejects_wrong_dimension():
    from agentops_runtime.memory_record_store import _pgvector_literal

    with pytest.raises(ValueError):
        _pgvector_literal([0.1, 0.2, 0.3])


def test_pgvector_literal_rejects_empty():
    from agentops_runtime.memory_record_store import _pgvector_literal

    with pytest.raises(ValueError):
        _pgvector_literal([])


def test_pgvector_literal_coerces_to_float():
    from agentops_runtime.memory_record_store import _pgvector_literal

    vec = [str(i) for i in range(384)]
    result = _pgvector_literal(vec)
    assert result.startswith("[")
    assert result.endswith("]")
    assert "." in result


def test_postgres_upsert_with_embed_fn_binds_non_null_vector(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_vec_upsert")
    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.upsert_record(_CTX_A, text="test vector binding", record_id="mem_vec_upsert")
    upsert_params = next(params for sql, params in connection.executed if "ON CONFLICT" in sql)
    embedding_param = upsert_params[-1]
    assert embedding_param is not None
    assert isinstance(embedding_param, str)
    assert embedding_param.startswith("[")
    assert embedding_param.endswith("]")
    assert embedding_param.count(",") == 383


def test_postgres_search_with_embed_fn_binds_vector_to_both_vector_slots(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.search_rows = [_pg_row(record_id="mem_vec_search", score=0.9)]
    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.search(_CTX_A, "neural network", limit=1)
    search_params = next(params for sql, params in connection.executed if "FROM ranked" in sql)
    # position 9 = vector rank param, position 11 = vector IS NOT NULL candidate filter
    vector_rank_param = search_params[9]
    vector_cand_param = search_params[11]
    assert vector_rank_param is not None
    assert isinstance(vector_rank_param, str)
    assert vector_rank_param.startswith("[")
    assert vector_rank_param == vector_cand_param


def test_postgres_injected_embed_fn_takes_precedence_over_default(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_inject")

    calls = []

    def tracking_embed_fn(texts):
        calls.extend(texts)
        return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr(mod, "_load_default_embed_fn", lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError("default embedder must not be called when embed_fn is injected")
    ), raising=False)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=tracking_embed_fn,
    )
    backend.upsert_record(_CTX_A, text="injection test", record_id="mem_inject")
    assert "injection test" in calls


def test_postgres_default_embedder_failure_is_sanitized(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row()

    sentinel_msg = "LEAKSENTINEL_EMBED_FAIL chromadb unavailable password=secret_dsn_val"
    monkeypatch.setattr(
        mod,
        "_load_default_embed_fn",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError(sentinel_msg)),
        raising=False,
    )

    backend = RelationalMemoryRecordBackend("postgresql://user:password@db.example/deepmem")
    with pytest.raises(RuntimeError) as exc_info:
        backend.upsert_record(_CTX_A, text="test sanitized", record_id="mem_sanitized")
    assert "LEAKSENTINEL_EMBED_FAIL" not in str(exc_info.value)
    assert "secret_dsn_val" not in str(exc_info.value)
    assert "LEAKSENTINEL_EMBED_FAIL" not in "".join(traceback.format_exception(exc_info.value))


def test_postgres_embedder_valueerror_does_not_leak_sentinel(monkeypatch):
    """Embedder-raised ValueError must be sanitized — not re-raised with raw message.

    When _load_default_embed_fn succeeds but the returned embed_fn raises a
    ValueError containing a sentinel/password string, that raw message must not
    appear in the raised exception or its formatted traceback.
    """
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)

    def leaking_embed_fn(texts):
        raise ValueError("LEAKSENTINEL_DEFAULT_EMBED password=secret")

    monkeypatch.setattr(
        mod,
        "_load_default_embed_fn",
        lambda *_a, **_kw: leaking_embed_fn,
        raising=False,
    )

    backend = RelationalMemoryRecordBackend("postgresql://user:***@db.example/deepmem")
    with pytest.raises(Exception) as exc_info:
        backend.upsert_record(_CTX_A, text="leak test", record_id="mem_leak_test")

    assert not isinstance(exc_info.value, ValueError), (
        "raw ValueError from embedder must not escape — must be wrapped as RuntimeError"
    )
    error_msg = str(exc_info.value)
    assert "LEAKSENTINEL_DEFAULT_EMBED" not in error_msg
    assert "password=secret" not in error_msg
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "LEAKSENTINEL_DEFAULT_EMBED" not in formatted
    assert "password=secret" not in formatted


def test_pgvector_literal_non_float_value_does_not_leak_sentinel(monkeypatch):
    """float() coercion failure in _pgvector_literal must not leak raw vector values."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)

    sentinel = "LEAKSENTINEL_VECTOR password=secret"

    def leaking_vector_embed_fn(texts):
        return [[sentinel] + [0.1] * 383]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:***@db.example/deepmem",
        embed_fn=leaking_vector_embed_fn,
    )

    with pytest.raises(Exception) as exc_info:
        backend.upsert_record(_CTX_A, text="sentinel leak test", record_id="mem_vector_leak")

    error_msg = str(exc_info.value)
    assert "LEAKSENTINEL_VECTOR" not in error_msg
    assert "password=secret" not in error_msg
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert "LEAKSENTINEL_VECTOR" not in formatted
    assert "password=secret" not in formatted


def test_postgres_invalid_embedding_dimension_fails_before_db_write(monkeypatch):
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)

    wrong_dim_embed_fn = _make_fake_embed_fn(dim=3)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=wrong_dim_embed_fn,
    )
    upserts_before = sum(1 for sql, _ in connection.executed if "ON CONFLICT" in sql)
    with pytest.raises((ValueError, RuntimeError)):
        backend.upsert_record(_CTX_A, text="dim fail test", record_id="mem_dim_fail")
    upserts_after = sum(1 for sql, _ in connection.executed if "ON CONFLICT" in sql)
    assert upserts_after == upserts_before, "DB write must not occur for invalid vector dims"


def test_pgvector_literal_custom_float_runtime_error_does_not_leak_sentinel():
    """__float__ raising RuntimeError must be sanitized — sentinel must not appear."""
    from agentops_runtime.memory_record_store import _pgvector_literal

    sentinel = "LEAKSENTINEL_FLOAT password=secret"

    class _BadFloat:
        def __float__(self):
            raise RuntimeError(sentinel)

    vec = [_BadFloat()] + [0.1] * 383

    with pytest.raises(Exception) as exc_info:
        _pgvector_literal(vec)

    error_msg = str(exc_info.value)
    assert sentinel not in error_msg
    assert "password=secret" not in error_msg
    formatted = "".join(traceback.format_exception(exc_info.value))
    assert sentinel not in formatted
    assert "password=secret" not in formatted


def test_compute_pg_embedding_empty_embedder_return_fails_safely(monkeypatch):
    """embed_fn returning [] must raise a sanitized RuntimeError with no raw internal text."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)

    def empty_embed_fn(texts):
        return []

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:***@db.example/deepmem",
        embed_fn=empty_embed_fn,
    )

    with pytest.raises(RuntimeError) as exc_info:
        backend.upsert_record(_CTX_A, text="empty embed test", record_id="mem_empty_embed")

    assert exc_info.value.__cause__ is None
    assert "list index out of range" not in str(exc_info.value)
    assert "IndexError" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# M5B — extracted-signal ingest (thirteenth slice)
# ---------------------------------------------------------------------------


def test_upsert_ingests_extracted_signals(backend):
    """upsert_record must populate memory_signals for the stored record."""
    record = backend.upsert_record(
        _CTX_A,
        text="fixed the authentication bug in login flow",
        record_id="mem_sig_001",
        source_uri="https://example.com/doc",
    )
    signals = backend._signals_for_record(_CTX_A, record.id)
    assert isinstance(signals, list)
    assert len(signals) >= 1


def test_signals_are_scope_isolated(backend):
    """Signals ingested under CTX_A must not be visible under CTX_B."""
    backend.upsert_record(
        _CTX_A,
        text="fixed the authentication bug in login flow",
        record_id="mem_sig_scope",
        source_uri="https://example.com/doc",
    )
    signals_a = backend._signals_for_record(_CTX_A, "mem_sig_scope")
    signals_b = backend._signals_for_record(_CTX_B, "mem_sig_scope")
    assert len(signals_a) >= 1
    assert signals_b == []


def test_reupsert_replaces_signals_not_appends(backend):
    """Re-upsert must replace signals, not accumulate them."""
    backend.upsert_record(
        _CTX_A,
        text="fixed the authentication bug in login flow",
        record_id="mem_sig_replace",
        source_uri="https://example.com/v1",
    )
    count_after_first = len(backend._signals_for_record(_CTX_A, "mem_sig_replace"))

    backend.upsert_record(
        _CTX_A,
        text="fixed the authentication bug in login flow",
        record_id="mem_sig_replace",
        source_uri="https://example.com/v2",
    )
    count_after_second = len(backend._signals_for_record(_CTX_A, "mem_sig_replace"))

    assert count_after_second == count_after_first, (
        f"Re-upsert accumulated signals: first={count_after_first}, second={count_after_second}"
    )


def test_signal_ingest_does_not_change_record_get_or_search(backend):
    """Signal ingest side effect must not break get_record or search."""
    record = backend.upsert_record(
        _CTX_A,
        text="fixed the authentication bug in login flow",
        record_id="mem_sig_nobreak",
        source_uri="https://example.com/doc",
    )
    fetched = backend.get_record(_CTX_A, record.id)
    assert fetched is not None
    assert fetched.id == record.id
    assert fetched.text == record.text

    results = backend.search(_CTX_A, "authentication bug")
    assert any(r.id == record.id for r in results)


def test_signal_build_failure_does_not_break_record_upsert(backend, monkeypatch):
    """If build_extracted_signal_lines raises, upsert_record must still succeed."""
    import agentops_runtime.memory_record_store as mod

    def _failing_build(*_args, **_kwargs):
        raise RuntimeError("synthetic signal build failure")

    monkeypatch.setattr(mod, "build_extracted_signal_lines", _failing_build, raising=False)

    record = backend.upsert_record(
        _CTX_A,
        text="should still store even if signals fail",
        record_id="mem_sig_fail",
    )
    assert record.id == "mem_sig_fail"
    fetched = backend.get_record(_CTX_A, "mem_sig_fail")
    assert fetched is not None
    assert fetched.text == "should still store even if signals fail"


def test_sidecar_insert_failure_rollbacks_and_preserves_prior_signals(backend, monkeypatch):
    """Failed sidecar INSERT must rollback (not leave stale DELETE) and preserve prior signals.

    Regression: _ingest_signals() swallowed sidecar DB write failures without rolling back.
    A DELETE+failed-INSERT left the connection in an open transaction; a later primary
    commit would silently commit the stale DELETE, wiping signals.
    """
    import agentops_runtime.memory_record_store as mod

    # First upsert: creates primary record and signals
    backend.upsert_record(
        _CTX_A,
        text="original signal content",
        record_id="mem_sig_rollback",
        source_uri="https://example.com/v1",
    )
    signals_before = backend._signals_for_record(_CTX_A, "mem_sig_rollback")
    assert len(signals_before) >= 1, "first upsert must create at least one signal"

    # Patch build_extracted_signal_lines to return [None] — None violates signal_line TEXT NOT NULL
    monkeypatch.setattr(mod, "build_extracted_signal_lines", lambda *_a, **_kw: [None], raising=False)

    # Second upsert: primary record update succeeds; sidecar INSERT fails after DELETE
    backend.upsert_record(
        _CTX_A,
        text="updated signal content",
        record_id="mem_sig_rollback",
        source_uri="https://example.com/v2",
    )

    # Primary record must be updated successfully
    fetched = backend.get_record(_CTX_A, "mem_sig_rollback")
    assert fetched is not None
    assert fetched.text == "updated signal content"

    # Connection must not be left in an open transaction after failed sidecar ingest
    assert not backend._conn().in_transaction, (
        "SQLite connection must not be in_transaction after failed sidecar ingest"
    )

    # Prior signals must be preserved — the stale DELETE must have been rolled back
    signals_after = backend._signals_for_record(_CTX_A, "mem_sig_rollback")
    assert len(signals_after) >= 1, (
        f"prior signals must remain after failed sidecar replacement, got {signals_after}"
    )
    assert signals_after == signals_before, (
        f"signals must be unchanged: before={signals_before}, after={signals_after}"
    )


# ---------------------------------------------------------------------------
# M5B — SQLite signal boost application in search() (fourteenth slice)
# ---------------------------------------------------------------------------


def test_signal_boost_marks_matched_via_record_plus_signal(backend):
    """A record whose extracted signal lines BM25-match the query must surface
    with matched_via=='record+signal' and score > bm25_score."""
    # "Kafka" repeated twice → entity word → signal line contains "kafka" token
    backend.upsert_record(
        _CTX_A,
        text="Kafka Kafka pipeline notes",
        record_id="mem_boost_sig_001",
    )
    results = backend.search(_CTX_A, "kafka")
    assert len(results) >= 1
    r = results[0]
    assert r.id == "mem_boost_sig_001"
    assert r.matched_via == "record+signal", (
        f"expected 'record+signal', got {r.matched_via!r}"
    )
    assert r.bm25_score > 0, f"expected bm25_score > 0, got {r.bm25_score}"
    assert r.score > r.bm25_score, (
        f"score {r.score} must exceed bm25_score {r.bm25_score} when signal boost applies"
    )


def test_signal_boost_reorders_above_equal_bm25_peer(backend):
    """Boosted record must rank above a peer with equal raw BM25 score.

    Both records have 'kafka' twice at equal doc length → equal raw BM25.
    The capitalized record has a lexicographically later ID so the pre-boost
    tie-break (sort by record_id asc for equal scores) would place it second.
    After the signal boost its effective score exceeds the peer's and it ranks
    first.
    """
    # All-lowercase → no capitalized entity words → no signal boost
    backend.upsert_record(
        _CTX_A,
        text="kafka pipeline notes kafka",
        record_id="mem_kafka_aaa",
    )
    # Capitalized Kafka × 2 → entity extracted → signal line has "kafka" token → boost
    backend.upsert_record(
        _CTX_A,
        text="Kafka pipeline notes Kafka",
        record_id="mem_kafka_zzz",
    )
    results = backend.search(_CTX_A, "kafka")
    ids = [r.id for r in results]
    assert len(results) >= 2, f"expected at least 2 results, got {ids}"
    assert ids.index("mem_kafka_zzz") < ids.index("mem_kafka_aaa"), (
        f"boosted 'mem_kafka_zzz' should rank before 'mem_kafka_aaa', got order {ids}"
    )


def test_non_signal_match_keeps_matched_via_bm25(backend):
    """A record whose signal lines do not BM25-match the query keeps matched_via=='bm25'."""
    # All-lowercase → no capitalized entity words → signal line has no 'kafka' token
    backend.upsert_record(
        _CTX_A,
        text="kafka pipeline notes",
        record_id="mem_no_boost_001",
    )
    results = backend.search(_CTX_A, "kafka")
    assert len(results) >= 1
    r = results[0]
    assert r.id == "mem_no_boost_001"
    assert r.matched_via == "bm25", (
        f"expected 'bm25' for non-signal match, got {r.matched_via!r}"
    )


def test_signal_boost_does_not_create_signal_only_candidate(backend):
    """Signal matches may rerank only records with positive raw BM25."""
    # No 'kafka' in text, but source_uri stem becomes a signal line containing 'kafka'.
    backend.upsert_record(
        _CTX_A,
        text="postgres pipeline notes",
        record_id="mem_signal_only_not_candidate",
        source_uri="file:///tmp/kafka.md",
    )
    results = backend.search(_CTX_A, "kafka")
    assert [r.id for r in results] == []


def test_signal_only_candidates_do_not_consume_boost_ladder_slots(backend):
    """Signal-only rows must not reduce the boost assigned to eligible BM25 hits."""
    # This record has a matching signal via source_uri but raw record BM25 is zero.
    # Its lexically earlier id would consume the top boost if signal-only rows were eligible.
    backend.upsert_record(
        _CTX_A,
        text="postgres pipeline notes",
        record_id="mem_a_signal_only",
        source_uri="file:///tmp/kafka.md",
    )
    backend.upsert_record(
        _CTX_A,
        text="Kafka Kafka pipeline notes",
        record_id="mem_z_bm25_and_signal",
    )

    results = backend.search(_CTX_A, "kafka")
    hit = next(r for r in results if r.id == "mem_z_bm25_and_signal")
    assert hit.matched_via == "record+signal"
    assert hit.score - hit.bm25_score == pytest.approx(0.40)


def test_signal_boost_respects_metadata_filter(backend):
    """Signal-bearing records excluded by metadata filter must not appear in results."""
    backend.upsert_record(
        _CTX_A,
        text="Kafka Kafka pipeline notes",
        record_id="mem_sig_filtered_out",
        metadata={"kind": "filtered"},
    )
    backend.upsert_record(
        _CTX_A,
        text="kafka search match notes",
        record_id="mem_sig_kept",
        metadata={"kind": "kept"},
    )
    results = backend.search(_CTX_A, "kafka", filters={"kind": "kept"})
    ids = [r.id for r in results]
    assert "mem_sig_filtered_out" not in ids, (
        "filtered signal record must be absent regardless of signal strength"
    )
    assert "mem_sig_kept" in ids, "matching unfiltered record must be present"


def test_signal_boost_is_scope_isolated(backend):
    """Signal rows from another RuntimeContext must not boost results in this scope.

    CTX_B holds 'mem_shared_scope' with capitalized Kafka × 2 (strong signal).
    CTX_A holds the same record_id but with all-lowercase text (no signal boost).
    Searching in CTX_A must not inherit CTX_B's signal rows.
    """
    backend.upsert_record(
        _CTX_B,
        text="Kafka Kafka pipeline notes",
        record_id="mem_shared_scope",
    )
    backend.upsert_record(
        _CTX_A,
        text="kafka pipeline notes",
        record_id="mem_shared_scope",
    )
    results = backend.search(_CTX_A, "kafka")
    assert len(results) >= 1
    r = results[0]
    assert r.id == "mem_shared_scope"
    assert r.matched_via == "bm25", (
        f"cross-scope signals must not boost CTX_A results, got matched_via={r.matched_via!r}"
    )


# ---------------------------------------------------------------------------
# M5B — Postgres memory_signals schema + best-effort ingest (fifteenth slice)
# ---------------------------------------------------------------------------


def test_postgres_schema_sql_has_memory_signals_table():
    sql = _schema_sql().lower()
    assert "create table if not exists memory_signals" in sql
    for col in _scope_cols():
        assert col in sql
    assert "signal_line text not null" in sql


def test_postgres_schema_sql_has_memory_signals_scope_record_index():
    sql = _single_spaced(_schema_sql())
    scope = ", ".join(_scope_cols())
    assert f"on memory_signals ({scope}, record_id)" in sql


class _FailingSignalInsertCursor:
    def __init__(self, connection):
        self.connection = connection
        self._fetchone_val = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        lowered = sql.lower().strip()
        if "insert into memory_signals" in lowered:
            raise RuntimeError("synthetic signal insert failure")
        if "on conflict" in lowered and "returning" in lowered:
            self._fetchone_val = self.connection.upsert_row
            self.description = [(col,) for col in _PG_ROW_COLUMNS]

    def fetchone(self):
        return self._fetchone_val

    def fetchall(self):
        return []


class _FailingSignalInsertConnection:
    def __init__(self, upsert_row):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.upsert_row = upsert_row

    def cursor(self):
        return _FailingSignalInsertCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_postgres_upsert_ingests_signals_after_primary_write(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_pg_sig_001")
    monkeypatch.setattr(mod, "build_extracted_signal_lines",
                        lambda *_a, **_kw: ["ingest signal"], raising=False)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.upsert_record(_CTX_A, text="signal ingest test", record_id="mem_pg_sig_001",
                          source_uri="https://example.com/doc")

    executed_sqls = [sql for sql, _ in connection.executed]
    upsert_idx = next((i for i, sql in enumerate(executed_sqls) if "ON CONFLICT" in sql), None)
    delete_idx = next(
        (i for i, sql in enumerate(executed_sqls)
         if "memory_signals" in sql.lower() and "delete" in sql.lower()),
        None,
    )
    assert upsert_idx is not None, "Expected primary upsert SQL"
    assert delete_idx is not None, "Expected DELETE from memory_signals"
    assert upsert_idx < delete_idx, "Primary upsert must precede signal ingest"
    signal_sqls = [sql for sql in executed_sqls if "memory_signals" in sql.lower()]
    assert any("delete" in sql.lower() for sql in signal_sqls)
    assert any("insert" in sql.lower() for sql in signal_sqls)


def test_postgres_signal_ingest_uses_bound_params_not_interpolation(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_pg_sig_params")
    monkeypatch.setattr(mod, "build_extracted_signal_lines",
                        lambda *_a, **_kw: ["bound signal"], raising=False)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.upsert_record(_CTX_A, text="bound params test",
                          record_id="mem_pg_sig_params",
                          source_uri="https://example.com/doc")

    delete_calls = [(sql, params) for sql, params in connection.executed
                    if "memory_signals" in sql.lower() and sql.lower().strip().startswith("delete")]
    insert_calls = [(sql, params) for sql, params in connection.executed
                    if "memory_signals" in sql.lower() and sql.lower().strip().startswith("insert")]

    assert delete_calls, "Expected DELETE from memory_signals"
    _, delete_params = delete_calls[0]
    assert delete_params is not None
    assert len(delete_params) == 8  # 7 scope + record_id
    assert delete_params[:7] == _scope_values_for_test(_CTX_A)
    assert delete_params[7] == "mem_pg_sig_params"

    assert insert_calls, "Expected INSERT into memory_signals"
    _, insert_params = insert_calls[0]
    assert insert_params is not None
    assert len(insert_params) == 9  # 7 scope + record_id + signal_line
    assert insert_params[:7] == _scope_values_for_test(_CTX_A)
    assert insert_params[7] == "mem_pg_sig_params"
    assert insert_params[8] == "bound signal"


def test_postgres_reupsert_replaces_signals_not_appends(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    connection = _FakePostgresConnection()
    _install_fake_psycopg2(monkeypatch, connection)
    connection.upsert_row = _pg_row(record_id="mem_pg_sig_replace")
    monkeypatch.setattr(mod, "build_extracted_signal_lines",
                        lambda *_a, **_kw: ["replace signal"], raising=False)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.upsert_record(_CTX_A, text="first content", record_id="mem_pg_sig_replace")
    backend.upsert_record(_CTX_A, text="second content", record_id="mem_pg_sig_replace")

    delete_calls = [sql for sql, _ in connection.executed
                    if "memory_signals" in sql.lower() and "delete" in sql.lower()]
    assert len(delete_calls) >= 2, (
        f"Expected at least 2 DELETE calls (one per upsert), got {len(delete_calls)}"
    )


def test_postgres_sidecar_failure_preserves_primary_record_without_raising(monkeypatch):
    import agentops_runtime.memory_record_store as mod
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    upsert_row = _pg_row(record_id="mem_pg_sidecar_fail", text="primary content")
    conn = _FailingSignalInsertConnection(upsert_row)
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    monkeypatch.setattr(mod, "build_extracted_signal_lines",
                        lambda *_a, **_kw: ["sidecar signal"], raising=False)

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    record = backend.upsert_record(_CTX_A, text="primary content",
                                   record_id="mem_pg_sidecar_fail",
                                   source_uri="https://example.com/doc")
    assert record is not None
    assert record.id == "mem_pg_sidecar_fail"
    assert conn.rollbacks >= 1, "Expected rollback after sidecar INSERT failure"


# ---------------------------------------------------------------------------
# M5B — Postgres memory_signals boost application in search() (sixteenth slice)
# ---------------------------------------------------------------------------


class _FakePostgresCursorWithSignals:
    """Fake cursor that routes memory_signals queries separately from memory_records queries.

    The existing _FakePostgresCursor matches 'record_id = any' for both get_many and
    signal queries. This variant checks for 'memory_signals' first so signal reads
    return signal rows rather than falling through to the memory_records get_many path.
    """

    def __init__(self, connection):
        self.connection = connection
        self._fetchone_val = None
        self._fetchall_val = []
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        lowered = " ".join(sql.lower().split())
        if "memory_signals" in lowered and "record_id = any" in lowered:
            requested_ids = list(params[-1]) if params else []
            self._fetchall_val = [
                r for r in self.connection.signal_rows
                if r.get("record_id") in requested_ids
            ]
            self._fetchone_val = None
            self.description = [("record_id",), ("signal_line",)]
        elif "record_id = %s" in lowered and "select *" in lowered:
            self._fetchone_val = self.connection.rows_by_id.get(params[-1])
            self._fetchall_val = []
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        elif "record_id = any" in lowered:
            requested = list(params[-1]) if params else []
            self._fetchall_val = [
                self.connection.rows_by_id[rid]
                for rid in requested
                if rid in self.connection.rows_by_id
            ]
            self._fetchone_val = None
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        elif "from ranked" in lowered:
            self._fetchall_val = list(self.connection.search_rows)
            self._fetchone_val = None
            self.description = [(col,) for col in (*_PG_ROW_COLUMNS, "score")]
        elif "on conflict" in lowered and "returning" in lowered:
            self._fetchone_val = self.connection.upsert_row
            self._fetchall_val = []
            self.description = [(col,) for col in _PG_ROW_COLUMNS]
        else:
            self._fetchone_val = None
            self._fetchall_val = []
            self.description = None

    def fetchone(self):
        return self._fetchone_val

    def fetchall(self):
        return self._fetchall_val


class _FakePostgresConnectionWithSignals:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.rows_by_id = {}
        self.search_rows = []
        self.signal_rows = []
        self.upsert_row = None

    def cursor(self):
        return _FakePostgresCursorWithSignals(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _FailingSignalSelectCursor:
    """Fake cursor that raises a RuntimeError on memory_signals SELECT queries."""

    def __init__(self, connection):
        self.connection = connection
        self._fetchall_val = []
        self._fetchone_val = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def execute(self, sql, params=None):
        self.connection.executed.append((sql, params))
        lowered = " ".join(sql.lower().split())
        if "memory_signals" in lowered and "record_id = any" in lowered:
            raise RuntimeError("synthetic signal SELECT failure")
        if "from ranked" in lowered:
            self._fetchall_val = list(self.connection.search_rows)
            self.description = [(col,) for col in (*_PG_ROW_COLUMNS, "score")]
        else:
            self._fetchone_val = None
            self._fetchall_val = []
            self.description = None

    def fetchone(self):
        return self._fetchone_val

    def fetchall(self):
        return self._fetchall_val


class _FailingSignalSelectConnection:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.search_rows = []

    def cursor(self):
        return _FailingSignalSelectCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_postgres_search_applies_signal_boost_marks_matched_via_record_plus_signal(monkeypatch):
    """Postgres search result with a matching signal row gets matched_via='record+signal',
    score > bm25_score, and Okapi BM25 value stored in bm25_score."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend
    from agent.local_memory.store import _bm25_scores

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    text = "kafka pipeline notes"
    conn.search_rows = [_pg_row(record_id="mem_pg_sig_boost", text=text, score=0.5)]
    conn.signal_rows = [
        {"record_id": "mem_pg_sig_boost", "signal_line": "kafka pipeline entity"},
        {"record_id": "mem_pg_sig_boost", "signal_line": "kafka streaming term"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r.id == "mem_pg_sig_boost"
    assert r.matched_via == "record+signal", (
        f"expected 'record+signal', got {r.matched_via!r}"
    )
    expected_okapi = _bm25_scores("kafka", [text])[0]
    assert r.bm25_score == pytest.approx(expected_okapi, abs=1e-9), (
        f"bm25_score must equal Okapi BM25 {expected_okapi}, got {r.bm25_score}"
    )
    assert r.score > r.bm25_score, (
        f"score {r.score} must exceed bm25_score {r.bm25_score} when signal boost applies"
    )


def test_postgres_search_signal_boost_reorders_within_returned_window(monkeypatch):
    """A row with lower base score but a matching signal must rank above a higher-base
    row with no matching signal after boost is applied."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    # High base score but no signal boost
    # Low base score but signal boost (0.20 + 0.40 = 0.60 > 0.55)
    conn.search_rows = [
        _pg_row(record_id="mem_pg_high_base", text="kafka processing notes", score=0.55),
        _pg_row(record_id="mem_pg_low_base", text="kafka streaming notes", score=0.2),
    ]
    conn.signal_rows = [
        {"record_id": "mem_pg_low_base", "signal_line": "kafka streaming entity"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=2)

    assert len(results) == 2
    ids = [r.id for r in results]
    assert ids.index("mem_pg_low_base") < ids.index("mem_pg_high_base"), (
        f"boosted record 'mem_pg_low_base' should rank above 'mem_pg_high_base', got {ids}"
    )


def test_postgres_search_signal_boost_uses_bound_scope_params(monkeypatch):
    """Signal query must bind all seven RuntimeContext scope predicates and
    record_id = ANY(%s) for candidate IDs; no ID must be interpolated into SQL."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [_pg_row(record_id="mem_pg_scope_check", text="kafka scope check", score=0.5)]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.search(_CTX_A, "kafka", limit=1)

    signal_calls = [
        (sql, params)
        for sql, params in conn.executed
        if "memory_signals" in sql.lower() and "record_id = any" in sql.lower()
    ]
    assert signal_calls, "Expected a signal query after Postgres search"

    sig_sql, sig_params = signal_calls[0]

    # No candidate ID literal in the SQL text
    assert "mem_pg_scope_check" not in sig_sql, (
        "Candidate ID must be a bound parameter, not interpolated into SQL"
    )

    # 7 scope params + 1 candidate IDs array param
    assert sig_params is not None
    assert len(sig_params) == 8, (
        f"Signal query must have 8 params (7 scope + 1 candidate IDs), got {len(sig_params)}"
    )
    assert sig_params[:7] == _scope_values_for_test(_CTX_A), (
        f"First 7 params must match scope values, got {sig_params[:7]}"
    )
    assert "mem_pg_scope_check" in list(sig_params[7]), (
        f"Last param must contain the candidate record ID, got {sig_params[7]}"
    )


def test_postgres_search_signal_read_failure_degrades_to_plain_bm25(monkeypatch):
    """When signal SELECT raises, search rolls back and returns plain BM25 results."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend
    from agent.local_memory.store import _bm25_scores

    conn = _FailingSignalSelectConnection()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    text = "kafka notes"
    conn.search_rows = [_pg_row(record_id="mem_pg_degrade", text=text, score=0.5)]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r.id == "mem_pg_degrade"
    assert r.matched_via == "bm25", (
        f"signal read failure must degrade to 'bm25', got {r.matched_via!r}"
    )
    expected_okapi = _bm25_scores("kafka", [text])[0]
    assert r.bm25_score == pytest.approx(expected_okapi, abs=1e-9)
    assert r.score == pytest.approx(r.bm25_score), (
        f"score {r.score} must equal bm25_score {r.bm25_score} when no boost applied"
    )
    assert conn.rollbacks >= 1, "Signal SELECT failure must trigger rollback"


def test_postgres_search_nonmatching_signal_does_not_boost(monkeypatch):
    """Signal lines whose text does not BM25-match the query must not boost the record."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [_pg_row(record_id="mem_pg_no_signal_boost", text="kafka notes", score=0.5)]
    conn.signal_rows = [
        {"record_id": "mem_pg_no_signal_boost", "signal_line": "unrelated entity term xyz"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:***@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=1)

    assert len(results) == 1
    r = results[0]
    assert r.matched_via == "bm25", (
        f"non-matching signal must not boost, expected 'bm25', got {r.matched_via!r}"
    )
    assert r.score == pytest.approx(r.bm25_score)


def test_postgres_search_signal_success_commits_read_transaction(monkeypatch):
    """Successful signal SELECT must commit so Postgres sessions are not left idle in transaction."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [_pg_row(record_id="mem_pg_signal_commit", text="kafka notes", score=0.5)]
    conn.signal_rows = [
        {"record_id": "mem_pg_signal_commit", "signal_line": "kafka commit signal"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:***@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    before_search_commits = conn.commits
    results = backend.search(_CTX_A, "kafka", limit=1)

    assert len(results) == 1
    search_commits = conn.commits - before_search_commits
    assert search_commits >= 2, (
        "Expected one commit for the ranked search SELECT and one commit for the "
        f"signal SELECT, got {search_commits}"
    )


# ---------------------------------------------------------------------------
# M5B — Postgres search overfetch for signal-boost candidates (seventeenth slice)
# ---------------------------------------------------------------------------


def test_postgres_search_overfetches_signal_strong_record_beyond_limit_window(monkeypatch):
    """With limit=2, a third SQL row that has a strong matching signal must appear
    in the final results after overfetch re-ranking, displacing a lower-boosted row."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    # Two higher-base rows with no signals, plus a lower-base third row with a strong signal.
    # After boost: row3 score = 0.10 + 0.40 = 0.50, row2 score = 0.45, row1 score = 0.80.
    # Sorted: row1 (0.80), row3 (0.50), row2 (0.45). With limit=2 after overfetch re-rank,
    # the final results should be [row1, row3], not [row1, row2].
    conn.search_rows = [
        _pg_row(record_id="mem_pg_high1", text="kafka pipeline notes", score=0.80),
        _pg_row(record_id="mem_pg_high2", text="kafka stream notes", score=0.45),
        _pg_row(record_id="mem_pg_low_signal", text="kafka signal notes", score=0.10),
    ]
    conn.signal_rows = [
        {"record_id": "mem_pg_low_signal", "signal_line": "kafka entity term"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=2)

    assert len(results) == 2
    result_ids = [r.id for r in results]
    assert "mem_pg_low_signal" in result_ids, (
        f"Signal-boosted third row must appear in limit=2 results after overfetch, got {result_ids}"
    )
    assert "mem_pg_high2" not in result_ids, (
        f"Unboosted second row should be displaced by the boosted third row, got {result_ids}"
    )


def test_postgres_search_binds_overfetched_limit_param(monkeypatch):
    """The SQL LIMIT bound to _postgres_search_sql() must be greater than the requested
    limit and equal to the computed overfetch window size (not safe_limit itself)."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [_pg_row(record_id="mem_pg_ovf_limit", text="kafka notes", score=0.5)]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    backend.search(_CTX_A, "kafka", limit=3)

    ranked_calls = [
        (sql, params)
        for sql, params in conn.executed
        if "from ranked" in " ".join(sql.lower().split())
    ]
    assert ranked_calls, "Expected a ranked search SQL execution"
    _sql, params = ranked_calls[0]
    sql_limit_param = params[-1]
    assert sql_limit_param > 3, (
        f"SQL LIMIT param must be greater than requested limit=3 (overfetch), got {sql_limit_param}"
    )
    from agentops_runtime import memory_record_store as mod
    overfetch_limit = getattr(mod, "_pg_overfetch_limit", lambda n: None)(3)
    assert overfetch_limit is not None, "_pg_overfetch_limit helper must exist in production module"
    assert sql_limit_param == overfetch_limit, (
        f"SQL LIMIT param {sql_limit_param} must equal _pg_overfetch_limit(3)={overfetch_limit}"
    )


def test_postgres_search_overfetch_truncates_to_requested_limit(monkeypatch):
    """Even when SQL returns more rows than requested, final results length must equal
    the requested limit (not the overfetch window size)."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [
        _pg_row(record_id=f"mem_pg_trunc_{i}", text=f"kafka notes {i}", score=0.9 - i * 0.1)
        for i in range(8)
    ]
    conn.signal_rows = []

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=3)

    assert len(results) == 3, (
        f"Final result count must equal requested limit=3, got {len(results)}"
    )


def test_postgres_search_overfetch_excludes_signal_only_record(monkeypatch):
    """A signal row whose record_id does not appear in the SQL ranked search rows
    must never appear in the final results or be passed as a candidate to _pg_signal_boosts."""
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [
        _pg_row(record_id="mem_pg_real_hit", text="kafka notes", score=0.5),
    ]
    conn.signal_rows = [
        {"record_id": "mem_pg_signal_only", "signal_line": "kafka entity term"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=2)

    result_ids = [r.id for r in results]
    assert "mem_pg_signal_only" not in result_ids, (
        f"Signal-only record must not appear in results, got {result_ids}"
    )
    signal_query_calls = [
        params
        for sql, params in conn.executed
        if "memory_signals" in sql.lower() and "record_id = any" in sql.lower()
    ]
    if signal_query_calls:
        candidate_ids = list(signal_query_calls[0][-1])
        assert "mem_pg_signal_only" not in candidate_ids, (
            f"Signal-only ID must not be in candidate array sent to signal query, got {candidate_ids}"
        )


# ---------------------------------------------------------------------------
# M5B — Postgres candidate-window Okapi BM25 parity (eighteenth slice)
# ---------------------------------------------------------------------------


def test_postgres_search_excludes_zero_okapi_vector_only_signal_candidates(monkeypatch):
    """Postgres must not return or signal-boost vector-only rows with zero Okapi BM25.

    SQL can return vector-only candidates whose text does not keyword-match the
    query.  SQLite only signal-boosts and returns BM25-positive candidates; the
    Postgres candidate-window rerank path must preserve that invariant instead
    of allowing a matching signal to pull a zero-Okapi row into results.
    """
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    conn.search_rows = [
        _pg_row(record_id="mem_pg_vector_only", text="unrelated vector document", score=0.9),
    ]
    conn.signal_rows = [
        {"record_id": "mem_pg_vector_only", "signal_line": "kafka signal entity"},
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:***@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "kafka", limit=1)

    assert results == []
    signal_query_calls = [
        params
        for sql, params in conn.executed
        if "memory_signals" in sql.lower() and "record_id = any" in sql.lower()
    ]
    if signal_query_calls:
        assert list(signal_query_calls[0][-1]) == []


def test_postgres_search_ranks_by_okapi_bm25_not_sql_score(monkeypatch):
    """Postgres search must rank by Okapi BM25, not by the SQL candidate score.

    The SQL score (vector+FTS blend) favours the long padded doc (score=0.9)
    over the short doc (score=0.1).  Okapi BM25 length-normalises and favours
    the short doc.  If the production code still uses the raw SQL score as the
    sort key, the long doc ranks first and this test is RED.
    """
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    long_text = "satellite " + "padding " * 60
    short_text = "satellite orbit"
    # SQL score deliberately favours the long doc — Okapi BM25 must override
    conn.search_rows = [
        _pg_row(record_id="mem_pg_bm25_long", text=long_text, score=0.9),
        _pg_row(record_id="mem_pg_bm25_short", text=short_text, score=0.1),
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "satellite", limit=2)

    assert len(results) == 2
    ids = [r.id for r in results]
    assert ids.index("mem_pg_bm25_short") < ids.index("mem_pg_bm25_long"), (
        f"Okapi BM25 must rank short doc before long doc, got order {ids}"
    )


def test_postgres_bm25_score_field_is_okapi_value_not_sql_score(monkeypatch):
    """SearchResult.bm25_score must equal the raw Okapi BM25 value, not the SQL score.

    The SQL score is set to 0.77 (a distinctive sentinel value).  Okapi BM25
    for the same text produces approximately 0.288.  If production code stores
    the SQL score in bm25_score, the assertion against the Okapi value fails
    and this test is RED.
    """
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend
    from agent.local_memory.store import _bm25_scores

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    text = "cluster nodes processing"
    sql_score = 0.77
    conn.search_rows = [
        _pg_row(record_id="mem_pg_okapi_field", text=text, score=sql_score),
    ]

    backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    results = backend.search(_CTX_A, "cluster", limit=1)

    assert len(results) == 1
    r = results[0]
    expected_okapi = _bm25_scores("cluster", [text])[0]
    assert r.bm25_score != pytest.approx(sql_score, abs=1e-3), (
        f"bm25_score must not equal SQL score {sql_score} — must be Okapi BM25"
    )
    assert r.bm25_score == pytest.approx(expected_okapi, abs=1e-9), (
        f"bm25_score {r.bm25_score} must equal Okapi BM25 {expected_okapi}"
    )


# ---------------------------------------------------------------------------
# M5B — embedder packaging / install hint (nineteenth slice)
# ---------------------------------------------------------------------------


def test_load_default_embed_fn_failure_mentions_hermes_agent_deep_memory(monkeypatch):
    """When _get_chroma_embedding_function raises, the RuntimeError must mention
    hermes-agent[deep-memory] and must NOT mention agentops[deep-memory].
    """
    import agentops_runtime.memory_record_store as mod

    monkeypatch.setattr(
        mod,
        "_get_chroma_embedding_function",
        lambda *_a, **_kw: (_ for _ in ()).throw(ImportError("chromadb not installed")),
        raising=False,
    )

    with pytest.raises(RuntimeError) as exc_info:
        mod._load_default_embed_fn()

    error_msg = str(exc_info.value)
    assert "hermes-agent[deep-memory]" in error_msg, (
        f"Error must mention 'hermes-agent[deep-memory]', got: {error_msg!r}"
    )
    assert "agentops[deep-memory]" not in error_msg, (
        f"Error must NOT mention 'agentops[deep-memory]', got: {error_msg!r}"
    )


def test_compute_pg_embedding_failure_mentions_hermes_agent_deep_memory(monkeypatch):
    """The real Postgres embedding path must preserve the install-extra hint."""
    import agentops_runtime.memory_record_store as mod

    monkeypatch.setattr(
        mod,
        "_load_default_embed_fn",
        lambda: (_ for _ in ()).throw(RuntimeError("install 'hermes-agent[deep-memory]'")),
    )
    backend = object.__new__(mod.RelationalMemoryRecordBackend)
    backend._embed_fn = None

    with pytest.raises(RuntimeError) as exc_info:
        backend._compute_pg_embedding("hello")

    error_msg = str(exc_info.value)
    assert "hermes-agent[deep-memory]" in error_msg
    assert "embed_fn=" in error_msg
    assert "agentops[deep-memory]" not in error_msg


def test_postgres_and_sqlite_agree_on_bm25_ordering(tmp_path, monkeypatch):
    """Postgres and SQLite must agree on BM25 ranking order for the same candidate corpus.

    SQLite computes Okapi BM25 natively; Postgres must compute the same scores
    over the candidate window rows.  When the fake Postgres candidate window
    contains the same two docs as the SQLite store, both backends must rank the
    short doc above the long padded doc.  SQL scores are set to the opposite
    order to confirm Postgres is not using the DB score as its sort key.
    """
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    long_text = "pipeline " + "noise " * 50
    short_text = "pipeline orchestration"

    sqlite_backend = RelationalMemoryRecordBackend(f"sqlite:///{tmp_path}/agree.db")
    sqlite_backend.upsert_record(_CTX_A, text=long_text, record_id="mem_agree_long")
    sqlite_backend.upsert_record(_CTX_A, text=short_text, record_id="mem_agree_short")
    sqlite_results = sqlite_backend.search(_CTX_A, "pipeline", limit=2)
    sqlite_ids = [r.id for r in sqlite_results]
    assert len(sqlite_ids) >= 2, "SQLite must return both docs"
    assert sqlite_ids[0] == "mem_agree_short", (
        f"SQLite must rank short doc first, got {sqlite_ids}"
    )

    conn = _FakePostgresConnectionWithSignals()
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePsycopg2(conn))
    # SQL scores reversed: long=0.9 would rank first if Postgres uses SQL score
    conn.search_rows = [
        _pg_row(record_id="mem_agree_long", text=long_text, score=0.9),
        _pg_row(record_id="mem_agree_short", text=short_text, score=0.1),
    ]

    pg_backend = RelationalMemoryRecordBackend(
        "postgresql://user:pass@db.example/deepmem",
        embed_fn=_make_fake_embed_fn(),
    )
    pg_results = pg_backend.search(_CTX_A, "pipeline", limit=2)
    pg_ids = [r.id for r in pg_results]

    assert len(pg_ids) == 2, f"Postgres must return both docs, got {pg_ids}"
    assert pg_ids == sqlite_ids[:2], (
        f"Postgres and SQLite must agree on BM25 order: SQLite={sqlite_ids}, Postgres={pg_ids}"
    )
