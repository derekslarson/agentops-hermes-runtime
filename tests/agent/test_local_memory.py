from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import threading

import pytest

from agent.local_memory.provider import LocalDeepMemoryProvider, format_hints, fenced_record
from agent.local_memory.store import (
    LocalMemoryStore,
    SearchResult,
    DeepMemoryUnavailableError,
    UnsupportedFilterError,
    _bm25_scores,
)
from plugins.memory import load_memory_provider


def test_local_store_verbatim_search_filters_and_fetch(tmp_path):
    store = LocalMemoryStore(tmp_path / "deep-memory")
    rec = store.upsert_record(
        text="Verbatim source says Derek's llama.cpp GPU note matters for local inference.",
        metadata={"project": "agentops", "type": "note", "tags": ["llama", "gpu"]},
        source="unit",
        source_uri="file://note.md",
        timestamp=123.0,
    )

    stats = store.stats()
    assert stats["storage_backend"] == "chromadb"
    assert stats["embedding_provider"] == "onnx-minilm"
    assert stats["embedding_model"] == "all-MiniLM-L6-v2"

    results = store.search("llama GPU inference", filters={"project": "agentops"}, limit=3)
    assert [r.id for r in results] == [rec.id]
    assert "llama" in results[0].excerpt.lower()

    assert store.search("llama", filters={"project": "other"}, limit=3) == []
    fetched = store.get_record(rec.id)
    assert fetched is not None
    assert fetched.text == "Verbatim source says Derek's llama.cpp GPU note matters for local inference."
    store.close()


def test_local_store_uses_semantic_chroma_not_hashing(tmp_path):
    store = LocalMemoryStore(tmp_path / "semantic")
    auto = store.upsert_record(
        text="The mechanic repaired the automobile engine after the highway breakdown.",
        metadata={"topic": "vehicles"},
        source="unit",
    )
    store.upsert_record(
        text="A sourdough bread recipe needs flour, water, salt, and patient fermentation.",
        metadata={"topic": "cooking"},
        source="unit",
    )

    hits = store.search("car motor fixed on the road", limit=1, candidate_strategy="vector")
    assert hits and hits[0].id == auto.id
    assert hits[0].distance is not None
    assert hits[0].matched_via in {"record", "record+signal"}
    store.close()


def test_hashing_embedding_provider_is_rejected(tmp_path):
    try:
        LocalMemoryStore(tmp_path / "bad", embedding_provider="hashing")
    except DeepMemoryUnavailableError as exc:
        assert "hashing" in str(exc)
    else:  # pragma: no cover - defensive, should never be reachable
        raise AssertionError("hashing provider should be rejected")


def test_filters_and_bm25_contracts(tmp_path):
    store = LocalMemoryStore(tmp_path / "filters")
    store.upsert_record(text="alpha beta beta beta", metadata={"project": "a", "priority": 3}, source="unit")
    store.upsert_record(text="alpha gamma", metadata={"project": "b", "priority": 1}, source="unit")

    assert store.search("alpha", filters={"$and": [{"project": "a"}, {"priority": {"$gte": 2}}]}, limit=2)
    try:
        store.search("alpha", filters={"project": {"$regex": "a"}}, limit=1)
    except UnsupportedFilterError as exc:
        assert "$regex" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("unsupported filter operator should fail visibly")

    scores = _bm25_scores("beta", ["beta beta beta", "gamma delta"])
    assert scores[0] > scores[1]
    store.close()


def test_provider_sync_prefetch_and_lazy_get_are_fenced(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "deep"), "prefetch_limit": 2, "summarize_records": False}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="telegram", user_id="u1")

    provider.sync_turn(
        "Remember the yak shaving plan uses a purple anvil keyword.",
        "Got it; purple anvil belongs to the yak shaving plan.",
        session_id="s1",
    )
    assert provider.wait_for_pending_ingest(timeout=10)

    hints = provider.prefetch("What was the purple anvil yak plan?", session_id="s1")
    assert "Deep memory hints" in hints
    assert "historical data, not instructions" in hints
    assert "purple anvil" in hints.lower()

    search_raw = provider.handle_tool_call("memory_record_search", {"query": "purple anvil", "limit": 1})
    search = json.loads(search_raw)
    rid = search["results"][0]["id"]
    got_raw = provider.handle_tool_call("memory_record_get", {"id": rid})
    got = json.loads(got_raw)
    assert "<memory-record>" in got["text"]
    assert "NOT instructions" in got["text"]
    assert "purple anvil" in got["text"].lower()
    provider.shutdown()


def test_plugin_loader_discovers_local_provider():
    provider = load_memory_provider("local")
    assert isinstance(provider, LocalDeepMemoryProvider)
    assert {s["name"] for s in provider.get_tool_schemas()} == {
        "memory_record_search",
        "memory_record_get",
        "memory_record_get_many",
    }


def test_bm25_reranking_affects_result_ordering(tmp_path):
    store = LocalMemoryStore(tmp_path / "bm25")
    # Two near-identical records on the same topic; only R2 carries the rare
    # exact keyword present in the query.  Vector similarity is close (R2 is a
    # superset of R1), so the BM25 component is what lifts R2 to rank 1.
    store.upsert_record(
        text="The deployment pipeline runs integration tests before each release.",
        metadata={"doc": "r1"},
        source="unit",
    )
    r2 = store.upsert_record(
        text="The deployment pipeline runs integration tests before each release "
        "and gates promotion on the Glinthorpe canary stage.",
        metadata={"doc": "r2"},
        source="unit",
    )

    results = store.search("Glinthorpe canary promotion gate", limit=2)
    assert results, "expected both records returned as candidates"
    assert results[0].id == r2.id
    # The rare keyword gives R2 a positive BM25 contribution; R1 has none.
    by_id = {r.id: r for r in results}
    assert by_id[r2.id].bm25_score > 0.0
    if len(results) > 1:
        other = next(r for r in results if r.id != r2.id)
        assert other.bm25_score == 0.0
        assert by_id[r2.id].bm25_score > other.bm25_score
    store.close()


def test_search_strategies_and_max_distance_gate(tmp_path):
    store = LocalMemoryStore(tmp_path / "strat")
    near = store.upsert_record(
        text="Raft consensus uses leader election and a replicated log.",
        metadata={"topic": "distributed"},
        source="unit",
    )
    store.upsert_record(
        text="Banana bread tastes best with ripe fruit and walnuts.",
        metadata={"topic": "baking"},
        source="unit",
    )

    # union + max_distance>0 must NOT raise (regression: it used to throw
    # ValueError for the default strategy whenever a distance gate was set).
    gated = store.search("Raft leader election", limit=5, max_distance=0.9, candidate_strategy="union")
    assert all(r.distance is None or r.distance <= 0.9 for r in gated)
    assert any(r.id == near.id for r in gated)

    # A very tight gate drops the unrelated record entirely.
    tight = store.search("Raft leader election", limit=5, max_distance=0.2)
    assert all(r.id == near.id for r in tight) or tight == []

    # vector strategy also works and never raises.
    assert store.search("Raft leader election", limit=2, candidate_strategy="vector")

    # Bogus strategy fails visibly.
    try:
        store.search("anything", candidate_strategy="bogus")
    except ValueError as exc:
        assert "candidate_strategy" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("invalid candidate_strategy should raise")
    store.close()


def test_ingest_markers_are_isolated_from_search_and_counts(tmp_path):
    store = LocalMemoryStore(tmp_path / "markers")
    rec = store.upsert_record(text="A real verbatim memory record about ospreys.", source="unit")
    baseline = store.stats()

    assert store.is_ingested("evt-1") is False
    assert store.mark_ingested("evt-1", rec.id, "file://x") is True
    assert store.mark_ingested("evt-1", rec.id, "file://x") is False  # idempotent
    assert store.is_ingested("evt-1") is True

    after = store.stats()
    # Markers live in their own collection: searchable record + signal counts
    # are unchanged; markers are tracked separately.
    assert after["records"] == baseline["records"]
    assert after["signals"] == baseline["signals"]
    assert after["ingest_markers"] == 1

    # The marker must never surface in search results.
    hits = store.search("ingest event ospreys", limit=10)
    assert all(h.record_kind != "ingest_event" for h in hits)
    assert all("ingest event" not in h.excerpt.lower() for h in hits)
    store.close()


def test_store_reopen_preserves_records(tmp_path):
    path = tmp_path / "reopen"
    store = LocalMemoryStore(path)
    rec = store.upsert_record(text="Persisted record about quokkas and selfies.", source="unit")
    store.close()

    # Reopening the same path must accept the persisted collection (guards the
    # ONNX embedding-function identity spoofing against regressions).
    store2 = LocalMemoryStore(path)
    again = store2.get_record(rec.id)
    assert again is not None and again.text == "Persisted record about quokkas and selfies."
    hits = store2.search("quokka selfie", limit=1)
    assert hits and hits[0].id == rec.id
    store2.close()


def test_provider_noop_when_backend_unavailable(tmp_path, monkeypatch):
    import agent.local_memory.provider as provider_mod

    monkeypatch.setattr(provider_mod, "ensure_local_memory_backend", lambda: False)
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "noop")}})

    # With the backend absent, the provider cleanly disables instead of crashing.
    assert provider.is_available() is False
    # initialize() is never called by the host when is_available() is False, so
    # the recall/sync/tool surfaces must degrade gracefully on an uninitialized
    # provider rather than raise.
    assert provider.prefetch("anything") == ""
    provider.sync_turn("user", "assistant")  # no-op, no raise
    err = provider.handle_tool_call("memory_record_search", {"query": "x"})
    assert "error" in err


def test_provider_availability_uses_lazy_backend_installer(tmp_path, monkeypatch):
    import agent.local_memory.provider as provider_mod

    calls = []
    monkeypatch.setattr(provider_mod, "ensure_local_memory_backend", lambda: calls.append("ensure") or True)
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "lazy")}})

    assert provider.is_available() is True
    assert calls == ["ensure"]


def test_automatic_prefetch_injection_enabled_by_default():
    # Provider-level defaults keep automatic recall on.
    from agent.local_memory.provider import DEFAULT_CONFIG as PROVIDER_DEFAULTS

    assert PROVIDER_DEFAULTS["enabled"] is True
    assert PROVIDER_DEFAULTS["prefetch_enabled"] is True
    assert PROVIDER_DEFAULTS["sync_turns"] is True

    # The shipped AgentOps fork config carries a dedicated, top-level
    # ``deep_memory`` section (kept separate from the curated ``memory``
    # provider surface) and enables deep memory + prefetch on by default for
    # local-compatible profiles.
    from hermes_cli.config import DEFAULT_CONFIG as HERMES_DEFAULTS

    deep = HERMES_DEFAULTS["deep_memory"]
    assert deep["enabled"] is True
    assert deep["prefetch_enabled"] is True
    assert deep["embedding_provider"] == "onnx-minilm"
    assert deep["embedding_model"] == "all-MiniLM-L6-v2"


def test_deep_memory_is_separate_from_curated_memory_provider():
    # The roadmap requires curated memory (native memory tool / external
    # providers via ``memory.provider``) and deep memory to stay distinct
    # surfaces. Deep memory must NOT hijack ``memory.provider``.
    from hermes_cli.config import DEFAULT_CONFIG as HERMES_DEFAULTS

    assert "deep_memory" in HERMES_DEFAULTS
    assert HERMES_DEFAULTS["memory"]["provider"] == ""


def test_no_palace_terminology_in_user_facing_surface():
    provider = load_memory_provider("local")
    palace_terms = ["wing", "room", "tunnel", "palace", "closet", "drawer", "diary", "ontology"]
    surface = provider.system_prompt_block().lower()
    for schema in provider.get_tool_schemas():
        surface += " " + str(schema.get("name", "")).lower()
        surface += " " + str(schema.get("description", "")).lower()
        surface += " " + json.dumps(schema.get("parameters", {})).lower()
    for term in palace_terms:
        assert term not in surface, f"palace term {term!r} leaked into user-facing surface"


def test_incomplete_turns_are_not_synced(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "incomplete"), "sync_platforms": ["cli"], "summarize_records": False}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    assert provider.store is not None
    baseline = provider.store.stats()["records"]

    # An interrupted/incomplete turn (empty assistant side) must not persist.
    provider.sync_turn("User asked something", "", session_id="s1")
    provider.sync_turn("", "Assistant replied", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline

    # A completed user+assistant exchange is stored verbatim.
    provider.sync_turn("User asked the real question", "Assistant gave the real answer", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline + 1
    provider.shutdown()


def test_mempalace_json_import_maps_wing_room_to_legacy_metadata(tmp_path):
    export = tmp_path / "drawers.jsonl"
    export.write_text(
        json.dumps({
            "id": "drawer-1",
            "wing": "old_wing",
            "room": "old_room",
            "content": "Legacy palace drawer verbatim text about flat memory migration.",
            "metadata": {"project": "hermes"},
        }) + "\n",
        encoding="utf-8",
    )
    storage = tmp_path / "store"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(tmp_path / "home")
    cmd = [
        sys.executable,
        "scripts/local_memory_import.py",
        "--storage-dir",
        str(storage),
        "mempalace-json",
        str(export),
    ]
    result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[2], env=env, text=True, capture_output=True, check=True)
    assert "imported 1" in result.stdout

    store = LocalMemoryStore(storage)
    hits = store.search("flat memory migration", filters={"legacy_wing": "old_wing", "legacy_room": "old_room"}, limit=1)
    assert len(hits) == 1
    rec = store.get_record(hits[0].id)
    assert rec is not None
    assert rec.text == "Legacy palace drawer verbatim text about flat memory migration."
    assert rec.metadata["legacy_wing"] == "old_wing"
    assert rec.metadata["legacy_room"] == "old_room"
    assert "wing" not in rec.metadata
    assert "room" not in rec.metadata
    store.close()


def test_import_requires_explicit_target_scope(tmp_path):
    # Bulk history imports must require an explicit target scope/profile so a
    # stray run cannot contaminate the wrong org/user/project's records.
    export = tmp_path / "drawers.jsonl"
    export.write_text(
        json.dumps({"id": "d1", "content": "some legacy text", "metadata": {}}) + "\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("HERMES_HOME", None)
    cmd = [
        sys.executable,
        "scripts/local_memory_import.py",
        "mempalace-json",
        str(export),
    ]
    result = subprocess.run(cmd, cwd=Path(__file__).resolve().parents[2], env=env, text=True, capture_output=True)
    assert result.returncode != 0
    assert "explicit target" in (result.stderr + result.stdout).lower()


def test_update_record_summary_preserves_verbatim_text_and_id(tmp_path):
    store = LocalMemoryStore(tmp_path / "summary")
    verbatim = "User:\nWhat is the capybara plan?\n\nAssistant:\nThe capybara plan uses a teal sprocket."
    rec = store.upsert_record(text=verbatim, metadata={"project": "zoo"}, source="unit", source_uri="file://cap.md")

    updated = store.update_record_summary(rec.id, "Capybara plan uses a teal sprocket.", status="ready", model="gemma4:e4b-mlx", version="v1")
    assert updated is not None
    # Summary is metadata only: ID, verbatim text, and text hash are untouched.
    assert updated.id == rec.id
    assert updated.text == verbatim
    assert updated.text_hash == rec.text_hash
    assert updated.summary == "Capybara plan uses a teal sprocket."
    assert updated.summary_status == "ready"
    assert updated.summary_model == "gemma4:e4b-mlx"
    assert updated.summary_version == "v1"
    assert updated.summary_generated_at is not None

    refetched = store.get_record(rec.id)
    assert refetched is not None
    assert refetched.text == verbatim
    assert refetched.summary == "Capybara plan uses a teal sprocket."
    store.close()


def test_search_results_include_summary_fields(tmp_path):
    store = LocalMemoryStore(tmp_path / "summary_search")
    rec = store.upsert_record(text="Llama.cpp GPU inference benchmark notes for local models.", source="unit")
    store.update_record_summary(rec.id, "Notes on llama.cpp GPU inference benchmarks.", status="ready", model="gemma4:e4b-mlx")

    results = store.search("llama gpu inference", limit=1)
    assert results
    r = results[0]
    assert r.summary == "Notes on llama.cpp GPU inference benchmarks."
    assert r.summary_status == "ready"
    assert r.summary_model == "gemma4:e4b-mlx"
    store.close()


def _search_result(**overrides):
    base = dict(
        id="mem_abc",
        score=0.812,
        rank=1,
        excerpt="raw excerpt fallback text",
        text="full short verbatim record text",
        metadata={"type": "conversation_turn"},
        source="conversation_turn",
        source_uri="",
        timestamp=None,
        record_kind="conversation_turn",
    )
    base.update(overrides)
    return SearchResult(**base)


def test_format_hints_prefers_ready_summary_over_excerpt():
    sr = _search_result(summary="Derek wants Gemma to summarize records off the request path.", summary_status="ready")
    out = format_hints([sr])
    assert "summary: Derek wants Gemma to summarize records off the request path." in out
    assert "\n  excerpt:" not in out
    assert "summary_status=ready" in out


def test_format_hints_falls_back_to_excerpt_when_summary_unavailable():
    for status, summary in [("", ""), ("pending", ""), ("failed", ""), ("ready", "")]:
        sr = _search_result(summary=summary, summary_status=status)
        out = format_hints([sr])
        assert "\n  excerpt: raw excerpt fallback text" in out, status
        assert "\n  summary:" not in out, status


def test_format_hints_uses_verbatim_text_for_short_skipped_records():
    sr = _search_result(
        summary="",
        summary_status="skipped",
        excerpt="clipped fallback",
        text="User: tiny ask under threshold. Assistant: tiny answer.",
    )
    out = format_hints([sr])
    assert "\n  verbatim_text:\n    User: tiny ask under threshold. Assistant: tiny answer." in out
    assert "\n  excerpt:" not in out
    assert "\n  summary:" not in out


def test_async_sync_turn_does_not_block_on_summarizer(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "async"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")

    release = threading.Event()
    completed = []

    def blocking_summarizer(text):
        # Block until the test thread releases it. If sync_turn ran the
        # summarizer inline, the test would deadlock below (release is not set
        # yet), proving the summarizer runs off the request path.
        release.wait(10)
        completed.append(text)
        return "Durable summary of the turn."

    provider._summarizer = blocking_summarizer
    provider.sync_turn("Tell me about the magenta gizmo plan.", "The magenta gizmo plan ships on Tuesday.", session_id="s1")

    # Reached here without deadlock => sync_turn returned without waiting on the
    # summarizer. The summarizer has not completed because release is unset.
    assert completed == []

    release.set()
    assert provider.wait_for_pending_ingest(timeout=10)
    assert completed, "summarizer should eventually run in the background"
    provider.shutdown()


def test_background_worker_writes_record_and_summary(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "bg"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    provider._summarizer = lambda text: "Concise durable summary about wombat tunnels."

    provider.sync_turn("Where do wombats dig their tunnels?", "Wombats dig tunnels in firm soil banks.", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)

    results = provider.store.search("wombat tunnels", limit=1)
    assert results
    assert results[0].summary_status == "ready"
    assert results[0].summary == "Concise durable summary about wombat tunnels."
    provider.shutdown()


def test_short_turns_skip_summary_and_use_verbatim_hint(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "short"), "sync_platforms": ["cli"], "prefetch_limit": 1, "summary_min_chars": 300}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    called = []
    provider._summarizer = lambda text: called.append(text) or "Should not be called."

    provider.sync_turn("ok?", "yep", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)

    assert called == []
    results = provider.store.search("ok yep", limit=1)
    assert results
    assert results[0].summary_status == "skipped"
    hints = provider.prefetch("ok yep", session_id="s1")
    assert "verbatim_text:" in hints
    assert "    User:\n    ok?\n    \n    Assistant:\n    yep" in hints
    assert "excerpt:" not in hints
    assert "summary:" not in hints
    provider.shutdown()


def test_summarizer_failure_still_inserts_record_with_excerpt_fallback(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "fail"), "sync_platforms": ["cli"], "prefetch_limit": 1, "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")

    def boom(text):
        raise RuntimeError("ollama is not installed")

    provider._summarizer = boom
    provider.sync_turn("How do ospreys hunt?", "Ospreys dive feet-first to catch fish near the surface.", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)

    # The verbatim record is still inserted even though the summary failed.
    results = provider.store.search("ospreys hunt fish", limit=1)
    assert results
    assert results[0].summary_status == "failed"

    # Hints fall back to the excerpt when no summary is available.
    hints = provider.prefetch("ospreys hunt fish", session_id="s1")
    assert "excerpt:" in hints
    assert "summary:" not in hints
    provider.shutdown()


def test_memory_record_get_returns_full_verbatim_not_summary(tmp_path):
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "verbatim"), "sync_platforms": ["cli"], "prefetch_limit": 1, "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    provider._summarizer = lambda text: "Tiny summary."

    provider.sync_turn(
        "Please remember the detailed quokka selfie protocol verbatim.",
        "The detailed quokka selfie protocol requires patience and a low angle.",
        session_id="s1",
    )
    assert provider.wait_for_pending_ingest(timeout=10)

    search = json.loads(provider.handle_tool_call("memory_record_search", {"query": "quokka selfie protocol", "limit": 1}))
    rid = search["results"][0]["id"]
    got = json.loads(provider.handle_tool_call("memory_record_get", {"id": rid}))
    assert "quokka selfie protocol requires patience" in got["text"]
    assert "Tiny summary." not in got["text"]
    provider.shutdown()


def test_shutdown_does_not_close_store_under_slow_summarizer(tmp_path):
    # Blocker 1 regression: a slow summarizer must not leave a live daemon
    # worker using a closed/None store after shutdown, and the worker handle
    # must not be nulled while the thread is still alive.
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "slow"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    # Bound the shutdown wait well under the (blocked) summary so the test is fast.
    provider._shutdown_join_timeout = 0.3

    release = threading.Event()
    summarized = []

    def slow_summarizer(text):
        release.wait(10)
        summarized.append(text)
        return "Durable summary after a slow local model run."

    provider._summarizer = slow_summarizer
    store = provider.store
    provider.sync_turn("Where do marmots burrow?", "Marmots burrow on alpine slopes.", session_id="s1")

    # Wait until the verbatim record is written and the worker is parked inside
    # the slow summarizer, so shutdown genuinely races a live summarization.
    rid = None
    deadline = time.time() + 5
    while time.time() < deadline:
        hits = store.search("marmots burrow", limit=1)
        if hits:
            rid = hits[0].id
            break
        time.sleep(0.02)
    assert rid is not None

    provider.shutdown()

    # Store stays open and the live worker is not hidden by _worker = None.
    assert provider.store is store
    assert provider._worker is not None and provider._worker.is_alive()
    # New work is refused once stopping, instead of resurrecting a worker.
    provider.sync_turn("late turn", "late reply", session_id="s1")

    # Release the summarizer; the still-live worker completes its summary update
    # against the still-open store (no closed/None-store crash) and then exits.
    release.set()
    deadline = time.time() + 10
    rec = None
    while time.time() < deadline:
        rec = store.get_record(rid)
        if rec and rec.summary_status == "ready":
            break
        time.sleep(0.02)
    assert summarized, "slow summarizer should have completed in the background"
    assert rec is not None and rec.summary_status == "ready"
    assert rec.summary == "Durable summary after a slow local model run."
    provider._worker.join(timeout=5)
    store.close()


def test_sync_turn_serialized_against_shutdown_stop_transition(tmp_path):
    # Blocker (race 1): the enqueue path and the shutdown stop transition share a
    # lifecycle lock, so a sync_turn that began before _stopping was observable
    # still sees it set and refuses to enqueue -- no job is stranded behind the
    # stop sentinel with no worker to drain it.
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "race1"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    provider._summarizer = lambda text: "summary"

    before = provider._ingest_queue.unfinished_tasks

    # Hold the lifecycle lock to stand in for a shutdown that is mid stop
    # transition; the racing sync_turn must block until we release it.
    provider._lifecycle_lock.acquire()
    results = []

    def racer():
        provider.sync_turn("late", "turn", session_id="s1")
        results.append("returned")

    t = threading.Thread(target=racer)
    t.start()
    time.sleep(0.1)
    # Still blocked on the lock inside sync_turn: nothing enqueued yet.
    assert results == []
    assert provider._ingest_queue.unfinished_tasks == before

    # Shutdown sets _stopping while holding the lock, then releases it.
    provider._stopping.set()
    provider._lifecycle_lock.release()

    t.join(timeout=5)
    assert results == ["returned"]
    # The racing turn observed _stopping and returned without enqueueing.
    assert provider._ingest_queue.unfinished_tasks == before
    provider.shutdown()


def test_worker_exits_when_stop_sentinel_lost(tmp_path):
    # Blocker (race 2): if the stop sentinel is ever dropped (e.g. a full queue),
    # the worker must still exit once _stopping is set and the queue drains,
    # rather than blocking forever on a get().
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "race2a"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    provider._summarizer = lambda text: "summary"

    provider.sync_turn("q", "a", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)

    worker = provider._worker
    assert worker is not None and worker.is_alive()

    # Simulate a lost sentinel: signal stopping but never enqueue _STOP.
    provider._stopping.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    provider.store.close()


def test_shutdown_drains_pending_jobs_on_full_queue(tmp_path):
    # Blocker (race 2): with the worker parked in a slow summary and the queue
    # full of pending turns, shutdown drains/drops the pending jobs (so
    # unfinished_tasks can reach zero) and the sentinel is not lost.
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "race2b"), "sync_platforms": ["cli"], "summary_min_chars": 0}})
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    provider._shutdown_join_timeout = 0.3
    # Tiny queue so we can fill it deterministically.
    import queue as _queue

    provider._ingest_queue = _queue.Queue(maxsize=2)

    release = threading.Event()

    def blocking_summarizer(text):
        release.wait(10)
        return "summary"

    provider._summarizer = blocking_summarizer
    store = provider.store

    # First turn parks the worker inside the blocked summarizer.
    provider.sync_turn("q1", "a1", session_id="s1")
    deadline = time.time() + 5
    while time.time() < deadline and not store.search("q1", limit=1):
        time.sleep(0.02)

    # Fill the queue with pending jobs that will never be processed.
    provider.sync_turn("q2", "a2", session_id="s1")
    provider.sync_turn("q3", "a3", session_id="s1")
    assert provider._ingest_queue.unfinished_tasks >= 2

    # Worker is still blocked, so shutdown leaves the store open, but it must
    # have drained the pending jobs to make room for the sentinel.
    provider.shutdown()
    assert provider.store is store

    # Release the in-flight summary; the worker finishes it, consumes the
    # sentinel and exits, and every queued task is accounted for.
    release.set()
    assert provider._worker is not None
    provider._worker.join(timeout=5)
    assert not provider._worker.is_alive()
    assert provider._ingest_queue.unfinished_tasks == 0
    store.close()


def test_redaction_failure_skips_record_and_does_not_store_or_summarize(tmp_path, monkeypatch):
    # Blocker 2 regression: with redaction enabled, a redaction failure must
    # fail closed -- the raw (secret-ish) text is neither stored nor summarized.
    def boom_redact(text, *, force=False, code_file=False):
        raise RuntimeError("redaction engine exploded")

    monkeypatch.setattr("agent.redact.redact_sensitive_text", boom_redact)

    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "redactfail"), "sync_platforms": ["cli"], "redact_secrets": True}}
    )
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")

    summarized = []
    provider._summarizer = lambda text: summarized.append(text) or "summary"

    secret = "AKIAIOSFODNN7EXAMPLE-supersecret-token"
    baseline = provider.store.stats()["records"]
    provider.sync_turn(f"My access key is {secret}", f"Saved your key {secret}", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)

    # Nothing stored, nothing summarized, raw secret never persisted/searchable.
    assert provider.store.stats()["records"] == baseline
    assert summarized == []
    assert provider.store.search(secret, limit=5) == []
    assert provider.store.search("access key supersecret token", limit=5) == []
    provider.shutdown()


def test_provider_search_tool_returns_bounded_excerpt_not_full_verbatim(tmp_path):
    # The provider's memory_record_search tool result must not contain the full
    # verbatim record text (only bounded excerpts); the full fenced record is
    # only reachable via memory_record_get.
    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "excerpt"), "sync_platforms": ["cli"], "summarize_records": False}}
    )
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    tail = "ZZTAILSECRETSENTINEL42"
    body = "alpenglow ridge survey notes " + ("padding token " * 500) + tail
    provider.store.upsert_record(text=body, source="unit", source_uri="unit://long")

    raw = provider.handle_tool_call("memory_record_search", {"query": "alpenglow ridge survey", "limit": 1})
    search = json.loads(raw)
    assert search["results"], "record should be found"
    hit = search["results"][0]
    assert "excerpt" in hit and hit["excerpt"]
    assert tail not in raw
    assert "text" not in hit or tail not in str(hit.get("text"))

    # The full fenced record is still reachable by ID.
    got = json.loads(provider.handle_tool_call("memory_record_get", {"id": hit["id"]}))
    assert tail in got["text"]
    provider.shutdown()


def test_provider_fails_closed_under_agentops_runtime_context(tmp_path, monkeypatch):
    # The MemoryManager plugin path must NOT silently open an unscoped local
    # Chroma store for an AgentOps/remote RuntimeContext. The local single-user
    # provider path stays available only for local single-user contexts.
    import agent.local_memory.provider as provider_mod
    from agent.runtime_context import RuntimeContext, use_runtime_context

    # Isolate the runtime-context guard from the dependency probe.
    monkeypatch.setattr(provider_mod, "ensure_local_memory_backend", lambda: True)
    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "agentops")}})

    agentops_ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="t1",
        backend_profile="compose-self-hosted",
    )
    with use_runtime_context(agentops_ctx):
        assert provider.is_available() is False
        with pytest.raises(RuntimeError):
            provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="telegram")
        # Fail-closed: no local store was opened.
        assert provider.store is None

    # local-multi also routes through the scoped DEEP_MEMORY backend, not this
    # single-store provider, so the provider refuses it too.
    multi_ctx = RuntimeContext(mode="agentops", user_id="bob", backend_profile="local-multi")
    with use_runtime_context(multi_ctx):
        assert provider.is_available() is False

    # Plain local single-user context (or no context) keeps the provider available.
    local_ctx = RuntimeContext(mode="local", backend_profile="local")
    with use_runtime_context(local_ctx):
        assert provider.is_available() is True
    assert provider.is_available() is True


def test_provider_consults_explicit_runtime_context_when_contextvar_unbound(tmp_path, monkeypatch):
    # Reproduces the agent_init path: agent.runtime_context is AgentOps/compose,
    # but the ContextVar is NOT yet bound (binding happens later, at turn start).
    # The provider must still fail closed via the explicitly supplied context.
    import agent.local_memory.provider as provider_mod
    from agent.runtime_context import RuntimeContext, get_current_runtime_context

    monkeypatch.setattr(provider_mod, "ensure_local_memory_backend", lambda: True)
    # The ContextVar is unbound here, exactly as during agent_init.
    assert get_current_runtime_context() is None

    provider = LocalDeepMemoryProvider({"deep_memory": {"storage_dir": str(tmp_path / "explicit")}})
    agentops_ctx = RuntimeContext(
        mode="agentops",
        org_id="acme",
        user_id="alice",
        conversation_id="t1",
        backend_profile="compose-self-hosted",
    )
    # agent_init supplies the agent's runtime_context explicitly to the provider.
    provider.set_runtime_context(agentops_ctx)

    assert provider.is_available() is False
    with pytest.raises(RuntimeError):
        provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="telegram")
    assert provider.store is None

    # A plain local single-user explicit context keeps the provider available,
    # still with the ContextVar unbound.
    provider.set_runtime_context(RuntimeContext(mode="local", backend_profile="local"))
    assert provider.is_available() is True
    # No explicit context + unbound ContextVar -> plain local single-user.
    provider.set_runtime_context(None)
    assert provider.is_available() is True


def test_agent_init_supplies_runtime_context_to_memory_provider():
    # The MemoryManager load path in agent_init must hand the agent's
    # runtime_context to the provider BEFORE checking availability / initializing,
    # so an AgentOps runtime_context fails closed even though the ContextVar is
    # only bound later at turn start. (Source-level assertion, mirroring the
    # existing agent_init wiring tests.)
    import inspect
    import agent.agent_init as agent_init

    src = inspect.getsource(agent_init)
    assert "set_runtime_context" in src, (
        "agent_init must call provider.set_runtime_context(agent.runtime_context) "
        "before is_available()/initialize_all() so non-local contexts fail closed"
    )


def test_config_defaults_include_local_summary_settings():
    from agent.local_memory.provider import DEFAULT_CONFIG as PROVIDER_DEFAULTS

    assert PROVIDER_DEFAULTS["summarize_records"] is True
    assert PROVIDER_DEFAULTS["summary_model"] == "gemma4:e4b-mlx"
    assert PROVIDER_DEFAULTS["summary_backend"] == "ollama"
    assert PROVIDER_DEFAULTS["summary_timeout_seconds"] == 60
    assert PROVIDER_DEFAULTS["summary_max_chars"] == 700

    from hermes_cli.config import DEFAULT_CONFIG as HERMES_DEFAULTS

    deep = HERMES_DEFAULTS["deep_memory"]
    assert deep["summarize_records"] is True
    assert deep["summary_model"] == "gemma4:e4b-mlx"
    assert deep["summary_backend"] == "ollama"
    assert deep["summary_timeout_seconds"] == 60
    assert deep["summary_max_chars"] == 700


def test_cron_run_type_completed_turn_excluded_by_default(tmp_path):
    # Internal-trace exclusion (M5A): cron/delegation/manual completed turns must
    # not pollute deep-memory auto-recall by default, even when the platform
    # allowlist would otherwise accept the turn.
    from agent.runtime_context import RuntimeContext

    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "rt-cron"), "sync_platforms": ["cli"], "summarize_records": False}}
    )
    provider.set_runtime_context(RuntimeContext(mode="local", run_type="cron"))
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    baseline = provider.store.stats()["records"]

    provider.sync_turn("Scheduled job asked something", "Scheduled job produced an answer", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline
    provider.shutdown()


def test_delegation_run_type_completed_turn_excluded_by_default(tmp_path):
    from agent.runtime_context import RuntimeContext

    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "rt-deleg"), "sync_platforms": ["cli"], "summarize_records": False}}
    )
    provider.set_runtime_context(RuntimeContext(mode="local", run_type="delegation"))
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    baseline = provider.store.stats()["records"]

    provider.sync_turn("Subagent task prompt", "Subagent task result", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline
    provider.shutdown()


def test_conversation_run_type_completed_turn_is_ingested(tmp_path):
    from agent.runtime_context import RuntimeContext

    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "rt-conv"), "sync_platforms": ["cli"], "summarize_records": False}}
    )
    provider.set_runtime_context(RuntimeContext(mode="local", run_type="conversation"))
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    baseline = provider.store.stats()["records"]

    provider.sync_turn("A real user question", "A real assistant answer", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline + 1
    provider.shutdown()


def test_run_type_opt_in_allows_cron_ingest(tmp_path):
    from agent.runtime_context import RuntimeContext

    provider = LocalDeepMemoryProvider(
        {"deep_memory": {
            "storage_dir": str(tmp_path / "rt-optin"),
            "sync_platforms": ["cli"],
            "summarize_records": False,
            "ingest_run_types": ["conversation", "event", "cron"],
        }}
    )
    provider.set_runtime_context(RuntimeContext(mode="local", run_type="cron"))
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    baseline = provider.store.stats()["records"]

    provider.sync_turn("Scheduled job asked something", "Scheduled job produced an answer", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline + 1
    provider.shutdown()


def test_missing_run_type_defaults_to_ingest(tmp_path):
    # No bound/explicit RuntimeContext: preserve existing behavior (treat as a
    # normal conversation turn and ingest).
    provider = LocalDeepMemoryProvider(
        {"deep_memory": {"storage_dir": str(tmp_path / "rt-none"), "sync_platforms": ["cli"], "summarize_records": False}}
    )
    provider.initialize(session_id="s1", hermes_home=str(tmp_path), platform="cli")
    baseline = provider.store.stats()["records"]

    provider.sync_turn("Plain question", "Plain answer", session_id="s1")
    assert provider.wait_for_pending_ingest(timeout=10)
    assert provider.store.stats()["records"] == baseline + 1
    provider.shutdown()
