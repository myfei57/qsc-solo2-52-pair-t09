"""The decisions behind reminders, misses and grade blocks.

These functions are deliberately free of the event bus and the record
stream.  They take a ledger and return what changed so the service can
publish the matching records; this keeps every decision unit testable and
keeps the audit basis identical on a replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import InterlockBlockedError, OrderViolationError, ValidationError
from .ledger import MISSED, SAMPLED, DueItem, GradeLedger, GradeState
from .plan import GradePlan

ON_TIME = "on_time"
MAKEUP = "makeup"
EXTRA = "extra"


@dataclass(frozen=True, slots=True)
class SampleVerdict:
    """What filing a sample did to a grade."""

    kind: str
    grade: str
    batch_id: str
    sample_id: str

    @property
    def clears_due(self) -> bool:
        return self.kind == ON_TIME

    def describe(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "grade": self.grade,
            "batch_id": self.batch_id,
            "sample_id": self.sample_id,
        }


def register_completion(
    *,
    ledger: GradeLedger,
    plan: GradePlan,
    batch_id: str,
    tick: int,
    window_ticks: int,
) -> DueItem | None:
    """Count one completed batch and open a due item when the cadence is hit.

    A grade only ever has one open item.  Completions that arrive while an
    earlier item is still open advance the position but cannot queue another
    reminder; the open item is what the sweep can miss.
    """

    state = ledger.ensure(
        plan.grade,
        every_batches=plan.every_batches,
        block_after_misses=plan.block_after_misses,
    )
    state.completed += 1
    if state.open_item is not None:
        return None
    if state.completed % plan.every_batches != 0:
        return None
    item = DueItem(
        batch_id=batch_id,
        grade=plan.grade,
        position=state.completed,
        opened_tick=tick,
        deadline_tick=tick + window_ticks,
        window_ticks=window_ticks,
    )
    state.open_item = item
    return item


def file_sample(
    *,
    ledger: GradeLedger,
    grade: str,
    batch_id: str,
    sample_id: str,
    tick: int,
) -> SampleVerdict:
    """Apply one filed laboratory sample to a grade.

    * a sample for the open batch within its window clears the item;
    * a sample for the open batch after its window is a makeup sample -- it
      is recorded but the miss already stands;
    * anything else is an extra sample and changes nothing.
    """

    state = ledger.require(grade)
    item = state.open_item
    if item is not None and item.batch_id == batch_id:
        if tick <= item.deadline_tick:
            state.open_item = item.close(status=SAMPLED, tick=tick, sample_id=sample_id)
            state.history.append(state.open_item)
            state.misses_in_a_row = 0
            state.last_missed_batch = None
            state.open_item = None
            return SampleVerdict(kind=ON_TIME, grade=grade, batch_id=batch_id, sample_id=sample_id)
        state.open_item = item.close(status=MISSED, tick=tick, sample_id=None)
        state.history.append(state.open_item)
        state.open_item = None
        state.last_missed_batch = batch_id
    if batch_id == state.last_missed_batch:
        state.makeup_sample_ids.append(sample_id)
        return SampleVerdict(kind=MAKEUP, grade=grade, batch_id=batch_id, sample_id=sample_id)
    return SampleVerdict(kind=EXTRA, grade=grade, batch_id=batch_id, sample_id=sample_id)


def sweep_grade(*, state: GradeState, now: int) -> DueItem | None:
    """Close the open item of one grade when its sampling window elapsed.

    Returns the item that just became a miss, or ``None`` when nothing
    changed.  The streak is advanced here, in the same transition the press
    decision is made from, so the two can never disagree.
    """

    item = state.open_item
    if item is None or not item.is_overdue(now):
        return None
    missed = item.close(status=MISSED, tick=now)
    state.open_item = None
    state.last_missed_batch = missed.batch_id
    state.history.append(missed)
    state.misses_in_a_row += 1
    return missed


def should_press(state: GradeState) -> bool:
    """A grade is pressed at its configured consecutive miss threshold."""

    return not state.blocked and state.misses_in_a_row >= state.block_after_misses


def press(*, state: GradeState, tick: int) -> GradeState:
    """Press a grade once; a second press while pressed is an order error."""

    if state.blocked:
        raise OrderViolationError("the grade is already pressed", grade=state.grade)
    state.blocked = True
    state.blocked_tick = tick
    state.released_tick = None
    state.makeup_sample_ids = []
    return state


def release(*, state: GradeState, tick: int) -> GradeState:
    """Release a pressed grade after a makeup sample was filed."""

    if not state.blocked:
        raise OrderViolationError("the grade is not pressed", grade=state.grade)
    if not state.makeup_sample_ids:
        raise ValidationError(
            "a pressed grade needs a makeup sample before it can be released",
            grade=state.grade,
        )
    state.blocked = False
    state.released_tick = tick
    state.misses_in_a_row = 0
    state.makeup_sample_ids = []
    return state


def assert_not_pressed(ledger: GradeLedger, grade: str) -> None:
    """Refuse new production for a pressed grade."""

    state = ledger.get(grade)
    if state is not None and state.blocked:
        raise InterlockBlockedError(
            "this grade is pressed: its missed samples must be made up first",
            grade=grade,
            blocked_tick=state.blocked_tick,
            misses_in_a_row=state.misses_in_a_row,
        )


def adopt_completion(
    *,
    ledger: GradeLedger,
    grade: str,
    every_batches: int,
    block_after_misses: int,
    completed: int,
) -> GradeState:
    """Adopt a counted completion read back from the stream.

    Unlike :func:`register_completion` this never decides that a sample is
    due; the due items are replayed from their own ``qa.due`` records.
    """

    state = ledger.ensure(
        grade,
        every_batches=every_batches,
        block_after_misses=block_after_misses,
    )
    state.completed = max(state.completed, completed)
    return state


def adopt_open_item(*, ledger: GradeLedger, item: DueItem) -> None:
    """Adopt a still open reminder read back from the stream."""

    state = ledger.require(item.grade)
    state.open_item = item


def adopt_sampled(*, ledger: GradeLedger, grade: str, batch_id: str, sample_id: str, tick: int) -> None:
    """Adopt an on time sample read back from the stream."""

    state = ledger.require(grade)
    open_item = state.open_item
    if open_item is not None and open_item.batch_id == batch_id:
        state.history.append(open_item.close(status=SAMPLED, tick=tick, sample_id=sample_id))
        state.open_item = None
    state.misses_in_a_row = 0


def adopt_miss(*, ledger: GradeLedger, grade: str, item: DueItem, misses_in_a_row: int) -> None:
    """Adopt a miss read back from the stream."""

    state = ledger.require(grade)
    if state.open_item is not None and state.open_item.batch_id == item.batch_id:
        state.open_item = None
    state.history.append(item)
    state.misses_in_a_row = max(state.misses_in_a_row, misses_in_a_row)


def adopt_makeup(*, ledger: GradeLedger, grade: str, sample_id: str) -> None:
    """Adopt a makeup sample read back from the stream."""

    ledger.require(grade).makeup_sample_ids.append(sample_id)


def adopt_press(*, ledger: GradeLedger, grade: str, tick: int, misses_in_a_row: int) -> None:
    """Adopt a press read back from the stream."""

    state = ledger.require(grade)
    state.blocked = True
    state.blocked_tick = tick
    state.misses_in_a_row = max(state.misses_in_a_row, misses_in_a_row)
    state.makeup_sample_ids = []


def adopt_release(*, ledger: GradeLedger, grade: str, tick: int) -> None:
    """Adopt a release read back from the stream."""

    state = ledger.require(grade)
    state.blocked = False
    state.released_tick = tick
    state.misses_in_a_row = 0
    state.makeup_sample_ids = []
