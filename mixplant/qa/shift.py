"""The shift the handover report is written for."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import ValidationError


@dataclass(slots=True)
class Shift:
    """One handover window with the sampling totals accumulated inside it."""

    code: str
    started_tick: int
    due_opened: int = 0
    sampled_on_time: int = 0
    missed: int = 0
    pressed: int = 0
    closed_tick: int | None = None

    def is_open(self) -> bool:
        return self.closed_tick is None

    def close(self, tick: int) -> None:
        if self.closed_tick is not None:
            raise ValidationError("this shift is already closed", shift=self.code)
        self.closed_tick = tick

    def describe(self) -> dict[str, Any]:
        return {
            "active": self.is_open(),
            "shift": self.code,
            "started_tick": self.started_tick,
            "closed_tick": self.closed_tick,
            "due_opened": self.due_opened,
            "sampled_on_time": self.sampled_on_time,
            "missed": self.missed,
            "pressed": self.pressed,
        }


class ShiftBook:
    """Opens shifts, closes them and keeps the newest for the console."""

    __slots__ = ("_current", "_history", "_number")

    def __init__(self) -> None:
        self._current = Shift(code="shift-000001", started_tick=0)
        self._history: list[Shift] = []
        self._number = 1

    @property
    def current(self) -> Shift:
        return self._current

    def handover(self, tick: int) -> Shift:
        """Close the open shift and open the next one."""

        self._current.close(tick)
        closed = self._current
        self._history.append(closed)
        self._number += 1
        self._current = Shift(code=f"shift-{self._number:06d}", started_tick=tick)
        return closed

    def hydrate(self, *, code: str, started_tick: int, closed_tick: int | None) -> None:
        """Adopt the shift recorded in the stream after a restart."""

        number = int(code.split("-")[-1])
        self._number = max(self._number, number)
        shift = Shift(code=code, started_tick=started_tick, closed_tick=closed_tick)
        if closed_tick is None:
            self._current = shift
        else:
            self._history.append(shift)
            if number >= self._number:
                self._number = number + 1
                self._current = Shift(code=f"shift-{self._number:06d}", started_tick=closed_tick)

    def describe(self) -> dict[str, Any]:
        return {
            "current": self._current.describe(),
            "closed": [shift.describe() for shift in self._history],
        }
