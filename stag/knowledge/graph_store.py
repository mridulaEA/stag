"""Knowledge graph store using NetworkX.

Builds and maintains a dynamic knowledge graph from stream events,
capturing relationships between users, tools, departments, data types,
and incidents.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import networkx as nx
import structlog

from stag.models import StreamEvent, GraphNode, GraphEdge

logger = structlog.get_logger()


class GraphStore:
    """NetworkX-backed knowledge graph for relationship-aware retrieval.

    Automatically extracts entities and relationships from stream events
    to build a continuously evolving knowledge graph.
    """

    def __init__(self, persist_path: str = "./data/graph.json") -> None:
        self.persist_path = persist_path
        self.graph = nx.DiGraph()
        self._event_count = 0

    async def initialize(self) -> None:
        """Load persisted graph or start fresh."""
        path = Path(self.persist_path)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                self.graph = nx.node_link_graph(data)
                logger.info("graph_store.loaded", nodes=self.graph.number_of_nodes(), edges=self.graph.number_of_edges())
            except Exception as e:
                logger.warning("graph_store.load_failed", error=str(e))
                self.graph = nx.DiGraph()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("graph_store.initialized_empty")

    async def persist(self) -> None:
        """Save graph to disk."""
        Path(self.persist_path).parent.mkdir(parents=True, exist_ok=True)
        data = nx.node_link_data(self.graph)
        Path(self.persist_path).write_text(json.dumps(data, default=str))
        logger.debug("graph_store.persisted", nodes=self.graph.number_of_nodes())

    def _ensure_node(self, node_id: str, node_type: str, **props) -> None:
        """Add or update a node."""
        if self.graph.has_node(node_id):
            self.graph.nodes[node_id].update(props)
            self.graph.nodes[node_id]["weight"] = self.graph.nodes[node_id].get("weight", 0) + 1
        else:
            self.graph.add_node(node_id, node_type=node_type, weight=1, **props)

    def _ensure_edge(self, source: str, target: str, relation: str, **props) -> None:
        """Add or strengthen an edge."""
        if self.graph.has_edge(source, target):
            self.graph[source][target]["weight"] = self.graph[source][target].get("weight", 0) + 1
            self.graph[source][target].update(props)
        else:
            self.graph.add_edge(source, target, relation=relation, weight=1, **props)

    async def add_event(self, event: StreamEvent) -> None:
        """Extract entities and relationships from a stream event and add to graph."""
        self._event_count += 1
        payload = event.payload

        # Extract entities
        user = payload.get("user")
        tool = payload.get("tool")
        department = payload.get("department")
        data_type = payload.get("data_type")
        domain = payload.get("domain") or payload.get("destination")

        # Add nodes
        if user:
            self._ensure_node(f"user:{user}", "user", label=user)
        if tool:
            sanctioned = payload.get("sanctioned", False)
            self._ensure_node(f"tool:{tool}", "tool", label=tool, sanctioned=sanctioned)
        if department:
            self._ensure_node(f"dept:{department}", "department", label=department)
        if data_type:
            sensitive = event.metadata.get("sensitive", False)
            self._ensure_node(f"data:{data_type}", "data_type", label=data_type, sensitive=sensitive)
        if domain:
            self._ensure_node(f"domain:{domain}", "domain", label=domain)

        # Event node
        self._ensure_node(
            f"event:{event.id}",
            "event",
            label=event.event_type.value,
            severity=event.severity.value,
            timestamp=event.timestamp.isoformat(),
        )

        # Add relationships
        if user and tool:
            self._ensure_edge(f"user:{user}", f"tool:{tool}", "uses")
        if user and department:
            self._ensure_edge(f"user:{user}", f"dept:{department}", "belongs_to")
        if tool and data_type:
            self._ensure_edge(f"tool:{tool}", f"data:{data_type}", "processes")
        if tool and domain:
            self._ensure_edge(f"tool:{tool}", f"domain:{domain}", "connects_to")
        if department and tool:
            self._ensure_edge(f"dept:{department}", f"tool:{tool}", "uses_tool")
        if user:
            self._ensure_edge(f"user:{user}", f"event:{event.id}", "triggered")

        # Periodic persistence
        if self._event_count % 20 == 0:
            await self.persist()

        logger.debug("graph_store.event_added", event_id=event.id, nodes=self.graph.number_of_nodes())

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Search the knowledge graph by finding relevant nodes and traversing relationships.

        Matches query terms against node labels and returns subgraph context.
        """
        if self.graph.number_of_nodes() == 0:
            return []

        query_terms = query.lower().split()
        scored_nodes: dict[str, float] = {}

        # Score nodes by query term relevance
        for node_id, data in self.graph.nodes(data=True):
            label = str(data.get("label", "")).lower()
            node_type = str(data.get("node_type", "")).lower()
            score = 0.0
            for term in query_terms:
                if term in label:
                    score += 0.6
                if term in node_type:
                    score += 0.2
                if term in node_id.lower():
                    score += 0.3
            # Weight by node importance (connection count)
            weight = data.get("weight", 1)
            score *= (1 + 0.1 * min(weight, 10))
            if score > 0:
                scored_nodes[node_id] = score

        # Get top-scoring nodes
        top_nodes = sorted(scored_nodes.items(), key=lambda x: x[1], reverse=True)[:top_k]

        results = []
        for node_id, score in top_nodes:
            node_data = dict(self.graph.nodes[node_id])
            # Get neighbor context
            neighbors = []
            for neighbor in self.graph.neighbors(node_id):
                edge_data = self.graph[node_id][neighbor]
                neighbor_data = self.graph.nodes[neighbor]
                neighbors.append({
                    "id": neighbor,
                    "label": neighbor_data.get("label", ""),
                    "relation": edge_data.get("relation", ""),
                })
            # Also check predecessors
            for pred in self.graph.predecessors(node_id):
                edge_data = self.graph[pred][node_id]
                pred_data = self.graph.nodes[pred]
                neighbors.append({
                    "id": pred,
                    "label": pred_data.get("label", ""),
                    "relation": f"(incoming) {edge_data.get('relation', '')}",
                })

            context_text = f"{node_data.get('label', node_id)} [{node_data.get('node_type', '')}]"
            if neighbors:
                rel_strs = [f"{n['relation']} → {n['label']}" for n in neighbors[:5]]
                context_text += " | Relations: " + "; ".join(rel_strs)

            results.append({
                "id": node_id,
                "text": context_text,
                "metadata": node_data,
                "neighbors": neighbors[:8],
                "score": min(1.0, score),
                "source": "graph",
            })

        return results

    @property
    def stats(self) -> dict:
        type_counts = defaultdict(int)
        for _, data in self.graph.nodes(data=True):
            type_counts[data.get("node_type", "unknown")] += 1
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "node_types": dict(type_counts),
            "events_processed": self._event_count,
        }
