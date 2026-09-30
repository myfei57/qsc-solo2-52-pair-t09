"""The open sampling obligations the console prompts and the shift settles.

An obligation is a due batch that has not yet produced a valid sample.  It
is opened at declaration (the moment the prompt appears), closed by a valid
sample, and swept at a shift close where anything still open is a miss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import DuplicateError, NotFoundError, OrderViolationError, ValidationError

OPEN = "open"
MISSED = "missed"
SAMPLED = "sampled"


@dataclass(frozen=True, slots=True)
class Obligation:
    """One due sample point waiting on the laboratory."""

    batch_id: str
    grade: str
    opened_tick: int
    shift_id: str
    status: str = OPEN

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "opened_tick": self.opened_tick,
            "shift_id": self.shift_id,
            "status": self.status,
        }


class ObligationRegistry:
    """Keeps the due batches per grade until they are sampled or missed."""

    __slots__ = ("_by_batch", "_order")

    def __init__(self) -> None:
        self._by_batch: dict[str, Obligation] = {}
        self._order: list[str] = []

    def open(self, *, batch_id: str, grade: str, tick: int, shift_id: str) -> Obligation:
        """Register a newly due batch and refuse a reused identity."""

        if not batch_id:
            raise ValidationError("an obligation needs a batch identity")
        if batch_id in self._by_batch:
            raise DuplicateError("this batch already carries a sampling obligation", batch_id=batch_id)
        obligation = Obligation(batch_id=batch_id, grade=grade, opened_tick=tick, shift_id=shift_id)
        self._by_batch[batch_id] = obligation
        self._order.append(batch_id)
        return obligation

    def require_open(self, batch_id: str) -> Obligation:
        obligation = self.get(batch_id)
        if obligation is None:
            raise NotFoundError("this batch has no open sampling obligation", batch_id=batch_id)
        if obligation.status != OPEN:
            raise OrderViolationError(
                "this sampling obligation is already settled",
                batch_id=batch_id,
                status=obligation.status,
            )
        return obligation

    def get(self, batch_id: str) -> Obligation | None:
        return self._by_batch.get(batch_id)

    def mark_sampled(self, batch_id: str) -> Obligation:
        """Close an open obligation with a valid sample."""

        obligation = self.require_open(batch_id)
        sampled = Obligation(
            batch_id=obligation.batch_id,
            grade=obligation.grade,
            opened_tick=obligation.opened_tick,
            shift_id=obligation.shift_id,
            status=SAMPLED,
        )
        self._by_batch[batch_id] = sampled
        return sampled

    def open_for_grade(self, grade: str) -> tuple[str, ...]:
        return tuple(
            batch_id
            for batch_id in self._order
            if self._by_batch[batch_id].grade == grade and self._by_batch[batch_id].status == OPEN
        )

    def open_in_shift(self, shift_id: str) -> tuple[Obligation, ...]:
        return tuple(
            self._by_batch[batch_id]
            for batch_id in self._order
            if self._by_batch[batch_id].shift_id == shift_id
            and self._by_batch[batch_id].status == OPEN
        )

    def close_shift_misses(self, shift_id: str) -> tuple[Obligation, ...]:
        """Mark every obligation still open in the shift as missed."""

        settled: list[Obligation] = []
        for obligation in self.open_in_shift(shift_id):
            missed = Obligation(
                batch_id=obligation.batch_id,
                grade=obligation.grade,
                opened_tick=obligation.opened_tick,
                shift_id=obligation.shift_id,
                status=MISSED,
            )
            self._by_batch[obligation.batch_id] = missed
            settled.append(missed)
        return tuple(settled)

    def all(self) -> tuple[Obligation, ...]:
        return tuple(self._by_batch[batch_id] for batch_id in self._order)

    def prompt(self) -> tuple[Obligation, ...]:
        """The obligations the console must prompt for right now."""

        return tuple(self._by_batch[batch_id] for batch_id in self._order if self._by_batch[batch_id].status == OPEN)

    def adopt(self, obligation: Obligation) -> None:
        """Rebuild one obligation from the replayed stream."""

        self._by_batch[obligation.batch_id] = obligation
        if obligation.batch_id not in self._order:
            self._order.append(obligation.batch_id)
