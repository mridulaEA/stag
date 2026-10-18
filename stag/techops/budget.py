"""Budget admission control: reserve before dispatch, settle afterward.

    Available allocation = approved allocation − recorded spend − outstanding reservations

A dashboard shows what happened. An admission controller decides whether more work may start.
Each retry is another possible cost even when it does not create another business action, and
an uncertain or timed-out request is reconciled rather than treated as free.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from pydantic import BaseModel, Field


class BudgetExhausted(Exception):
    """Raised when a reservation would exceed the available allocation."""


def _amount(value: float, what: str) -> float:
    """Validate a monetary/credit amount: finite, non-negative, numeric."""
    try:
        amount = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{what} must be numeric") from e
    if isinstance(value, bool) or not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{what} must be a finite non-negative number, got {value!r}")
    return amount


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
        self.name = name
        self.allocation = _amount(allocation, "allocation")
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
        amount = _amount(amount, "reservation amount")
        existing = self._reservations.get(ref)
        if existing is not None:
            if not existing.settled:
                return existing  # idempotent: retrying the reservation does not double-reserve
            raise ValueError(f"reservation '{ref}' is already settled; a new attempt needs a new ref")
        if amount > self.available:
            raise BudgetExhausted(
                f"budget '{self.name}': requested {amount:.2f}, available {self.available:.2f}"
            )
        reservation = Reservation(ref=ref, amount=amount)
        self._reservations[ref] = reservation
        return reservation

    def settle(self, ref: str, actual: float) -> Reservation:
        """Record actual usage and release the unused part of the reservation."""
        actual = _amount(actual, "settled usage")
        reservation = self._reservations[ref]
        if reservation.settled:
            return reservation  # idempotent settlement
        reservation.settled = True
        reservation.actual = actual
        self.spent += actual
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
        self.allocation = _amount(allocation, "allocation")

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
