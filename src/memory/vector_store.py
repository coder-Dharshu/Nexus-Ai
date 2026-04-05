"""
Nexus AI — Vector Memory.

FAISS-backed persistent vector store using BAAI/bge-m3 embeddings.

Used for:
  - User preference memory (preferred sources, past query results)
  - Email history retrieval for tone matching (Drafter)
  - Agent knowledge base (facts from past verified answers)

Persists to disk at settings.faiss_index_path.
Thread-safe reads, serialized writes.
"""
from __future__ import annotations

import asyncio
import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class MemoryEntry:
    id: str
    text: str
    source: str
    metadata: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    embedding: Optional[list[float]] = field(default=None, repr=False)


class VectorMemory:
    """
    Async-friendly FAISS vector store.
    Embeddings are generated lazily with bge-m3.
    Falls back to keyword search if FAISS/transformers not installed.
    """

    def __init__(self, index_path: Optional[Path] = None) -> None:
        from config.settings import get_settings
        self._index_path = index_path or get_settings().faiss_index_path
        self._entries: list[MemoryEntry] = []
        self._index = None          # FAISS index
        self._model = None          # sentence-transformers model
        self._ready = False
        self._lock = asyncio.Lock()

    # ── Initialisation ────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        """Load or create FAISS index. Falls back gracefully if deps missing."""
        async with self._lock:
            if self._ready:
                return
            try:
                self._model = await asyncio.to_thread(self._load_model)
                self._load_or_create_index()
                self._ready = True
                log.info("vector_memory_ready", entries=len(self._entries))
            except ImportError as exc:
                log.warning("vector_memory_fallback", reason=str(exc))
                self._ready = False

    @staticmethod
    def _load_model():
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("BAAI/bge-m3")

    def _load_or_create_index(self) -> None:
        import faiss
        import numpy as np

        meta_path = self._index_path.with_suffix(".meta.pkl")
        if self._index_path.exists() and meta_path.exists():
            self._index = faiss.read_index(str(self._index_path))
            with open(meta_path, "rb") as f:
                self._entries = pickle.load(f)
            log.info("faiss_index_loaded", path=str(self._index_path), entries=len(self._entries))
        else:
            # Create empty L2 index (1024 dims for bge-m3)
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            self._index = faiss.IndexFlatL2(1024)
            self._entries = []
            log.info("faiss_index_created")

    # ── Add documents ─────────────────────────────────────────────────────────

    async def add(self, text: str, source: str, metadata: Optional[dict] = None) -> str:
        """Add a document to the vector store. Returns entry ID."""
        import uuid
        entry_id = str(uuid.uuid4())

        async with self._lock:
            if not self._ready:
                await self.initialize()

            embedding = await asyncio.to_thread(
                self._embed, [text]
            )

            entry = MemoryEntry(
                id=entry_id,
                text=text,
                source=source,
                metadata=metadata or {},
                embedding=embedding[0].tolist(),
            )
            self._entries.append(entry)

            if self._index is not None:
                import numpy as np
                self._index.add(np.array([embedding[0]], dtype="float32"))

            await asyncio.to_thread(self._persist)

        log.debug("memory_entry_added", id=entry_id, source=source)
        return entry_id

    # ── Search ────────────────────────────────────────────────────────────────

    async def search(self, query: str, k: int = 5) -> list[dict]:
        """
        Semantic search. Falls back to keyword search if FAISS not ready.
        Returns list of {id, text, source, score, metadata} dicts.
        """
        if not self._ready:
            await self.initialize()

        if not self._entries:
            return []

        if self._index is not None and self._ready:
            return await asyncio.to_thread(self._faiss_search, query, k)
        else:
            return self._keyword_search(query, k)

    def _faiss_search(self, query: str, k: int) -> list[dict]:
        import numpy as np
        q_emb = self._embed([query])
        actual_k = min(k, len(self._entries))
        distances, indices = self._index.search(
            np.array([q_emb[0]], dtype="float32"), actual_k
        )
        results = []
        for dist, idx in zip(distances[0], indices[0]):
            if idx < 0 or idx >= len(self._entries):
                continue
            entry = self._entries[idx]
            results.append({
                "id": entry.id,
                "text": entry.text,
                "source": entry.source,
                "score": float(1.0 / (1.0 + dist)),  # normalise to 0-1
                "metadata": entry.metadata,
            })
        return results

    def _keyword_search(self, query: str, k: int) -> list[dict]:
        """Simple keyword fallback when FAISS unavailable."""
        query_words = set(query.lower().split())
        scored = []
        for entry in self._entries:
            words = set(entry.text.lower().split())
            overlap = len(query_words & words) / max(len(query_words), 1)
            scored.append((overlap, entry))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [
            {"id": e.id, "text": e.text, "source": e.source, "score": s, "metadata": e.metadata}
            for s, e in scored[:k]
            if s > 0
        ]

    # ── Persistence ───────────────────────────────────────────────────────────

    def _persist(self) -> None:
        if self._index is None:
            return
        import faiss
        faiss.write_index(self._index, str(self._index_path))
        meta_path = self._index_path.with_suffix(".meta.pkl")
        with open(meta_path, "wb") as f:
            pickle.dump(self._entries, f)

    def _embed(self, texts: list[str]):
        if self._model is None:
            raise RuntimeError("Model not loaded")
        return self._model.encode(texts, normalize_embeddings=True)

    # ── Convergence check ─────────────────────────────────────────────────────

    async def similarity(self, text_a: str, text_b: str) -> float:
        """
        Compute cosine similarity between two texts using bge-m3.
        Used by the meeting room to detect convergence (threshold 0.92).
        Falls back to word-overlap if model unavailable.
        """
        if self._model is not None and self._ready:
            import numpy as np
            embs = await asyncio.to_thread(self._embed, [text_a, text_b])
            a, b = embs[0], embs[1]
            return float(np.dot(a, b))  # already normalised
        # Fallback: Jaccard similarity
        wa = set(text_a.lower().split())
        wb = set(text_b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def __len__(self) -> int:
        return len(self._entries)


# Module-level singleton
vector_memory = VectorMemory()
