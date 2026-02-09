"""Vector store using sentence-transformers for semantic similarity search.

Uses in-memory numpy-based cosine similarity with sentence-transformer embeddings.
For production, swap in Qdrant, Weaviate, or ChromaDB.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

import numpy as np
import structlog

from stag.models import StreamEvent

logger = structlog.get_logger()


class VectorStore:
    """Sentence-transformer vector store for semantic search over stream events.

    Embeds events using sentence-transformers and performs cosine similarity
    search in-memory with numpy. Lightweight, no external DB required.
    """

    def __init__(
        self,
        persist_dir: str = "./data/vectors",
        embedding_model: str = "all-MiniLM-L6-v2",
        max_documents: int = 10000,
    ) -> None:
        self.persist_dir = persist_dir
        self.embedding_model_name = embedding_model
        self.max_documents = max_documents
        self._model = None
        self._documents: OrderedDict[str, dict] = OrderedDict()
        self._embeddings: dict[str, np.ndarray] = {}

    async def initialize(self) -> None:
        """Load the sentence-transformer model."""
        from sentence_transformers import SentenceTransformer

        Path(self.persist_dir).mkdir(parents=True, exist_ok=True)
        self._model = SentenceTransformer(self.embedding_model_name)

        # Load persisted data if available
        index_path = Path(self.persist_dir) / "index.json"
        embeddings_path = Path(self.persist_dir) / "embeddings.npz"
        if index_path.exists() and embeddings_path.exists():
            try:
                docs = json.loads(index_path.read_text())
                npz = np.load(str(embeddings_path), allow_pickle=False)
                for doc_id, doc in docs.items():
                    self._documents[doc_id] = doc
                    if doc_id in npz:
                        self._embeddings[doc_id] = npz[doc_id]
                logger.info("vector_store.loaded", count=len(self._documents))
            except Exception as e:
                logger.warning("vector_store.load_failed", error=str(e))

        logger.info(
            "vector_store.initialized",
            model=self.embedding_model_name,
            count=len(self._documents),
        )

    def _embed(self, text: str) -> np.ndarray:
        """Embed a single text string."""
        return self._model.encode(text, normalize_embeddings=True)

    async def add_event(self, event: StreamEvent) -> None:
        """Index a stream event for semantic search."""
        if not self._model:
            await self.initialize()

        text = event.text
        embedding = self._embed(text)

        self._documents[event.id] = {
            "id": event.id,
            "text": text,
            "metadata": {
                "source": event.source,
                "event_type": event.event_type.value,
                "severity": event.severity.value,
                "timestamp": event.timestamp.isoformat(),
                **{k: str(v) for k, v in event.payload.items() if isinstance(v, (str, int, float, bool))},
            },
        }
        self._embeddings[event.id] = embedding

        # Evict oldest if over capacity
        while len(self._documents) > self.max_documents:
            old_id, _ = self._documents.popitem(last=False)
            self._embeddings.pop(old_id, None)

        # Periodic persistence
        if len(self._documents) % 50 == 0:
            await self._persist()

        logger.debug("vector_store.indexed", event_id=event.id)

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Semantic search over indexed events using cosine similarity."""
        if not self._model or not self._embeddings:
            return []

        query_embedding = self._embed(query)

        # Compute cosine similarity against all stored embeddings
        doc_ids = list(self._embeddings.keys())
        if not doc_ids:
            return []

        matrix = np.stack([self._embeddings[did] for did in doc_ids])
        similarities = matrix @ query_embedding  # cosine sim (embeddings are normalized)

        # Get top-k indices
        k = min(top_k, len(doc_ids))
        top_indices = np.argsort(similarities)[::-1][:k]

        hits = []
        for idx in top_indices:
            doc_id = doc_ids[idx]
            score = float(similarities[idx])
            if score <= 0:
                continue
            doc = self._documents[doc_id]
            hits.append({
                "id": doc_id,
                "text": doc["text"],
                "metadata": doc["metadata"],
                "score": score,
                "source": "vector",
            })
        return hits

    async def _persist(self) -> None:
        """Save index and embeddings to disk."""
        try:
            index_path = Path(self.persist_dir) / "index.json"
            embeddings_path = Path(self.persist_dir) / "embeddings.npz"
            index_path.write_text(json.dumps(
                {k: v for k, v in self._documents.items()},
                default=str,
            ))
            np.savez_compressed(str(embeddings_path), **self._embeddings)
            logger.debug("vector_store.persisted", count=len(self._documents))
        except Exception as e:
            logger.warning("vector_store.persist_failed", error=str(e))

    @property
    def count(self) -> int:
        return len(self._documents)
