"""Budget admission control: reserve before dispatch, settle afterward.

    Available allocation = approved allocation − recorded spend − outstanding reservations

A dashboard shows what happened. An admission controller decides whether more work may start.
Each retry is another possible cost even when it does not create another business action, and
an uncertain or timed-out request is reconciled rather than treated as free.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class BudgetExhausted(Exception):
    """Raised when a reservation would exceed the available allocation."""


class Reservation(BaseModel):
    ref: str
    amount: float
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    settled: bool = False
    actual: float | None = None


class BudgetLedger:
    """A single allocation (per incident, service, environment, or cell).

    The ledger is deliberately simple: it never grows its own allocation. Raising the allocation
    is an authorized operation performed by whoever owns the budget, never by the agent.
    """

    def __init__(self, allocation: float, name: str = "default") -> None:
        if allocation < 0:
            raise ValueError("allocation must be non-negative")
        self.name = name
        self.allocation = float(allocation)
        self.spent = 0.0
        self._reservations: dict[str, Reservation] = {}

    @property
    def reserved(self) -> float:
        return sum(r.amount for r in self._reservations.values() if not r.settled)

    @property
    def available(self) -> float:
        return self.allocation - self.spent - self.reserved

    def reserve(self, ref: str, amount: float) -> Reservation:
        """Reserve a conservative allowance for a bounded call or task before dispatch."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        existing = self._reservations.get(ref)
        if existing and not existing.settled:
            return existing  # idempotent: retrying the reservation does not double-reserve
        if amount > self.available:
            raise BudgetExhausted(
                f"budget '{self.name}': requested {amount:.2f}, available {self.available:.2f}"
            )
        reservation = Reservation(ref=ref, amount=amount)
        self._reservations[ref] = reservation
        return reservation

    def settle(self, ref: str, actual: float) -> Reservation:
        """Record actual usage and release the unused part of the reservation."""
        reservation = self._reservations[ref]
        if reservation.settled:
            return reservation
        reservation.settled = True
        reservation.actual = float(actual)
        self.spent += float(actual)
        return reservation

    def release(self, ref: str) -> None:
        """Release a reservation whose work never ran (not for uncertain outcomes: settle those)."""
        reservation = self._reservations.get(ref)
        if reservation and not reservation.settled:
            reservation.settled = True
            reservation.actual = 0.0

    def set_allocation(self, allocation: float, authorized_by: str) -> None:
        """Change the allocation. Requires an explicit authorizer; agents never call this."""
        if not authorized_by:
            raise ValueError("an allocation change requires an authorizer")
        self.allocation = float(allocation)

    @property
    def stats(self) -> dict:
        return {
            "name": self.name,
            "allocation": self.allocation,
            "spent": round(self.spent, 4),
            "reserved": round(self.reserved, 4),
            "available": round(self.available, 4),
            "reservations": len(self._reservations),
        }
