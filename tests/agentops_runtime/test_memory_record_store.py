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
    assert search_params[-1] == 3


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
