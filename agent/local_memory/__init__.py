"""Flat local deep-memory storage for Hermes.

This package keeps MemPalace's storage/retrieval shape -- a ChromaDB persistent
store, ONNX all-MiniLM-L6-v2 semantic embeddings, cosine HNSW, over-fetch plus
hybrid BM25 reranking, and a secondary extracted-signal collection used only as
a rank boost -- but exposes a flat, verbatim "memory record" API with no palace
ontology (no wings, rooms, tunnels, graph traversal, or taxonomy).

In the AgentOps runtime fork this local provider is the default deep-memory
backend for local-compatible profiles. Tenant-scoped selection and fail-closed
behaviour for AgentOps/remote profiles are layered on top via
``agent.runtime_backends`` (the ``DEEP_MEMORY`` capability) -- this package
stays a single-store, dependency-optional building block.
"""

from .store import LocalMemoryStore, MemoryRecord, SearchResult
from .provider import LocalDeepMemoryProvider

__all__ = ["LocalMemoryStore", "MemoryRecord", "SearchResult", "LocalDeepMemoryProvider"]
