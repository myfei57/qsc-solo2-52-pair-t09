"""Shift bookkeeping: open, hand over and close one crew at a time."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import NotFoundError, OrderViolationError, ValidationError

OPEN = "open"
CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class Shift:
    """One crew's production window."""

    shift_id: str
    crew: str
    opened_tick: int
    closed_tick: int | None = None
    status: str = OPEN

    def describe(self) -> dict[str, Any]:
        return {
            "shift_id": self.shift_id,
            "crew": self.crew,
            "opened_tick": self.opened_tick,
            "closed_tick": self.closed_tick,
            "status": self.status,
        }


class ShiftBook:
    """Holds exactly one open shift and keeps the closed ones for reports."""

    __slots__ = ("_shifts", "_open_id")

    def __init__(self) -> None:
        self._shifts: dict[str, Shift] = {}
        self._open_id: str | None = None

    def open_shift(self, *, shift_id: str, crew: str, tick: int) -> Shift:
        """Open a crew's shift, refusing to nest a second open shift."""

        if not shift_id:
            raise ValidationError("a shift needs an identity")
        if not crew:
            raise ValidationError("a shift needs a crew identity")
        if self._open_id is not None:
            raise OrderViolationError(
                "the previous shift must be handed over before a new one opens",
                open_shift=self._open_id,
            )
        if shift_id in self._shifts:
            raise ValidationError("shift identity was reused", shift_id=shift_id)
        shift = Shift(shift_id=shift_id, crew=crew, opened_tick=tick)
        self._shifts[shift_id] = shift
        self._open_id = shift_id
        return shift

    def require_open(self) -> Shift:
        if self._open_id is None:
            raise NotFoundError("no shift is open")
        return self._shifts[self._open_id]

    def close(self, *, tick: int) -> Shift:
        """Hand over the open shift. It may not close twice."""

        current = self.require_open()
        closed = Shift(
            shift_id=current.shift_id,
            crew=current.crew,
            opened_tick=current.opened_tick,
            closed_tick=tick,
            status=CLOSED,
        )
        self._shifts[current.shift_id] = closed
        self._open_id = None
        return closed

    def current_id(self) -> str | None:
        return self._open_id

    def all(self) -> tuple[Shift, ...]:
        return tuple(self._shifts.values())

    def adopt(self, shift: Shift) -> None:
        """Rebuild a shift from the replayed stream."""

        self._shifts[shift.shift_id] = shift
        if shift.status == OPEN:
            self._open_id = shift.shift_id
