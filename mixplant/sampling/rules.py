"""Sampling frequency and hold thresholds, configured per grade."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError


@dataclass(frozen=True, slots=True)
class GradeSamplingRule:
    """How often one grade must be sampled and when missing it holds the grade.

    ``every_n_batches`` is the sampling frequency: one sample is due every
    time this many batches of the grade have been produced.
    ``block_after_misses`` is the number of consecutive missed due samples at
    which the grade is held so no further batch of it can be declared.
    """

    grade: str
    every_n_batches: int
    block_after_misses: int

    def __post_init__(self) -> None:
        if not self.grade:
            raise ValidationError("a sampling rule needs a grade")
        if self.every_n_batches < 1:
            raise ValidationError("sampling frequency must be at least one", grade=self.grade)
        if self.block_after_misses < 1:
            raise ValidationError("hold threshold must be at least one", grade=self.grade)

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "every_n_batches": self.every_n_batches,
            "block_after_misses": self.block_after_misses,
        }


class SamplingRuleBook:
    """The per grade frequency table, with a fallback for unlisted grades."""

    __slots__ = ("_rules", "_fallback")

    def __init__(
        self,
        rules: Mapping[str, GradeSamplingRule] | tuple[GradeSamplingRule, ...],
        *,
        fallback: GradeSamplingRule,
    ) -> None:
        if isinstance(rules, Mapping):
            by_grade = dict(rules)
        else:
            by_grade = {rule.grade: rule for rule in rules}
        for code, rule in by_grade.items():
            if rule.grade != code:
                raise ValidationError("sampling rule is filed under the wrong grade", grade=code)
        self._rules = dict(sorted(by_grade.items()))
        self._fallback = fallback

    @classmethod
    def default(cls) -> "SamplingRuleBook":
        """A conservative table covering the catalogue grades.

        Every batch is due by default; the hold engages after two missed
        samples so one slip never stops production unnoticed.
        """

        return cls(
            (
                GradeSamplingRule("C20", every_n_batches=2, block_after_misses=2),
                GradeSamplingRule("C30", every_n_batches=1, block_after_misses=2),
                GradeSamplingRule("C40", every_n_batches=1, block_after_misses=1),
            ),
            fallback=GradeSamplingRule("*", every_n_batches=1, block_after_misses=1),
        )

    def rule_for(self, grade: str) -> GradeSamplingRule:
        """Return the rule of ``grade`` or the fallback for an unlisted grade."""

        if not grade:
            raise ValidationError("a grade is needed to look up its sampling rule")
        return self._rules.get(grade, self._fallback)

    def describe(self) -> dict[str, Any]:
        return {
            "rules": [self._rules[code].describe() for code in sorted(self._rules)],
            "fallback": self._fallback.describe(),
        }
