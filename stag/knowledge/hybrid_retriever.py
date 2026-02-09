"""Hybrid retriever combining vector, graph, and sparse search.

Implements Reciprocal Rank Fusion (RRF) to merge results from
all three retrieval methods into a single ranked list.
"""

from __future__ import annotations

import time

import structlog

from stag.knowledge.vector_store import VectorStore
from stag.knowledge.graph_store import GraphStore
from stag.knowledge.sparse_store import SparseStore
from stag.models import StreamEvent, RetrievalResult

logger = structlog.get_logger()


class HybridRetriever:
    """Combines vector, graph, and sparse retrieval with Reciprocal Rank Fusion.

    The three retrieval paths complement each other:
    - Vector: Semantic similarity (captures meaning)
    - Graph: Relationship traversal (captures connections)
    - Sparse/BM25: Exact keyword matching (captures identifiers, names)

    Results are fused using RRF with configurable weights.
    """

    def __init__(
        self,
        vector_store: VectorStore,
        graph_store: GraphStore,
        sparse_store: SparseStore,
        vector_weight: float = 0.4,
        graph_weight: float = 0.35,
        sparse_weight: float = 0.25,
        rrf_k: int = 60,
    ) -> None:
        self.vector_store = vector_store
        self.graph_store = graph_store
        self.sparse_store = sparse_store
        self.vector_weight = vector_weight
        self.graph_weight = graph_weight
        self.sparse_weight = sparse_weight
        self.rrf_k = rrf_k

    async def initialize(self) -> None:
        """Initialize all stores."""
        await self.vector_store.initialize()
        await self.graph_store.initialize()
        logger.info("hybrid_retriever.initialized")

    async def add_event(self, event: StreamEvent) -> None:
        """Index an event across all three stores."""
        await self.vector_store.add_event(event)
        await self.graph_store.add_event(event)
        await self.sparse_store.add_event(event)

    async def search(self, query: str, top_k: int = 10) -> RetrievalResult:
        """Execute hybrid search across all three stores and fuse results."""
        start = time.time()

        # Parallel retrieval from all stores
        vector_results = await self.vector_store.search(query, top_k=top_k)
        graph_results = await self.graph_store.search(query, top_k=top_k)
        sparse_results = await self.sparse_store.search(query, top_k=top_k)

        # Reciprocal Rank Fusion
        fused = self._rrf_fuse(vector_results, graph_results, sparse_results, top_k)

        elapsed = (time.time() - start) * 1000

        logger.info(
            "hybrid_retriever.search",
            query=query[:100],
            vector_hits=len(vector_results),
            graph_hits=len(graph_results),
            sparse_hits=len(sparse_results),
            fused_hits=len(fused),
            time_ms=round(elapsed, 1),
        )

        return RetrievalResult(
            query=query,
            vector_results=vector_results,
            graph_results=graph_results,
            sparse_results=sparse_results,
            fused_results=fused,
            retrieval_time_ms=elapsed,
        )

    def _rrf_fuse(
        self,
        vector_results: list[dict],
        graph_results: list[dict],
        sparse_results: list[dict],
        top_k: int,
    ) -> list[dict]:
        """Reciprocal Rank Fusion across three result lists.

        RRF score = sum( weight / (k + rank) ) for each result across sources.
        """
        scores: dict[str, float] = {}
        texts: dict[str, str] = {}
        metadata_map: dict[str, dict] = {}
        sources_map: dict[str, list[str]] = {}

        def _process(results: list[dict], weight: float, source_name: str) -> None:
            for rank, item in enumerate(results):
                item_id = item.get("id", item.get("text", "")[:50])
                rrf_score = weight / (self.rrf_k + rank + 1)
                scores[item_id] = scores.get(item_id, 0) + rrf_score
                texts[item_id] = item.get("text", "")
                metadata_map[item_id] = item.get("metadata", {})
                if item_id not in sources_map:
                    sources_map[item_id] = []
                sources_map[item_id].append(source_name)

        _process(vector_results, self.vector_weight, "vector")
        _process(graph_results, self.graph_weight, "graph")
        _process(sparse_results, self.sparse_weight, "sparse")

        # Sort by fused score
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]

        max_score = ranked[0][1] if ranked else 1.0
        return [
            {
                "id": item_id,
                "text": texts.get(item_id, ""),
                "metadata": metadata_map.get(item_id, {}),
                "score": score / max(max_score, 1e-6),
                "sources": sources_map.get(item_id, []),
            }
            for item_id, score in ranked
        ]

    @property
    def stats(self) -> dict:
        return {
            "vector_count": self.vector_store.count,
            "graph": self.graph_store.stats,
            "sparse_count": self.sparse_store.count,
            "weights": {
                "vector": self.vector_weight,
                "graph": self.graph_weight,
                "sparse": self.sparse_weight,
            },
        }
