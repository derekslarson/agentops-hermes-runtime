"""TDD tests for RelationalMemoryRecordBackend (M5B durable store slice).

Contract: upsert/get, scope isolation, idempotency, durability, safe errors.
All tests written before implementation — run RED first, then GREEN.
"""
from __future__ import annotations

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


def test_postgres_scaffold_fails_closed():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

    sentinel_password = "LEAKSENTINEL123"
    with pytest.raises(Exception) as exc_info:
        RelationalMemoryRecordBackend(f"postgresql://user:{sentinel_password}@localhost:5432/db")
    error_msg = str(exc_info.value)
    assert sentinel_password not in error_msg


def test_postgres_url_error_message_is_informative():
    from agentops_runtime.memory_record_store import RelationalMemoryRecordBackend

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
