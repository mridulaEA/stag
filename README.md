# STAG — Stream-Triggered Augmented Generation

**Proactive AI that monitors data streams, detects events, and generates insights autonomously.**

STAG is an open-source framework that flips traditional RAG on its head. Instead of waiting for user queries, STAG continuously monitors data streams, fires triggers on significant events, performs hybrid retrieval across vector, graph, and sparse knowledge stores, and autonomously generates actionable insights.

Named after the concept of **Stream-Triggered Augmented Generation** — where the stream triggers the AI, not the user.

![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python) ![FastAPI](https://img.shields.io/badge/FastAPI-0.109-green?logo=fastapi) ![React](https://img.shields.io/badge/React-18.3-blue?logo=react) ![License](https://img.shields.io/badge/License-MIT-green) ![Skills](https://img.shields.io/badge/Agent%20Skills-Claude%20Code%20%7C%20Codex%20%7C%20Cursor-purple)

> **New in 0.2:** a [TechOps backbone](#techops-deterministic-backbone-bounded-agents) (dedup, incident lifecycle, bounded investigation, retry-safe actions) and a [skill pack](#agent-skills-claude-code-codex-cursor) that works in Claude Code, Codex and Cursor. Start with [`skills/README.md`](skills/README.md).

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
3b. **Coordinate** *(optional, TechOps)* — Duplicate deliveries are counted, repeated symptoms update the open incident, and only a *material change* creates an investigation request
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

### TechOps Backbone (deterministic, bounded agents)
- **Four identities kept separate**: observation (tenant+source+event_id), signal grouping key, incident episode, action operation
- **Incident lifecycle state machine**: `DETECTED → … → VERIFYING → RESOLVED`; every transition recorded with actor, reason and version; an agent message never changes state
- **Coordinator**: redelivery counted without inflating evidence, conflicting payloads quarantined, repeated symptoms attached to the right episode, investigation only on material change (new incident, severity increase, new signature, recurrence, cooldown)
- **Assignment contract**: YAML scope / capabilities / limits / required output enforced by `BoundedRun` outside the model
- **Action ledger**: deterministic operation ids, authorize → revalidate → dispatch, `OUTCOME_UNKNOWN → RECONCILING`, independent verification
- **Budget ledger**: reserve before dispatch, settle afterward
- **REST**: `/api/techops/incidents`, `/api/techops/investigations`, `/api/techops/actions`

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
│   ├── techops/                   # Deterministic TechOps backbone
│   │   ├── identity.py           # Observation / signal / incident / action identity
│   │   ├── incident.py           # Incident lifecycle state machine
│   │   ├── coordinator.py        # Dedup, correlation, material-change gating
│   │   ├── assignment.py         # Bounded investigation contract + enforcement
│   │   ├── actions.py            # Retry-safe action ledger
│   │   └── budget.py             # Reserve/settle admission control
│   ├── skills/                    # `stag-skills` CLI (list / validate / install)
│   ├── pipeline/                  # Pipeline orchestration
│   │   └── orchestrator.py       # Stream → Trigger → (Coordinate) → Retrieve → Generate
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
├── skills/                        # Agent skill pack (Claude Code / Codex / Cursor)
│   ├── stag-techops-*/SKILL.md   # TechOps workflows
│   ├── stag-multi-agent-orchestration/
│   └── stag-*/                   # development skills
├── agents/                        # Claude Code subagents (coordinator, investigator, reviewer, verifier)
├── .claude-plugin/                # plugin.json + marketplace.json (install from anywhere)
├── .claude/skills, .agents/skills # → ../skills (project discovery for Claude Code, Codex, Cursor)
├── AGENTS.md, CLAUDE.md, .cursor/rules/   # agent guidance per runtime
├── docs/techops/                  # TechOps design summary
├── docs/community/                # Developer community guide
├── tests/                         # Test suite
│   ├── test_triggers.py          # Trigger rule tests
│   ├── test_pipeline.py          # Pipeline component tests
│   ├── test_techops.py           # Backbone: dedup, lifecycle, contracts, ledger, budget
│   ├── test_techops_api.py       # /api/techops endpoints
│   └── test_skills.py            # Skill pack validation + installer
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
| **TechOps** | | |
| `TECHOPS_COORDINATOR_ENABLED` | `false` | Gate the LLM path behind dedup + incident correlation |
| `TECHOPS_TENANT` | `default` | Tenant scope enforced on every identity |
| `TECHOPS_INVESTIGATION_COOLDOWN_SECONDS` | `300` | Minimum gap between routine re-investigations (severity increase / new signature bypass it) |
| `TECHOPS_RECURRENCE_WINDOW_SECONDS` | `3600` | Signal returning within this window after RESOLVED is flagged as recurrence |
| `TECHOPS_DEDUP_RETENTION` | `50000` | Observation identities kept for duplicate detection (size to your replay horizon) |
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
| `GET` | `/api/techops/incidents?open_only=true` | Incident episodes with dedup / correlation stats |
| `GET` | `/api/techops/incidents/{id}` | One incident with history and its actions |
| `POST` | `/api/techops/incidents/{id}/transition` | Explicit lifecycle transition `{to, actor, reason}` |
| `POST` | `/api/techops/incidents/{id}/external` | Map to an external ticket / incident id |
| `GET` | `/api/techops/investigations` | Investigation requests (material changes) |
| `GET` | `/api/techops/actions` | Action ledger |
| `POST` | `/api/techops/actions/prepare` | Prepare a remediation operation (deterministic id) |
| `POST` | `/api/techops/actions/{op}/authorize` | Bind an approval to target + incident version |
| `POST` | `/api/techops/actions/{op}/dispatch?current_incident_version=` | Revalidate staleness, then dispatch |
| `POST` | `/api/techops/actions/{op}/outcome-unknown` | Lost response → reconciling |
| `POST` | `/api/techops/actions/{op}/reconcile?target_state_observed=` | Resolve from the target's state |
| `POST` | `/api/techops/actions/{op}/verify` | Record independent recovery verification |

### WebSocket

Connect to `ws://localhost:8000/api/ws` for real-time streaming:

```json
// Event (no trigger fired)
{"type": "event", "event": {...}}

// Insight (trigger fired → retrieval → generation)
{"type": "insight", "output": {"event": {...}, "trigger": {...}, "retrieval": {...}, "insight": {...},
                               "incident_id": "INC-…", "investigation_request_id": "INV-…", "material_change": "new_incident"}}

// Incident update (TechOps coordinator enabled; trigger fired but no material change → no LLM call)
{"type": "incident_update", "event": {...}, "trigger": {...}, "decision": {"incident_id": "INC-…", "duplicate": false, "material_change": "none", ...}}
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

## TechOps: deterministic backbone, bounded agents

STAG 0.2 adds the controls from the *STAG in Production* and *Enterprise STAG* articles so that
**continuous monitoring does not require continuous LLM calls** and investigations never act twice.

```text
event + trigger ──► IncidentCoordinator.ingest
   ├─ redelivery (same identity, same payload)  → count only
   ├─ conflict   (same identity, other payload) → quarantine
   ├─ repeated symptom on open incident          → attach evidence, bump version, no model call
   └─ material change                            → InvestigationRequest ──► bounded investigation
                                                                              │ (assignment contract)
                                                   ActionLedger.prepare ◄─── proposal
                                                   authorize → revalidate → dispatch → verify
```

```bash
TECHOPS_COORDINATOR_ENABLED=true python -m stag.main
curl localhost:8000/api/techops/incidents | jq '.stats'
```

Programmatic use:

```python
from stag.techops import IncidentCoordinator, InvestigationAssignment, ActionLedger

coord = IncidentCoordinator(tenant="acme")
decision = coord.ingest(event, trigger)          # .duplicate / .material_change / .investigation_request

assignment = InvestigationAssignment.from_yaml(open("assignment.yaml").read())
run = assignment.start()
run.use_capability("query_scoped_logs", service="checkout-api")   # PermissionDenied if not allowed
findings = run.finish(my_findings)               # partial + escalate when limits/evidence fall short

ledger = ActionLedger()
op = ledger.prepare(decision.incident_id, "restart_component", "checkout-api", {"replicas": 2})
ledger.authorize(op.operation_id, actor="oncall@acme", incident_version=coord.get_incident(op.incident_id).version)
ledger.dispatch(op.operation_id)                 # retries reuse op.operation_id
```

Design summary: [`docs/techops/README.md`](docs/techops/README.md). Tests mirroring the
failure-scenario matrix: [`tests/test_techops.py`](tests/test_techops.py).

## Agent skills (Claude Code, Codex, Cursor)

Thirteen skills in the open Agent Skills format live in [`skills/`](skills/) and are discovered
automatically in this repo by Claude Code (`.claude/skills`), Codex and Cursor (`.agents/skills`).
Use them from any other project or machine:

```bash
# Claude Code plugin (skills + 4 subagents)
claude plugin marketplace add mridulaEA/stag
claude plugin install stag-techops@stag-skills

# Claude Code / Codex / Cursor user-level skills, from a clone ...
python -m stag.skills install --target all        # or --target claude|codex|cursor

# ... or without cloning
curl -fsSL https://raw.githubusercontent.com/mridulaEA/stag/main/scripts/install-skills.sh | bash
```

| Group | Skills |
|---|---|
| TechOps incident workflow | triage · dedup/correlation · bounded investigation · remediation ledger · recovery verification · postmortem |
| TechOps platform | signal tuning · resilience tests · enterprise onboarding |
| Multi-agent | `stag-multi-agent-orchestration` + Claude Code subagents in [`agents/`](agents/) |
| Development | trigger rule authoring · stream adapter · skill authoring |

Full catalog and invocation per tool: [`skills/README.md`](skills/README.md).

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
- *STAG in Production: When One Failure Becomes a Thousand AI Triggers* and *Enterprise STAG: Deterministic Operations, Bounded Agentic Investigation* — the TechOps articles behind `stag/techops/` and the skill pack (summary in [`docs/techops/README.md`](docs/techops/README.md))

---

## Community & Contributing

- Start here: [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`docs/community/README.md`](docs/community/README.md) · [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- Propose a skill with the *Skill proposal* issue template; propose backbone changes with *TechOps workflow change*.
- Agent-friendly repo: `AGENTS.md`, `CLAUDE.md`, `.cursor/rules/` and the skill pack mean Codex, Claude Code and Cursor follow the project's conventions out of the box.

Areas where help is needed:

- [ ] Persistence for incidents and the action ledger (PostgreSQL / Redis) with an outbox dispatcher
- [ ] PagerDuty / Jira / ServiceNow record mapping with verified dedup semantics
- [ ] Labelled incident replay set for false-merge / false-split metrics
- [ ] More skills: on-call handoff, change-freeze review, knowledge curation (see `docs/community`)
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
