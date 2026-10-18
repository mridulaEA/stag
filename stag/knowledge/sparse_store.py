"""Sparse retrieval using BM25 for keyword-based search."""

from __future__ import annotations

import re
from collections import OrderedDict

import structlog
from rank_bm25 import BM25Okapi

from stag.models import StreamEvent

logger = structlog.get_logger()


class SparseStore:
    """BM25-based sparse retrieval over stream events.

    Complements vector search by handling exact keyword matches,
    acronyms, tool names, and policy identifiers that semantic
    embeddings may miss.
    """

    def __init__(self, max_documents: int = 10000) -> None:
        self.max_documents = max_documents
        self._documents: OrderedDict[str, dict] = OrderedDict()
        self._bm25: BM25Okapi | None = None
        self._dirty = True

    def _tokenize(self, text: str) -> list[str]:
        """Simple tokenization: lowercase, split on non-alphanumeric."""
        return re.findall(r"[a-z0-9]+", text.lower())

    def _rebuild_index(self) -> None:
        """Rebuild BM25 index from current documents."""
        if not self._documents:
            self._bm25 = None
            return
        corpus = [self._tokenize(doc["text"]) for doc in self._documents.values()]
        self._bm25 = BM25Okapi(corpus)
        self._dirty = False

    async def add_event(self, event: StreamEvent) -> None:
        """Add a stream event to the BM25 index."""
        self._documents[event.doc_key] = {
            "id": event.doc_key,
            "event_id": event.id,
            "text": event.text,
            "metadata": {
                "source": event.source,
                "event_type": event.event_type.value,
                "severity": event.severity.value,
                "timestamp": event.timestamp.isoformat(),
                **{k: str(v) for k, v in event.payload.items() if isinstance(v, (str, int, float, bool))},
            },
        }
        self._dirty = True

        # Evict oldest if over capacity
        while len(self._documents) > self.max_documents:
            self._documents.popitem(last=False)

        logger.debug("sparse_store.indexed", event_id=event.id, total=len(self._documents))

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """BM25 keyword search over indexed events."""
        if not self._documents:
            return []

        if self._dirty:
            self._rebuild_index()

        if not self._bm25:
            return []

        tokens = self._tokenize(query)
        if not tokens:
            return []

        scores = self._bm25.get_scores(tokens)
        doc_ids = list(self._documents.keys())

        # Get top-k by score
        scored = sorted(zip(doc_ids, scores, strict=True), key=lambda x: x[1], reverse=True)[:top_k]

        if not scored:
            return []

        max_score = max(s for _, s in scored)
        min_score = min(s for _, s in scored)

        results = []
        for doc_id, score in scored:
            # Skip only if zero relevance (all negative scores still indicate some match in small corpora)
            if max_score > 0 and score <= 0:
                continue
            doc = self._documents[doc_id]
            # Normalize: shift to 0+ range if needed, then scale to 0-1
            if max_score == min_score:
                norm_score = 1.0 if max_score != 0 else 0.0
            elif min_score < 0:
                norm_score = (score - min_score) / max(max_score - min_score, 1e-6)
            else:
                norm_score = score / max(max_score, 1e-6)
            results.append({
                "id": doc_id,
                "text": doc["text"],
                "metadata": doc["metadata"],
                "score": norm_score,
                "source": "sparse",
            })

        return results

    @property
    def count(self) -> int:
        return len(self._documents)
