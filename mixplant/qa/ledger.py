"""Per grade ledger of what is due, what was missed and whether it is pressed.

The ledger is the in memory working state.  Every transition it makes is also
written to the record stream by the service, which is what lets a restart
rebuild the exact same ledger and keeps the decision explainable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import OrderViolationError, ValidationError

DUE = "due"
SAMPLED = "sampled"
MISSED = "missed"


@dataclass(frozen=True, slots=True)
class DueItem:
    """One sample the laboratory is still waiting for."""

    batch_id: str
    grade: str
    position: int
    opened_tick: int
    deadline_tick: int
    window_ticks: int
    status: str = DUE
    closed_tick: int | None = None
    sample_id: str | None = None

    def __post_init__(self) -> None:
        if not self.batch_id:
            raise ValidationError("a due item needs a batch identity")
        if not self.grade:
            raise ValidationError("a due item needs a grade")
        if self.status not in (DUE, SAMPLED, MISSED):
            raise ValidationError("unknown due item status", status=self.status)

    @property
    def open(self) -> bool:
        return self.status == DUE

    def is_overdue(self, now: int) -> bool:
        """A due item is missed once the sampling window has elapsed."""

        return self.open and now > self.deadline_tick

    def close(self, *, status: str, tick: int, sample_id: str | None = None) -> "DueItem":
        if not self.open:
            raise OrderViolationError("the due item is already closed", batch_id=self.batch_id)
        return DueItem(
            batch_id=self.batch_id,
            grade=self.grade,
            position=self.position,
            opened_tick=self.opened_tick,
            deadline_tick=self.deadline_tick,
            window_ticks=self.window_ticks,
            status=status,
            closed_tick=tick,
            sample_id=sample_id,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "grade": self.grade,
            "position": self.position,
            "opened_tick": self.opened_tick,
            "deadline_tick": self.deadline_tick,
            "window_ticks": self.window_ticks,
            "status": self.status,
            "closed_tick": self.closed_tick,
            "sample_id": self.sample_id,
        }


@dataclass(slots=True)
class GradeState:
    """The running sampling position and streak of one grade."""

    grade: str
    every_batches: int
    block_after_misses: int
    completed: int = 0
    misses_in_a_row: int = 0
    blocked: bool = False
    blocked_tick: int | None = None
    released_tick: int | None = None
    open_item: DueItem | None = None
    history: list[DueItem] = field(default_factory=list)
    makeup_sample_ids: list[str] = field(default_factory=list)
    last_missed_batch: str | None = None

    def describe(self, now: int) -> dict[str, Any]:
        open_item = self.open_item
        return {
            "grade": self.grade,
            "every_batches": self.every_batches,
            "block_after_misses": self.block_after_misses,
            "completed": self.completed,
            "misses_in_a_row": self.misses_in_a_row,
            "blocked": self.blocked,
            "blocked_tick": self.blocked_tick,
            "released_tick": self.released_tick,
            "open": open_item is not None,
            "overdue": open_item.is_overdue(now) if open_item is not None else False,
            "due_item": None if open_item is None else open_item.describe(),
            "makeup_samples": list(self.makeup_sample_ids),
            "history": [item.describe() for item in self.history],
        }


class GradeLedger:
    """Keeps one :class:`GradeState` per grade the plant has produced."""

    __slots__ = ("_states",)

    def __init__(self) -> None:
        self._states: dict[str, GradeState] = {}

    def ensure(self, grade: str, *, every_batches: int, block_after_misses: int) -> GradeState:
        state = self._states.get(grade)
        if state is None:
            state = GradeState(
                grade=grade,
                every_batches=every_batches,
                block_after_misses=block_after_misses,
            )
            self._states[grade] = state
        return state

    def get(self, grade: str) -> GradeState | None:
        return self._states.get(grade)

    def require(self, grade: str) -> GradeState:
        state = self._states.get(grade)
        if state is None:
            raise ValidationError("this grade has not produced a batch yet", grade=grade)
        return state

    def grades(self) -> tuple[str, ...]:
        return tuple(sorted(self._states))

    def blocked_grades(self) -> tuple[str, ...]:
        return tuple(grade for grade, state in self._states.items() if state.blocked)

    def describe(self, now: int) -> dict[str, Any]:
        return {
            "blocked": self.blocked_grades(),
            "grades": [self._states[code].describe(now) for code in sorted(self._states)],
        }
