"""Bind the action ledger's state machine to a destination connector.

The ledger decides what a result means; the connector only reports what happened:

* connector returns *applied* → ``mark_applied`` with the receipt (verification still required)
* connector raises ``ConnectorTimeout`` / any error → ``mark_outcome_unknown`` → RECONCILING; a
  later ``reconcile()`` asks the connector to *observe* the destination (never to retry blindly)
* connector raises ``ConnectorRejected`` → the destination refused: FAILED with the reason

The ledger transition to DISPATCHED happens *before* the destination is called, so an attempt is
counted even when the process dies mid-call, and no connector is called for an operation the
ledger would not dispatch (stale approval, exhausted retry ceiling, wrong state).
"""

from __future__ import annotations

from typing import Any

import structlog

from stag.connectors.actions import ActionConnector, ActionReceipt
from stag.connectors.base import ConnectorRejected, NoConnectorError
from stag.techops.actions import ActionLedger, ActionRecord

logger = structlog.get_logger()


class ActionDispatcher:
    def __init__(self, ledger: ActionLedger, connectors: list[ActionConnector]) -> None:
        self.ledger = ledger
        self.connectors = list(connectors)
        self.receipts: list[ActionReceipt] = []

    def connector_for(self, record: ActionRecord) -> ActionConnector | None:
        return next((c for c in self.connectors if c.supports(record)), None)

    def execute(self, operation_id: str, actor: str = "executor", current_incident_version: int | None = None) -> tuple[ActionRecord, ActionReceipt]:
        record = self.ledger.get(operation_id)
        connector = self.connector_for(record)
        if connector is None:
            raise NoConnectorError(f"no action connector accepts target '{record.target}' (configured: {[c.name for c in self.connectors] or 'none'})")
        record = self.ledger.dispatch(operation_id, actor=actor, current_incident_version=current_incident_version)
        try:
            receipt = connector.dispatch(record)
        except ConnectorRejected as e:
            receipt = ActionReceipt(operation_id=operation_id, connector=connector.name, status="rejected", detail={"reason": str(e)[:300]}, simulated=connector.simulated)
            self.ledger.mark_failed(operation_id, actor=connector.name, reason=f"destination rejected: {str(e)[:200]}")
        except Exception as e:  # timeout, transport failure, adapter bug: outcome unknown, never "failed"
            receipt = ActionReceipt(operation_id=operation_id, connector=connector.name, status="unknown", detail={"reason": f"{type(e).__name__}: {str(e)[:300]}"}, simulated=connector.simulated)
            self.ledger.mark_outcome_unknown(operation_id, actor=connector.name, note=f"{type(e).__name__}: {str(e)[:200]}")
        else:
            if receipt.status == "applied":
                self.ledger.mark_applied(operation_id, actor=connector.name, receipt=receipt.model_dump(mode="json"))
            elif receipt.status == "rejected":
                self.ledger.mark_failed(operation_id, actor=connector.name, reason=str(receipt.detail.get("reason", "rejected"))[:200])
            else:
                self.ledger.mark_outcome_unknown(operation_id, actor=connector.name, note=str(receipt.detail.get("reason", "unknown outcome"))[:200])
        self.receipts.append(receipt)
        logger.info("connector.dispatch", operation_id=operation_id, connector=connector.name, status=receipt.status, external_id=receipt.external_id)
        return self.ledger.get(operation_id), receipt

    def reconcile(self, operation_id: str, actor: str = "reconciler") -> tuple[ActionRecord, bool | None]:
        """Observe the destination and let the ledger settle the uncertain outcome."""
        record = self.ledger.get(operation_id)
        connector = self.connector_for(record)
        if connector is None:
            raise NoConnectorError(f"no action connector accepts target '{record.target}'")
        observed = connector.reconcile(record)
        if observed is None:
            logger.warning("connector.reconcile_unknown", operation_id=operation_id, connector=connector.name)
            return record, None
        note = "destination shows the effect" if observed else "destination shows no effect"
        return self.ledger.reconcile(operation_id, observed, actor=f"{actor}:{connector.name}", note=note), observed

    def describe(self) -> list[dict[str, Any]]:
        return [c.describe() for c in self.connectors]
