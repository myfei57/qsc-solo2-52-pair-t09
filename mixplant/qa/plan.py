"""The per grade sampling plan.

Every grade fixes two numbers:

* ``every_batches`` -- one sample is due each time this many completed
  batches of the grade have gone out.
* ``block_after_misses`` -- a grade is pressed (blocked) once that many of
  its due samples have been missed in a row.

The blocking threshold therefore lives on the grade, never on the shift and
never on the whole station: a pressed grade stops only that grade.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError

# Sampling cadence and blocking threshold for grades the plan does not name.
DEFAULT_EVERY_BATCHES = 1
DEFAULT_BLOCK_AFTER_MISSES = 2


@dataclass(frozen=True, slots=True)
class GradePlan:
    """The sampling frequency and block threshold of one grade."""

    grade: str
    every_batches: int = DEFAULT_EVERY_BATCHES
    block_after_misses: int = DEFAULT_BLOCK_AFTER_MISSES

    def __post_init__(self) -> None:
        if not self.grade:
            raise ValidationError("a grade plan needs a grade")
        if self.every_batches < 1:
            raise ValidationError("sampling frequency must be at least one batch", grade=self.grade)
        if self.block_after_misses < 1:
            raise ValidationError("a grade must block after at least one miss", grade=self.grade)

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "every_batches": self.every_batches,
            "block_after_misses": self.block_after_misses,
        }


# The frequencies the station ships with.  Key grades are inspected tighter
# (every batch) than an ordinary structural grade.
_DEFAULT_PLANS: Mapping[str, tuple[int, int]] = {
    "C20": (2, 2),
    "C30": (1, 2),
    "C40": (1, 1),
}


class SamplingPlan:
    """Hands out the grade plan, falling back to the station default."""

    __slots__ = ("_grades", "_default_every", "_default_block")

    def __init__(
        self,
        grades: Mapping[str, GradePlan],
        *,
        default_every_batches: int = DEFAULT_EVERY_BATCHES,
        default_block_after_misses: int = DEFAULT_BLOCK_AFTER_MISSES,
    ) -> None:
        if default_every_batches < 1:
            raise ValidationError("the default sampling frequency must be at least one batch")
        if default_block_after_misses < 1:
            raise ValidationError("the default block threshold must be at least one miss")
        self._grades = dict(grades)
        self._default_every = default_every_batches
        self._default_block = default_block_after_misses

    @classmethod
    def default(cls) -> "SamplingPlan":
        """Build the plan the station boots with."""

        grades = {
            code: GradePlan(
                grade=code,
                every_batches=every_batches,
                block_after_misses=block_after_misses,
            )
            for code, (every_batches, block_after_misses) in _DEFAULT_PLANS.items()
        }
        return cls(grades)

    @classmethod
    def from_mapping(cls, document: Mapping[str, Any] | None) -> "SamplingPlan":
        """Build a plan from a config document such as ``{"C30": {...}}``."""

        document = document or {}
        grades: dict[str, GradePlan] = {}
        for raw in document.get("grades", ()):
            if not isinstance(raw, Mapping):
                raise ValidationError("each grade plan must be an object")
            plan = GradePlan(
                grade=str(raw["grade"]),
                every_batches=int(raw.get("every_batches", DEFAULT_EVERY_BATCHES)),
                block_after_misses=int(raw.get("block_after_misses", DEFAULT_BLOCK_AFTER_MISSES)),
            )
            grades[plan.grade] = plan
        defaults = document.get("defaults") or {}
        return cls(
            grades,
            default_every_batches=int(defaults.get("every_batches", DEFAULT_EVERY_BATCHES)),
            default_block_after_misses=int(
                defaults.get("block_after_misses", DEFAULT_BLOCK_AFTER_MISSES)
            ),
        )

    def for_grade(self, grade: str) -> GradePlan:
        """Return the grade plan or a plan built from the station defaults."""

        known = self._grades.get(grade)
        if known is not None:
            return known
        return GradePlan(
            grade=grade,
            every_batches=self._default_every,
            block_after_misses=self._default_block,
        )

    def grades(self) -> tuple[str, ...]:
        return tuple(sorted(self._grades))

    def describe(self) -> dict[str, Any]:
        return {
            "default": {
                "every_batches": self._default_every,
                "block_after_misses": self._default_block,
            },
            "grades": [self._grades[code].describe() for code in sorted(self._grades)],
        }
