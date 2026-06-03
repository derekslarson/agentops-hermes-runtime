from __future__ import annotations

"""MemPalace-style local deep-memory store without palace ontology.

This module intentionally keeps MemPalace's storage/retrieval shape -- ChromaDB
persistent collections, ONNX MiniLM embeddings, cosine HNSW, over-fetch + hybrid
BM25 reranking, and a secondary extracted-signal collection used only as a rank
boost -- while exposing a flat ``memory record`` API to Hermes.
"""

import hashlib
import json
import logging
import math
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 200_000
_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)
_SIGNAL_REF_RE = re.compile(r"→([^\n|]+)")
_HNSW_BLOAT_GUARD = {"hnsw:batch_size": 50_000, "hnsw:sync_threshold": 50_000}
# Mirror MemPalace's collection metadata: cosine space, single-threaded HNSW
# (deterministic, avoids segment-write races), and the bloat guards.
_COLLECTION_METADATA = {"hnsw:space": "cosine", "hnsw:num_threads": 1, **_HNSW_BLOAT_GUARD}
_SUPPORTED_OPERATORS = frozenset(
    {"$eq", "$ne", "$in", "$nin", "$and", "$or", "$contains", "$gt", "$gte", "$lt", "$lte"}
)

_EF_CACHE: dict[tuple[str, ...], Any] = {}
_WARNED_DEVICES: set[str] = set()

# all-MiniLM-L6-v2 is fixed at 384 dims. Ingest-event markers live in their own
# never-searched collection and are written with a constant unit embedding so we
# skip ONNX inference entirely for bookkeeping rows (and never pollute recall).
_EMBED_DIM = 384
_MARKER_EMBEDDING = [1.0] + [0.0] * (_EMBED_DIM - 1)


@dataclass
class MemoryRecord:
    id: str
    text: str
    text_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    source: str = "unknown"
    source_uri: str = ""
    timestamp: Optional[float] = None
    created_at: float = 0.0
    updated_at: float = 0.0
    record_kind: str = "verbatim"
    parent_id: Optional[str] = None
    embedding_status: str = "ready"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_version: str = "onnx-minilm-l6-v2"
    provenance: Dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    summary_status: str = ""
    summary_model: str = ""
    summary_generated_at: Optional[float] = None
    summary_version: str = ""


@dataclass
class SearchResult:
    id: str
    score: float
    rank: int
    excerpt: str
    text: str
    metadata: Dict[str, Any]
    source: str
    source_uri: str
    timestamp: Optional[float]
    record_kind: str
    distance: Optional[float] = None
    bm25_score: float = 0.0
    matched_via: str = "record"
    summary: str = ""
    summary_status: str = ""
    summary_model: str = ""
    summary_generated_at: Optional[float] = None
    summary_version: str = ""


class DeepMemoryUnavailableError(RuntimeError):
    """Raised when ChromaDB/ONNX dependencies are unavailable."""


class UnsupportedFilterError(ValueError):
    """Raised when a metadata filter uses unsupported operators."""


def _now() -> float:
    return time.time()


def sanitize_text(text: str) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    text = text.replace("\x00", "")
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS]
    return text


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def stable_record_id(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(str(part or "").encode("utf-8", errors="replace"))
        h.update(b"\x00")
    return "mem_" + h.hexdigest()[:32]


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj or {}, ensure_ascii=False, sort_keys=True)


def _json_loads(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback


def _tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


def _bm25_scores(query: str, documents: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Okapi-BM25 copied from MemPalace searcher semantics."""
    n_docs = len(documents)
    query_terms = set(_tokenize(query))
    if not query_terms or n_docs == 0:
        return [0.0] * n_docs
    tokenized = [_tokenize(d) for d in documents]
    doc_lens = [len(toks) for toks in tokenized]
    if not any(doc_lens):
        return [0.0] * n_docs
    avgdl = sum(doc_lens) / n_docs or 1.0
    df = {term: 0 for term in query_terms}
    for toks in tokenized:
        for term in set(toks) & query_terms:
            df[term] += 1
    idf = {term: math.log((n_docs - df[term] + 0.5) / (df[term] + 0.5) + 1) for term in query_terms}
    scores: list[float] = []
    for toks, dl in zip(tokenized, doc_lens):
        if dl == 0:
            scores.append(0.0)
            continue
        tf: dict[str, int] = {}
        for tok in toks:
            if tok in query_terms:
                tf[tok] = tf.get(tok, 0) + 1
        score = 0.0
        for term, freq in tf.items():
            num = freq * (k1 + 1)
            den = freq + k1 * (1 - b + b * dl / avgdl)
            score += idf[term] * num / den
        scores.append(score)
    return scores


def excerpt(text: str, query: str = "", max_chars: int = 240) -> str:
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= max_chars:
        return clean
    idx = -1
    low = clean.lower()
    for tok in _tokenize(query):
        idx = low.find(tok)
        if idx >= 0:
            break
    if idx < 0:
        return clean[: max_chars - 1] + "…"
    start = max(0, idx - max_chars // 3)
    end = min(len(clean), start + max_chars)
    out = clean[start:end]
    if start:
        out = "…" + out
    if end < len(clean):
        out += "…"
    return out


def _ensure_lazy_local_memory_deps() -> None:
    """Install local deep-memory backend deps via Hermes's lazy-deps allowlist."""
    try:
        from tools.lazy_deps import ensure
        ensure("memory.local", prompt=False)
    except Exception as exc:
        raise DeepMemoryUnavailableError(
            "local deep memory requires the memory.local optional backend dependencies"
        ) from exc


def _import_chromadb():
    try:
        import chromadb  # type: ignore
        return chromadb
    except Exception as first:
        # Lazy-install the declared memory.local backend; if that genuinely
        # can't be satisfied it raises DeepMemoryUnavailableError, which is the
        # correct "backend unavailable" signal to propagate.
        _ensure_lazy_local_memory_deps()
        # Only clear a partially-initialized chromadb (and the opentelemetry it
        # may have pulled in) so the retry imports cleanly after install. If
        # chromadb was never imported (clean ModuleNotFound), leave sys.modules
        # alone -- purging unrelated modules globally risks breaking other
        # in-process state.
        if any(m == "chromadb" or m.startswith("chromadb.") for m in list(sys.modules)):
            for mod_name in list(sys.modules):
                if mod_name.split(".")[0] in {"chromadb", "opentelemetry"}:
                    sys.modules.pop(mod_name, None)
        try:
            import chromadb  # type: ignore
            return chromadb
        except Exception as second:
            raise DeepMemoryUnavailableError(
                "local deep memory requires chromadb (MemPalace-compatible storage backend); "
                f"initial import error: {first}"
            ) from second


def chroma_backend_available() -> bool:
    """Cheap probe: can the ChromaDB storage backend be imported?

    Uses ``importlib.util.find_spec`` (which does not execute the module) so a
    missing backend degrades the provider to a clean no-op instead of crashing
    agent init or spamming warnings. Intentionally does not trigger a lazy
    install — it only reports whether the declared ``memory.local`` backend is
    already importable.
    """
    import importlib.util

    try:
        return importlib.util.find_spec("chromadb") is not None
    except Exception:
        return False


def _resolve_embedding_providers(device: str) -> tuple[list[str], str]:
    device = (device or "auto").strip().lower()
    provider_map = {
        "cpu": ["CPUExecutionProvider"],
        "cuda": ["CUDAExecutionProvider", "CPUExecutionProvider"],
        "coreml": ["CoreMLExecutionProvider", "CPUExecutionProvider"],
        "dml": ["DmlExecutionProvider", "CPUExecutionProvider"],
    }
    auto_order = [("CUDAExecutionProvider", "cuda"), ("CoreMLExecutionProvider", "coreml"), ("DmlExecutionProvider", "dml")]
    try:
        import onnxruntime as ort  # type: ignore
    except Exception:
        _ensure_lazy_local_memory_deps()
        import onnxruntime as ort  # type: ignore
    available = set(ort.get_available_providers())
    if device == "auto":
        for provider, name in auto_order:
            if provider in available:
                return [provider, "CPUExecutionProvider"], name
        return ["CPUExecutionProvider"], "cpu"
    requested = provider_map.get(device)
    if requested is None:
        if device not in _WARNED_DEVICES:
            logger.warning("Unknown deep_memory.embedding_device %r — falling back to cpu", device)
            _WARNED_DEVICES.add(device)
        return ["CPUExecutionProvider"], "cpu"
    preferred = requested[0]
    if preferred != "CPUExecutionProvider" and preferred not in available:
        if device not in _WARNED_DEVICES:
            logger.warning("embedding_device=%r requested but %s is unavailable — falling back to CPU", device, preferred)
            _WARNED_DEVICES.add(device)
        return ["CPUExecutionProvider"], "cpu"
    return requested, device


def get_embedding_function(device: str = "auto"):
    """Return MemPalace-compatible ONNX MiniLM embedding function."""
    _import_chromadb()
    providers, effective = _resolve_embedding_providers(device)
    cache_key = tuple(providers)
    if cache_key in _EF_CACHE:
        return _EF_CACHE[cache_key]
    from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2  # type: ignore

    class _HermesDeepMemoryONNX(ONNXMiniLM_L6_V2):
        @staticmethod
        def name() -> str:
            # Match MemPalace's spoofing for compatibility with Chroma's EF identity.
            return "default"

    ef = _HermesDeepMemoryONNX(preferred_providers=providers)
    _EF_CACHE[cache_key] = ef
    logger.info("Deep-memory embedding function initialized (device=%s providers=%s)", effective, providers)
    return ef


def _sanitize_chroma_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            out[str(key)] = value
        else:
            # Chroma metadata is scalar-only; preserve non-scalars in the JSON
            # sidecar but do not expose them as first-class filter fields.
            continue
    return out


def _record_metadata(
    *,
    metadata: dict[str, Any],
    text_hash_: str,
    source: str,
    source_uri: str,
    timestamp: Optional[float],
    created_at: float,
    updated_at: float,
    record_kind: str,
    parent_id: Optional[str],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    flat = dict(metadata or {})
    full = {
        "metadata": flat,
        "provenance": provenance or {},
        "source": source or "unknown",
        "source_uri": source_uri or "",
        "timestamp": timestamp,
        "created_at": created_at,
        "updated_at": updated_at,
        "record_kind": record_kind or "verbatim",
        "parent_id": parent_id,
        "text_hash": text_hash_,
    }
    chroma_md = _sanitize_chroma_metadata(flat)
    chroma_md.update(
        {
            "__text_hash": text_hash_,
            "__source": source or "unknown",
            "__source_uri": source_uri or "",
            "__created_at": float(created_at),
            "__updated_at": float(updated_at),
            "__record_kind": record_kind or "verbatim",
            "__deep_memory_json": _json_dumps(full),
        }
    )
    if timestamp is not None:
        chroma_md["__timestamp"] = float(timestamp)
    if parent_id:
        chroma_md["__parent_id"] = parent_id
    return chroma_md


def _record_from_chroma(record_id: str, document: str, metadata: Optional[dict[str, Any]]) -> MemoryRecord:
    meta = metadata or {}
    full = _json_loads(meta.get("__deep_memory_json"), {})
    user_md = full.get("metadata") if isinstance(full, dict) else None
    if not isinstance(user_md, dict):
        user_md = {k: v for k, v in meta.items() if not k.startswith("__")}
    return MemoryRecord(
        id=record_id,
        text=document or "",
        text_hash=str(full.get("text_hash") or meta.get("__text_hash") or text_hash(document or "")),
        metadata=user_md,
        source=str(full.get("source") or meta.get("__source") or "unknown"),
        source_uri=str(full.get("source_uri") or meta.get("__source_uri") or ""),
        timestamp=full.get("timestamp", meta.get("__timestamp")),
        created_at=float(full.get("created_at") or meta.get("__created_at") or 0.0),
        updated_at=float(full.get("updated_at") or meta.get("__updated_at") or 0.0),
        record_kind=str(full.get("record_kind") or meta.get("__record_kind") or "verbatim"),
        parent_id=full.get("parent_id") or meta.get("__parent_id"),
        provenance=(full.get("provenance") if isinstance(full.get("provenance"), dict) else {}) or {},
        summary=str(full.get("summary") or ""),
        summary_status=str(full.get("summary_status") or meta.get("__summary_status") or ""),
        summary_model=str(full.get("summary_model") or ""),
        summary_generated_at=full.get("summary_generated_at"),
        summary_version=str(full.get("summary_version") or ""),
    )


def _first(result: Any, key: str) -> list:
    val = getattr(result, key, None) if not isinstance(result, dict) else result.get(key)
    if not val:
        return []
    return val[0] or []


def _all(result: Any, key: str) -> list:
    val = getattr(result, key, None) if not isinstance(result, dict) else result.get(key)
    return val or []


def _validate_where(where: Optional[dict[str, Any]]) -> None:
    if not where:
        return
    stack = [where]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key.startswith("$") and key not in _SUPPORTED_OPERATORS:
                raise UnsupportedFilterError(f"metadata filter operator {key!r} is not supported")
            if isinstance(value, dict):
                stack.append(value)
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, dict))


def _normalize_filters(filters: Optional[dict[str, Any]]) -> dict[str, Any]:
    if not filters:
        return {}
    _validate_where(filters)
    if len(filters) <= 1:
        return dict(filters)
    # Chroma requires a single top-level operator/key. Plain multi-key equality
    # filters become an explicit $and, matching MemPalace's wing+room helper.
    return {"$and": [{key: value} for key, value in filters.items()]}


def _extract_record_ids_from_signal(signal_doc: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for match in _SIGNAL_REF_RE.findall(signal_doc or ""):
        for rid in match.split(","):
            rid = rid.strip()
            if rid and rid not in seen:
                seen.add(rid)
                out.append(rid)
    return out


def _candidate_entity_words(text: str) -> list[str]:
    return re.findall(r"\b[A-Z][\w'’-]{2,}\b", text or "")


def build_extracted_signal_lines(source_uri: str, record_ids: list[str], content: str) -> list[str]:
    """Flat replacement for MemPalace closet pointer lines."""
    pointer = ",".join(record_ids[:3])
    window = (content or "")[:5000]
    stop = {"The", "This", "That", "These", "Those", "When", "Where", "What", "Why", "Who", "Which", "How", "User", "Assistant", "System", "Tool"}
    freqs: dict[str, int] = {}
    for word in _candidate_entity_words(window):
        if word not in stop:
            freqs[word] = freqs.get(word, 0) + 1
    entities = sorted([w for w, c in freqs.items() if c >= 2], key=lambda w: -freqs[w])[:5]
    entity_str = ";".join(entities)
    topics: list[str] = []
    for pattern in [r"(?:built|fixed|wrote|added|pushed|tested|created|decided|migrated|reviewed|deployed|configured|removed|updated|remembered|discussed)\s+[\w\s]{3,40}"]:
        topics.extend(re.findall(pattern, window, re.IGNORECASE))
    for header in re.findall(r"^#{1,3}\s+(.{5,60})$", window, re.MULTILINE):
        topics.append(header.strip())
    topics = list(dict.fromkeys(t.strip().lower() for t in topics if t.strip()))[:12]
    quotes = re.findall(r'"([^"]{15,150})"', window)
    lines = [f"{topic}|{entity_str}|→{pointer}" for topic in topics]
    lines.extend(f'"{quote}"|{entity_str}|→{pointer}' for quote in quotes[:3])
    if not lines:
        name = Path(source_uri or "memory-record").stem[:40]
        lines.append(f"{name}|{entity_str}|→{pointer}")
    return lines


class LocalMemoryStore:
    """Flat memory-record store backed by ChromaDB + ONNX embeddings."""

    def __init__(
        self,
        root: Path,
        *,
        embedding_provider: str = "onnx-minilm",
        embedding_model: str = "all-MiniLM-L6-v2",
        embedding_device: str = "auto",
        records_collection: str = "hermes_memory_records",
        signals_collection: str = "hermes_memory_signals",
        ingest_collection: str = "hermes_memory_ingest",
    ):
        if str(embedding_provider or "").lower() in {"hashing", "hash", "none"}:
            raise DeepMemoryUnavailableError("hashing embeddings are not supported for local deep memory")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "chroma.sqlite3"
        self.embedding_provider = "onnx-minilm"
        self.embedding_model = embedding_model or "all-MiniLM-L6-v2"
        self.embedding_device = embedding_device or "auto"
        self.records_collection_name = records_collection
        self.signals_collection_name = signals_collection
        self.ingest_collection_name = ingest_collection
        self._lock = threading.RLock()
        chromadb = _import_chromadb()
        self.embedding_function = get_embedding_function(self.embedding_device)
        self.client = chromadb.PersistentClient(path=str(self.root))
        self.records = self.client.get_or_create_collection(
            name=self.records_collection_name,
            embedding_function=self.embedding_function,
            metadata=dict(_COLLECTION_METADATA),
        )
        self.signals = self.client.get_or_create_collection(
            name=self.signals_collection_name,
            embedding_function=self.embedding_function,
            metadata=dict(_COLLECTION_METADATA),
        )
        # Ingest-event dedup markers live here, fully isolated from the
        # searchable record/signal collections so import bookkeeping never
        # leaks into recall, prefetch hints, rank boosts, or record counts.
        self.ingest = self.client.get_or_create_collection(
            name=self.ingest_collection_name,
            embedding_function=self.embedding_function,
            metadata=dict(_COLLECTION_METADATA),
        )

    def close(self) -> None:
        # Chroma PersistentClient has no explicit close API, but it does keep
        # process-global system caches that retain Rust segment readers. Clear
        # those caches for short-lived tests/tools so macOS's low default fd
        # limit is not exhausted across many temporary stores.
        try:
            from chromadb.api.client import SharedSystemClient  # type: ignore

            SharedSystemClient.clear_system_cache()
        except Exception:
            logger.debug("failed to clear Chroma system cache during close", exc_info=True)

    def upsert_record(
        self,
        *,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        source: str = "unknown",
        source_uri: str = "",
        timestamp: Optional[float] = None,
        record_kind: str = "verbatim",
        parent_id: Optional[str] = None,
        provenance: Optional[Dict[str, Any]] = None,
        record_id: Optional[str] = None,
    ) -> MemoryRecord:
        text = sanitize_text(text)
        if not text.strip():
            raise ValueError("memory text is empty")
        metadata = dict(metadata or {})
        provenance = dict(provenance or {})
        th = text_hash(text)
        rid = record_id or stable_record_id(source_uri, record_kind, parent_id or "", th)
        now = _now()
        existing = self.get_record(rid)
        created = existing.created_at if existing else now
        chroma_md = _record_metadata(
            metadata=metadata,
            text_hash_=th,
            source=source,
            source_uri=source_uri,
            timestamp=timestamp,
            created_at=created,
            updated_at=now,
            record_kind=record_kind,
            parent_id=parent_id,
            provenance=provenance,
        )
        with self._lock:
            self.records.upsert(ids=[rid], documents=[text], metadatas=[chroma_md])
            self._upsert_extracted_signals(rid, text, source_uri, metadata)
        return self.get_record(rid)  # type: ignore[return-value]

    def _upsert_extracted_signals(self, record_id: str, text: str, source_uri: str, metadata: dict[str, Any]) -> None:
        signal_id_base = stable_record_id("signals", record_id)
        try:
            self.signals.delete(where={"record_id": record_id})
        except Exception:
            logger.debug("signal purge failed for %s", record_id, exc_info=True)
        lines = build_extracted_signal_lines(source_uri, [record_id], text)
        docs: list[str] = []
        ids: list[str] = []
        metas: list[dict[str, Any]] = []
        current: list[str] = []
        current_chars = 0
        part = 1
        scalar_md = _sanitize_chroma_metadata(metadata)
        for line in lines:
            extra = len(line) + (1 if current else 0)
            if current and current_chars + extra > 1500:
                ids.append(f"{signal_id_base}_{part:02d}")
                docs.append("\n".join(current))
                metas.append({"record_id": record_id, "source_uri": source_uri or "", **scalar_md})
                part += 1
                current = []
                current_chars = 0
            current.append(line)
            current_chars += extra
        if current:
            ids.append(f"{signal_id_base}_{part:02d}")
            docs.append("\n".join(current))
            metas.append({"record_id": record_id, "source_uri": source_uri or "", **scalar_md})
        if ids:
            self.signals.upsert(ids=ids, documents=docs, metadatas=metas)

    def update_record_summary(
        self,
        record_id: str,
        summary: str,
        *,
        status: str = "ready",
        model: str = "",
        version: str = "v1",
    ) -> Optional[MemoryRecord]:
        """Attach a compact summary to a record as metadata only.

        Touches neither the stored document (verbatim text / retrieval text),
        the record ID, nor the text hash, so search/embedding behaviour is
        unchanged -- the summary is purely a display/context aid.
        """
        summary = sanitize_text(summary)
        with self._lock:
            try:
                got = self.records.get(ids=[record_id], include=["metadatas"])
            except Exception:
                logger.debug("summary update fetch failed for %s", record_id, exc_info=True)
                return None
            ids = _all(got, "ids")
            if not ids:
                return None
            metas = _all(got, "metadatas")
            meta = dict(metas[0] or {}) if metas else {}
            full = _json_loads(meta.get("__deep_memory_json"), {})
            if not isinstance(full, dict):
                full = {}
            full["summary"] = summary
            full["summary_status"] = status or ""
            full["summary_model"] = model or ""
            full["summary_version"] = version or ""
            full["summary_generated_at"] = _now()
            meta["__deep_memory_json"] = _json_dumps(full)
            meta["__summary_status"] = status or ""
            # Metadata-only update: omit documents so Chroma keeps the existing
            # verbatim document and its embedding untouched.
            self.records.update(ids=[record_id], metadatas=[meta])
        return self.get_record(record_id)

    def mark_ingested(self, event_id: str, record_id: str, source_uri: str = "") -> bool:
        # Dedup bookkeeping only: written to the isolated ``ingest`` collection
        # with a constant embedding (no ONNX inference) so it never appears in
        # search/prefetch and never inflates the searchable record count.
        marker_id = stable_record_id("ingest_event", event_id)
        if self.is_ingested(event_id):
            return False
        with self._lock:
            self.ingest.upsert(
                ids=[marker_id],
                documents=[f"ingest event {event_id} -> {record_id}"],
                embeddings=[list(_MARKER_EMBEDDING)],
                metadatas=[{"event_id": event_id, "record_id": record_id, "source_uri": source_uri or ""}],
            )
        return True

    def is_ingested(self, event_id: str) -> bool:
        marker_id = stable_record_id("ingest_event", event_id)
        with self._lock:
            try:
                got = self.ingest.get(ids=[marker_id])
            except Exception:
                logger.debug("ingest marker fetch failed for %s", event_id, exc_info=True)
                return False
        return bool(_all(got, "ids"))

    def get_record(self, record_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            try:
                got = self.records.get(ids=[record_id], include=["documents", "metadatas"])
            except Exception:
                logger.debug("record fetch failed for %s", record_id, exc_info=True)
                return None
        ids = _all(got, "ids")
        if not ids:
            return None
        docs = _all(got, "documents")
        metas = _all(got, "metadatas")
        return _record_from_chroma(str(ids[0]), docs[0] if docs else "", metas[0] if metas else {})

    def get_many(self, ids: Iterable[str]) -> List[MemoryRecord]:
        ids_list = [str(rid) for rid in ids if rid]
        if not ids_list:
            return []
        with self._lock:
            got = self.records.get(ids=ids_list, include=["documents", "metadatas"])
        out: list[MemoryRecord] = []
        for rid, doc, meta in zip(_all(got, "ids"), _all(got, "documents"), _all(got, "metadatas")):
            out.append(_record_from_chroma(str(rid), doc or "", meta or {}))
        order = {rid: i for i, rid in enumerate(ids_list)}
        out.sort(key=lambda r: order.get(r.id, len(order)))
        return out

    def _bm25_union_candidates(self, query: str, where: dict[str, Any], n_results: int) -> list[dict[str, Any]]:
        try:
            kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
            if where:
                kwargs["where"] = where
            got = self.records.get(**kwargs)
        except Exception:
            logger.debug("BM25 union candidate fetch failed", exc_info=True)
            return []
        ids = [str(x) for x in _all(got, "ids")]
        docs = [d or "" for d in _all(got, "documents")]
        metas = [m or {} for m in _all(got, "metadatas")]
        bm25_raw = _bm25_scores(query, docs)
        max_bm25 = max(bm25_raw) if bm25_raw else 0.0
        rows = []
        for rid, doc, meta, raw in zip(ids, docs, metas, bm25_raw):
            rows.append({"id": rid, "text": doc, "metadata": meta, "distance": None, "bm25_score": round(raw, 3), "_bm25_norm": (raw / max_bm25) if max_bm25 > 0 else 0.0, "matched_via": "bm25"})
        rows.sort(key=lambda r: r["_bm25_norm"], reverse=True)
        return rows[: max(1, n_results)]

    def _signal_boosts(self, query: str, where: dict[str, Any], n_results: int) -> dict[str, float]:
        boosts: dict[str, float] = {}
        try:
            try:
                signal_count = int(self.signals.count())
            except Exception:
                signal_count = n_results
            kwargs = {"query_texts": [query], "n_results": max(1, min(n_results, signal_count or 1)), "include": ["documents", "metadatas", "distances"]}
            # Signals share user metadata, so flat filters can apply there too.
            if where:
                kwargs["where"] = where
            results = self.signals.query(**kwargs)
        except Exception:
            logger.debug("extracted signal query failed; using record-only search", exc_info=True)
            return boosts
        rank_boosts = [0.40, 0.25, 0.15, 0.08, 0.04]
        for rank, (doc, dist) in enumerate(zip(_first(results, "documents"), _first(results, "distances"))):
            if rank >= len(rank_boosts):
                break
            try:
                distance = float(dist)
            except Exception:
                continue
            if distance > 1.5:
                continue
            for rid in _extract_record_ids_from_signal(doc or ""):
                boosts.setdefault(rid, rank_boosts[rank])
        return boosts

    def search(
        self,
        query: str,
        *,
        filters: Optional[Dict[str, Any]] = None,
        limit: int = 5,
        max_distance: float = 0.0,
        candidate_strategy: str = "union",
    ) -> List[SearchResult]:
        limit = max(1, min(int(limit or 5), 100))
        if candidate_strategy not in ("vector", "union"):
            raise ValueError("candidate_strategy must be 'vector' or 'union'")
        where = _normalize_filters(filters)
        with self._lock:
            if not query.strip():
                kwargs: dict[str, Any] = {"include": ["documents", "metadatas"]}
                if where:
                    kwargs["where"] = where
                got = self.records.get(**kwargs)
                rows = [
                    {"id": str(rid), "text": doc or "", "metadata": meta or {}, "distance": None, "bm25_score": 0.0, "_score": 0.0, "matched_via": "record"}
                    for rid, doc, meta in zip(_all(got, "ids"), _all(got, "documents"), _all(got, "metadatas"))
                ]
                rows.sort(key=lambda r: float((r["metadata"] or {}).get("__timestamp") or (r["metadata"] or {}).get("__created_at") or 0), reverse=True)
            else:
                try:
                    record_count = int(self.records.count())
                except Exception:
                    record_count = limit
                n_results = max(1, min(limit * 3, record_count or 1))
                qkwargs: dict[str, Any] = {"query_texts": [query], "n_results": n_results, "include": ["documents", "metadatas", "distances"]}
                if where:
                    qkwargs["where"] = where
                results = self.records.query(**qkwargs)
                boosts = self._signal_boosts(query, where, min(limit * 2, n_results))
                rows = []
                for rid, doc, meta, dist in zip(_first(results, "ids"), _first(results, "documents"), _first(results, "metadatas"), _first(results, "distances")):
                    try:
                        raw_dist = float(dist)
                    except Exception:
                        raw_dist = None
                    if raw_dist is not None and max_distance > 0.0 and raw_dist > max_distance:
                        continue
                    boost = boosts.get(str(rid), 0.0)
                    eff_dist = max(0.0, min(2.0, raw_dist - boost)) if raw_dist is not None else None
                    rows.append({"id": str(rid), "text": doc or "", "metadata": meta or {}, "distance": raw_dist, "effective_distance": eff_dist, "closet_boost": boost, "matched_via": "record+signal" if boost else "record"})
                # Union only merges BM25-only candidates when no distance gate
                # is set: BM25 candidates carry no vector distance, so they
                # cannot satisfy max_distance and must be skipped (matches
                # MemPalace). "vector" strategy never merges them.
                if candidate_strategy == "union" and max_distance <= 0.0:
                    seen = {r["id"] for r in rows}
                    for extra in self._bm25_union_candidates(query, where, limit * 3):
                        if extra["id"] not in seen:
                            rows.append(extra)
                            seen.add(extra["id"])
        if query.strip():
            docs = [r.get("text", "") for r in rows]
            bm25_raw = _bm25_scores(query, docs)
            max_bm25 = max(bm25_raw) if bm25_raw else 0.0
            for r, raw in zip(rows, bm25_raw):
                dist = r.get("effective_distance", r.get("distance"))
                vec_sim = 0.0 if dist is None else max(0.0, 1.0 - float(dist))
                bm25_norm = raw / max_bm25 if max_bm25 > 0 else 0.0
                r["bm25_score"] = round(raw, 3)
                # MemPalace hybrid rank: 0.6 vector + 0.4 normalized BM25.
                r["_score"] = 0.6 * vec_sim + 0.4 * bm25_norm
            rows.sort(key=lambda r: r.get("_score", 0.0), reverse=True)
        out: List[SearchResult] = []
        for rank, row in enumerate(rows[:limit], start=1):
            rec = _record_from_chroma(row["id"], row.get("text") or "", row.get("metadata") or {})
            out.append(
                SearchResult(
                    id=rec.id,
                    score=round(float(row.get("_score", 0.0)), 4),
                    rank=rank,
                    excerpt=excerpt(rec.text, query),
                    text=rec.text,
                    metadata=rec.metadata,
                    source=rec.source,
                    source_uri=rec.source_uri,
                    timestamp=rec.timestamp,
                    record_kind=rec.record_kind,
                    distance=round(row["distance"], 4) if isinstance(row.get("distance"), (int, float)) else None,
                    bm25_score=float(row.get("bm25_score") or 0.0),
                    matched_via=str(row.get("matched_via") or "record"),
                    summary=rec.summary,
                    summary_status=rec.summary_status,
                    summary_model=rec.summary_model,
                    summary_generated_at=rec.summary_generated_at,
                    summary_version=rec.summary_version,
                )
            )
        return out

    def stats(self) -> Dict[str, Any]:
        try:
            records = int(self.records.count())
        except Exception:
            records = 0
        try:
            signals = int(self.signals.count())
        except Exception:
            signals = 0
        try:
            ingest_markers = int(self.ingest.count())
        except Exception:
            ingest_markers = 0
        return {
            "records": records,
            "signals": signals,
            "ingest_markers": ingest_markers,
            "db_path": str(self.db_path),
            "storage_backend": "chromadb",
            "embedding_provider": self.embedding_provider,
            "embedding_model": self.embedding_model,
        }
