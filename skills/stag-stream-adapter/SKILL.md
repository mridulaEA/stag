---
name: stag-stream-adapter
description: Build a new STAG stream source / adapter (stag/streams) as a governed product — stable event identity, checkpointing, backfill and replay rules, schema-drift handling, fixtures and tests. Use when connecting Kafka topics, webhooks, SaaS audit logs, OpenTelemetry or any new source to STAG. Ensures redelivery keeps identity and vendor cursors are not mistaken for event ids.
license: MIT
metadata:
  author: Enterprise AI Tech
  version: "1.0.0"
  domain: development
---

# STAG: Stream Adapter Authoring

An adapter turns a source's records into `StreamEvent`s **without destroying source semantics**
and with identity the coordinator can trust. Adapters are governed products: owner, supported
source versions, fixtures, rate limits, checkpointing, backfill rules, schema-drift behaviour.

## Procedure

1. **Subclass `StreamSource`** (`stag/streams/base.py`): implement `connect`, `disconnect`,
   `_read_events` (put `StreamEvent`s on `self._queue`). Respect the bounded queue; do not grow
   unbounded buffers (backpressure is an operational requirement).
2. **Identity mapping (most important step).**
   - `StreamEvent.id` = the source's *logical event id* (CloudEvents `id`, message key + offset
     when the producer guarantees stability, audit record id). A resend must keep the same id.
   - `StreamEvent.source` = `source_system/source_instance` (e.g. `salesforce/acct-7731`).
   - Never use a replay cursor or stream position as the event id; store it in
     `metadata.source_sequence`.
   - Never generate a fresh UUID for a forwarded event.
3. **Envelope mapping.** Fill `metadata.environment`, `metadata.service`,
   `metadata.schema_version`, `metadata.adapter_version`, `metadata.observed_at`,
   `metadata.sensitivity`. Keep original severity in `payload.original_severity` and map a
   normalized `severity`. Keep units on metrics; keep span ids on traces. Missing fields stay
   visible (`metadata.missing_fields`).
4. **Time.** Distinguish event time (`timestamp`), observation time and ingestion time. Late data
   is normal; the coordinator applies late-event policy, the adapter just labels honestly.
5. **Checkpointing and replay.** Commit the source position only after the event is enqueued.
   Support a `replay=True` mode that tags events `metadata.replay=true` so downstream disables
   notifications and mutations.
6. **Schema drift.** Validate against the supported source schema versions; on an unknown shape,
   emit the event with `metadata.parsing_status="partial"` and `metadata.mapping_provenance`,
   and raise a `schema_drift` counter. Never silently remap in production.
7. **Fixtures and tests** (`tests/test_streams_<name>.py`): a normal record, an exact redelivery
   (same id, same payload), a conflicting redelivery (same id, different payload), a late record,
   a drifted schema, a replay batch. Assert on produced `StreamEvent` identity and metadata.
8. **Register** in `stag/main.py::create_stream_source` behind a `STREAM_SOURCE` value and
   document env vars in `.env.example` and the README configuration table.

## Guardrails

- Credentials come from settings/env, never from event payloads.
- Rate-limit and bound retries toward the source; an adapter must not become part of the incident.
- Where a SaaS exposes no internal logs, record the visibility limit instead of inventing signals.
  No telemetry is not the same as a healthy application.
