"""The per grade sampling counters and the due / missed / hold decisions.

The governor is deliberately free of infrastructure here: it only folds
domain events (a batch was declared, a due sample was taken, a shift was
closed) into counters and answers the three timing questions:

* is the freshly declared batch due for a sample,
* which due samples were still open when the shift closed,
* has the grade reached its hold threshold.

Keeping this pure is what lets a restart rebuild exactly the same decisions
by replaying the committed stream.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import NotFoundError, OrderViolationError, ValidationError
from .rules import GradeSamplingRule


@dataclass(frozen=True, slots=True)
class DueDecision:
    """Whether one declared batch is a sampling point for its grade."""

    due: bool
    grade: str
    batch_id: str
    batches_since_sample: int
    every_n_batches: int
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "due": self.due,
            "grade": self.grade,
            "batch_id": self.batch_id,
            "batches_since_sample": self.batches_since_sample,
            "every_n_batches": self.every_n_batches,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class MissedDecision:
    """One due batch that left the shift without a valid sample."""

    grade: str
    batch_id: str
    reason: str

    def describe(self) -> dict[str, Any]:
        return {"grade": self.grade, "batch_id": self.batch_id, "reason": self.reason}


@dataclass(frozen=True, slots=True)
class HoldDecision:
    """Whether a shift close holds the grade, and why."""

    hold: bool
    grade: str
    consecutive_misses: int
    threshold: int
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "hold": self.hold,
            "grade": self.grade,
            "consecutive_misses": self.consecutive_misses,
            "threshold": self.threshold,
            "reason": self.reason,
        }


class GradeSamplingState:
    """The counters of one grade.

    ``batches_since_sample`` counts declared batches since the last valid
    sample and drives due points.  ``consecutive_misses`` survives shift
    boundaries so an operator cannot dilute a run of misses by waiting for
    a handover; it clears only on a valid sample or an explicit release.
    """

    __slots__ = ("grade", "batches_since_sample", "consecutive_misses", "held")

    def __init__(self, grade: str) -> None:
        if not grade:
            raise ValidationError("grade sampling state needs a grade")
        self.grade = grade
        self.batches_since_sample = 0
        self.consecutive_misses = 0
        self.held = False

    def declare(self, batch_id: str, rule: GradeSamplingRule) -> DueDecision:
        """Account for a newly declared batch and decide if it is due.

        A held grade can never declare: the batch line is stopped at the
        source, so this state never advances while held.
        """

        if self.held:
            raise OrderViolationError(
                "grade is held for missed samples",
                grade=self.grade,
                batch_id=batch_id,
                consecutive_misses=self.consecutive_misses,
            )
        self.batches_since_sample += 1
        due = self.batches_since_sample % rule.every_n_batches == 0
        reason = (
            f"every {rule.every_n_batches} batches of {self.grade} need a sample"
            if due
            else f"next {self.grade} sample is due at batch {rule.every_n_batches - (self.batches_since_sample % rule.every_n_batches)} later"
        )
        return DueDecision(
            due=due,
            grade=self.grade,
            batch_id=batch_id,
            batches_since_sample=self.batches_since_sample,
            every_n_batches=rule.every_n_batches,
            reason=reason,
        )

    def is_next_due(self, rule: GradeSamplingRule) -> bool:
        """Whether the next declared batch lands on a sampling point."""

        if self.held:
            return False
        return (self.batches_since_sample + 1) % rule.every_n_batches == 0

    def sample_taken(self, batch_id: str) -> None:
        """A valid sample was filed: both counters return to a clean run."""

        self.batches_since_sample = 0
        self.consecutive_misses = 0

    def close_shift(self, rule: GradeSamplingRule, open_due: tuple[str, ...]) -> tuple[
        tuple[MissedDecision, ...],
        HoldDecision,
    ]:
        """Count the due batches still open and decide on holding."""

        missed = tuple(
            MissedDecision(
                grade=self.grade,
                batch_id=batch_id,
                reason="due sample was not taken before the shift closed",
            )
            for batch_id in open_due
        )
        self.consecutive_misses += len(missed)
        if self.consecutive_misses >= rule.block_after_misses:
            self.held = True
            decision = HoldDecision(
                hold=True,
                grade=self.grade,
                consecutive_misses=self.consecutive_misses,
                threshold=rule.block_after_misses,
                reason="consecutive missed samples reached the grade hold threshold",
            )
        else:
            decision = HoldDecision(
                hold=False,
                grade=self.grade,
                consecutive_misses=self.consecutive_misses,
                threshold=rule.block_after_misses,
                reason="consecutive missed samples are below the hold threshold",
            )
        return missed, decision

    def release(self, *, reason: str) -> HoldDecision:
        """Explicitly lift the hold and restart the miss counter."""

        if not self.held:
            raise NotFoundError("grade is not held", grade=self.grade)
        self.held = False
        self.consecutive_misses = 0
        return HoldDecision(
            hold=False,
            grade=self.grade,
            consecutive_misses=0,
            threshold=0,
            reason=reason,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "batches_since_sample": self.batches_since_sample,
            "consecutive_misses": self.consecutive_misses,
            "held": self.held,
        }


def is_due(decision: DueDecision) -> bool:
    return decision.due
