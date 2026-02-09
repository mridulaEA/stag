# STAG — Stream-Triggered Augmented Generation

**Proactive AI that monitors data streams, detects events, and generates insights autonomously.**

STAG is an open-source framework that flips traditional RAG on its head. Instead of waiting for user queries, STAG continuously monitors data streams, fires triggers on significant events, performs hybrid retrieval across vector, graph, and sparse knowledge stores, and autonomously generates actionable insights.

Named after the concept of **Stream-Triggered Augmented Generation** — where the stream triggers the AI, not the user.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python) ![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green?logo=fastapi) ![React](https://img.shields.io/badge/React-18.3-blue?logo=react) ![License](https://img.shields.io/badge/License-MIT-green)

---

## The Problem with Traditional RAG

Traditional Retrieval-Augmented Generation is **reactive** — it sits idle until a user asks a question. In enterprise environments with continuous data streams (security events, logs, transactions, IoT telemetry), critical patterns emerge between queries that go undetected.

| | Traditional RAG | STAG |
|---|---|---|
| **Trigger** | User query | Data stream event |
| **Mode** | Reactive | Proactive |
| **Knowledge** | Static index | Continuously evolving |
| **Retrieval** | Single method (vector) | Hybrid (vector + graph + sparse) |
| **Output** | Answer to question | Autonomous insight generation |
| **Latency** | On-demand | Real-time |

**STAG bridges this gap by making retrieval-augmented generation event-driven.**

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA STREAMS                              │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐       │
│  │  Kafka   │  │ Webhook  │  │ Simulator│  │   API    │       │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └────┬─────┘       │
│       └──────────────┴──────────────┴──────────────┘             │
│                          │                                       │
│                    ┌─────▼─────┐                                 │
│                    │  STREAM   │                                 │
│                    │  SOURCE   │                                 │
│                    └─────┬─────┘                                 │
│                          │                                       │
│              ┌───────────▼───────────┐                           │
│              │    TRIGGER ENGINE     │                           │
│              │                       │                           │
│              │  • Severity Threshold │                           │
│              │  • Pattern Matching   │                           │
│              │  • Sensitive Data     │                           │
│              │  • Frequency/Temporal │                           │
│              │  • Data Volume        │                           │
│              └───────┬───────────────┘                           │
│                      │ (trigger fires)                           │
│                      │                                           │
│         ┌────────────▼────────────┐                              │
│         │    HYBRID RETRIEVER     │                              │
│         │                         │                              │
│         │  ┌───────┐ ┌────────┐  │                              │
│         │  │Vector │ │ Graph  │  │                              │
│         │  │ChromaDB│ │NetworkX│  │                              │
│         │  └───┬───┘ └───┬────┘  │                              │
│         │      │    RRF  │       │                              │
│         │  ┌───┴────┐    │       │                              │
│         │  │Sparse  │◄───┘       │                              │
│         │  │ BM25   │            │                              │
│         │  └────────┘            │                              │
│         └────────────┬───────────┘                              │
│                      │                                           │
│              ┌───────▼───────┐                                   │
│              │   GENERATOR   │                                   │
│              │               │                                   │
│              │  OpenAI /     │                                   │
│              │  Anthropic /  │                                   │
│              │  Fallback     │                                   │
│              └───────┬───────┘                                   │
│                      │                                           │
│              ┌───────▼───────┐        ┌──────────────┐          │
│              │   PIPELINE    │───────►│  WebSocket   │          │
│              │   OUTPUT      │        │  Broadcast   │          │
│              └───────────────┘        └──────┬───────┘          │
│                                              │                   │
│                                    ┌─────────▼─────────┐        │
│                                    │  React Dashboard  │        │
│                                    │  (Real-time UI)   │        │
│                                    └───────────────────┘        │
└─────────────────────────────────────────────────────────────────┘
```

### Pipeline Flow

1. **Stream** — Events flow in from Kafka, webhooks, or the built-in simulator
2. **Index** — Every event is indexed across all three knowledge stores simultaneously
3. **Trigger** — Rules evaluate each event (severity, patterns, sensitive data, frequency, volume)
4. **Retrieve** — When a trigger fires, hybrid retrieval pulls relevant context from vector (semantic), graph (relational), and sparse (keyword) stores
5. **Fuse** — Reciprocal Rank Fusion (RRF) merges results from all three retrieval paths
6. **Generate** — LLM produces a structured insight with title, analysis, severity, and recommendations
7. **Broadcast** — Output is pushed to all connected clients via WebSocket in real-time

---

## Features

### Stream Processing
- **Multiple stream sources**: Kafka consumer, HTTP webhook, built-in event simulator
- **Pluggable architecture**: Abstract `StreamSource` base class for custom sources
- **Async processing**: Full async/await pipeline for high throughput
- **Backpressure handling**: Bounded queue with configurable capacity

### Trigger Engine
- **5 built-in rule types**:
  - **Severity Threshold** — Fires on events at or above a severity level
  - **Pattern Matching** — Regex matching on any payload field
  - **Sensitive Data** — Detects sensitive data types in unsanctioned tools
  - **Frequency/Temporal** — Rate limiting per user/source within time windows
  - **Data Volume** — Fires on large data transfers
- **Composable rules**: Register custom rules via `RuleRegistry`
- **Best-match selection**: Returns highest-confidence trigger when multiple rules fire

### Hybrid Knowledge Store
- **Vector Store (ChromaDB)**: Semantic similarity search with sentence-transformer embeddings
- **Graph Store (NetworkX)**: Dynamic knowledge graph that extracts entities (users, tools, departments, data types) and relationships from events, supports relationship-aware retrieval
- **Sparse Store (BM25)**: Keyword-based retrieval for exact matches on tool names, policy IDs, usernames
- **Reciprocal Rank Fusion**: Merges results with configurable weights (default: vector 40%, graph 35%, sparse 25%)

### LLM Generation
- **Multi-provider**: OpenAI and Anthropic support with async clients
- **Structured output**: JSON-formatted insights with title, summary, analysis, severity, recommendations
- **Fallback generation**: Template-based output when no LLM API key is configured
- **Configurable**: Temperature, max tokens, model selection via environment variables

### Real-time Dashboard
- **WebSocket streaming**: Live event feed and insight cards
- **Pipeline visualization**: Stream → Trigger → Retrieve → Generate flow with live counters
- **Charts**: Event rate area chart, severity distribution pie chart
- **Knowledge search**: Search the hybrid knowledge base from the UI
- **Expandable insights**: Drill into analysis, recommendations, retrieval sources, pipeline timing

### API
- **REST endpoints**: Events, insights, knowledge search, pipeline control, stats
- **WebSocket**: Real-time pipeline output streaming
- **Event injection**: POST events via API for integration with external systems
- **OpenAPI docs**: Auto-generated at `/docs`

---

## Tech Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| **Backend** | Python 3.11+ / FastAPI | Async API server and pipeline orchestration |
| **Vector Search** | sentence-transformers + numpy | In-memory cosine similarity with sentence-transformer embeddings |
| **Knowledge Graph** | NetworkX | Dynamic entity-relationship graph with persistence |
| **Sparse Search** | rank-bm25 | BM25 keyword retrieval |
| **Streaming** | aiokafka | Apache Kafka consumer (production) |
| **LLM** | OpenAI / Anthropic SDK | Insight generation |
| **Frontend** | React 18 + Vite + Tailwind | Real-time dashboard |
| **Charts** | Recharts | Data visualization |
| **Logging** | structlog | Structured logging |

---

## Getting Started

### Prerequisites

- Python 3.11+
- Node.js 18+ (for frontend)
- (Optional) OpenAI or Anthropic API key for LLM generation

### Quick Start

```bash
git clone https://github.com/mridulaEA/stag.git
cd stag

# Backend
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your API key (optional — works without one using fallback generation)

# Start the backend
python -m stag.main
```

The API starts at `http://localhost:8000` with the event simulator running.

### Frontend (optional)

```bash
cd frontend
npm install
npm run dev
```

Opens at `http://localhost:5173` with live WebSocket connection to the backend.

### Docker

```bash
docker-compose up --build
```

---

## Project Structure

```
stag/
├── stag/                          # Python backend
│   ├── streams/                   # Stream sources
│   │   ├── base.py               # Abstract StreamSource
│   │   ├── simulator.py          # Enterprise AI event simulator
│   │   ├── kafka_stream.py       # Kafka consumer
│   │   └── webhook.py            # HTTP webhook receiver
│   ├── triggers/                  # Trigger engine
│   │   ├── engine.py             # Rule evaluation engine
│   │   └── rules.py              # 5 built-in rule types + registry
│   ├── knowledge/                 # Knowledge stores
│   │   ├── vector_store.py       # ChromaDB vector search
│   │   ├── graph_store.py        # NetworkX knowledge graph
│   │   ├── sparse_store.py       # BM25 sparse retrieval
│   │   └── hybrid_retriever.py   # RRF fusion across all three
│   ├── generation/                # LLM generation
│   │   ├── generator.py          # Multi-provider insight generator
│   │   └── prompts.py            # System and insight prompt templates
│   ├── pipeline/                  # Pipeline orchestration
│   │   └── orchestrator.py       # Stream → Trigger → Retrieve → Generate
│   ├── api/                       # FastAPI layer
│   │   ├── routes.py             # REST + WebSocket endpoints
│   │   └── websocket.py          # Connection manager
│   ├── models/                    # Pydantic schemas
│   │   └── schemas.py            # StreamEvent, TriggerResult, Insight, etc.
│   ├── config.py                  # Settings from environment
│   └── main.py                    # App entry point + lifespan
├── frontend/                      # React dashboard
│   ├── src/
│   │   ├── App.jsx               # Main dashboard UI
│   │   └── hooks/useWebSocket.js # WebSocket connection hook
│   ├── package.json
│   └── vite.config.js            # Dev proxy to backend
├── tests/                         # Test suite
│   ├── test_triggers.py          # Trigger rule tests
│   └── test_pipeline.py          # Pipeline component tests
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
└── .env.example
```

---

## Configuration

All configuration is via environment variables (or `.env` file):

| Variable | Default | Description |
|----------|---------|-------------|
| **LLM** | | |
| `LLM_PROVIDER` | `openai` | LLM provider: `openai` or `anthropic` |
| `OPENAI_API_KEY` | - | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o` | OpenAI model |
| `ANTHROPIC_API_KEY` | - | Anthropic API key |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Anthropic model |
| **Embedding** | | |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence-transformer model for vector embeddings |
| **Stream** | | |
| `STREAM_SOURCE` | `simulator` | Stream source: `simulator`, `kafka`, `webhook` |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka broker addresses |
| `KAFKA_TOPIC` | `stag-events` | Kafka topic to consume |
| `KAFKA_GROUP_ID` | `stag-consumer` | Kafka consumer group ID |
| **Storage** | | |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | ChromaDB persistence directory |
| `GRAPH_PERSIST_PATH` | `./data/graph.json` | Knowledge graph persistence file |
| **Trigger** | | |
| `TRIGGER_EVAL_INTERVAL` | `2.0` | Trigger evaluation interval (seconds) |
| `TRIGGER_ANOMALY_THRESHOLD` | `0.75` | Anomaly detection confidence threshold |
| **Generation** | | |
| `GENERATION_MAX_TOKENS` | `1024` | Max tokens for LLM generation |
| `GENERATION_TEMPERATURE` | `0.3` | LLM temperature |
| **Simulator** | | |
| `SIMULATOR_INTERVAL` | `3.0` | Seconds between simulated events |
| `SIMULATOR_SCENARIO` | `enterprise_ai` | Simulation scenario |
| **Server** | | |
| `HOST` | `0.0.0.0` | Server bind address |
| `PORT` | `8000` | Server port |
| `CORS_ORIGINS` | `["http://localhost:5173","http://localhost:3000"]` | Allowed CORS origins (JSON array) |

---

## API Reference

### REST Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check |
| `GET` | `/api/stats` | Pipeline statistics |
| `GET` | `/api/events?limit=50` | Recent stream events |
| `POST` | `/api/events` | Inject an event |
| `GET` | `/api/insights?limit=20` | Recent generated insights |
| `GET` | `/api/knowledge/search?q=...&top_k=10` | Hybrid knowledge search |
| `GET` | `/api/knowledge/graph/stats` | Knowledge graph statistics |
| `POST` | `/api/pipeline/start` | Start the pipeline |
| `POST` | `/api/pipeline/stop` | Stop the pipeline |

### WebSocket

Connect to `ws://localhost:8000/api/ws` for real-time streaming:

```json
// Event (no trigger fired)
{"type": "event", "event": {...}}

// Insight (trigger fired → retrieval → generation)
{"type": "insight", "output": {"event": {...}, "trigger": {...}, "retrieval": {...}, "insight": {...}}}
```

---

## How It Works — Demo Scenario

STAG ships with a built-in enterprise AI monitoring simulator that generates realistic events:

1. **Shadow AI Usage** — Employees using ChatGPT, Claude, Gemini with corporate data
2. **Data Exposure** — Sensitive data (source code, PII, financials) detected by DLP
3. **Policy Violations** — Usage against acceptable use policies
4. **Network Anomalies** — Unusual traffic patterns to AI service endpoints
5. **Authentication Events** — Login attempts to AI tools
6. **Data Transfers** — Outbound data to AI services

When a trigger fires (e.g., "source code sent to ChatGPT by Engineering"), the pipeline:

- Searches the **vector store** for semantically similar past events
- Traverses the **knowledge graph** to find related users, tools, and departments
- Matches **keywords** via BM25 for exact tool/policy names
- **Fuses** all results via RRF
- **Generates** an insight with analysis, severity assessment, and recommendations

---

## Extending STAG

### Custom Stream Source

```python
from stag.streams.base import StreamSource

class MySource(StreamSource):
    async def connect(self):
        # Connect to your data source
        pass

    async def disconnect(self):
        pass

    async def _read_events(self):
        while self._running:
            event = await get_next_event()  # Your logic
            await self._queue.put(event)
```

### Custom Trigger Rule

```python
from stag.triggers.rules import TriggerRule
from stag.models.schemas import TriggerType

class MyRule(TriggerRule):
    def __init__(self):
        super().__init__("my_rule", TriggerType.PATTERN)

    def evaluate(self, event):
        # Your trigger logic
        fired = some_condition(event)
        return TriggerResult(
            trigger_type=self.trigger_type,
            rule_name=self.name,
            fired=fired,
            confidence=0.9,
            event_id=event.id,
            reason="My custom reason",
        )

# Register it
registry = RuleRegistry.default()
registry.register(MyRule())
```

### Production Deployment

For production, swap in real infrastructure:

| MVP Component | Production Alternative |
|--------------|----------------------|
| Event Simulator | Kafka, Kinesis, Pulsar |
| numpy cosine similarity | Qdrant, Weaviate, Pinecone, ChromaDB |
| NetworkX (in-memory) | Neo4j, Amazon Neptune |
| BM25 (in-memory) | Elasticsearch, OpenSearch |
| File persistence | PostgreSQL, Redis |

---

## Related Reading

- [Stream-Triggered RAG: How Proactive AI Monitors Data Streams](https://enterpriseaitech.com/blog/stream-triggered-rag) — The foundational article introducing STAG
- [Stream-Triggered Hybrid GraphRAG](https://enterpriseaitech.com/blog/stream-triggered-hybrid-graphrag) — Deep dive into combining streaming, knowledge graphs, and hybrid retrieval
- [Shadow AI Governance](https://enterpriseaitech.com/blog/shadow-ai-governance) — Enterprise AI governance framework (the demo scenario context)

---

## Contributing

Contributions are welcome. Areas where help is needed:

- [ ] Production Kafka/Kinesis connectors with exactly-once semantics
- [ ] Neo4j graph store adapter
- [ ] Qdrant/Weaviate vector store adapters
- [ ] Elasticsearch sparse store adapter
- [ ] Multi-modal stream support (images, audio, video)
- [ ] Trigger rule DSL for non-code rule authoring
- [ ] Grafana/Prometheus observability integration
- [ ] Authentication and multi-tenancy
- [ ] Benchmark suite for retrieval quality
- [ ] Mobile-responsive frontend

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

Built for the enterprise AI community by [Enterprise AI Tech](https://enterpriseaitech.com).
