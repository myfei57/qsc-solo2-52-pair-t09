"""The sampling governor: per grade frequency, prompts, misses and holds.

This is the governance layer sitting beside the execution layer
(``SamplerService``).  It owns the timing rules in one place and exposes the
single source of truth the batch line, the console and the shift ledger
read:

* ``enroll`` is called the moment a batch is declared.  A held grade is
  refused here, which stops further production of that grade at its source
  while other grades keep running.
* ``record_sample`` is called only on a valid sample drawn while the
  sampler device is online.  An offline draw is a partial failure and never
  reaches this method.
* ``handover`` settles the open shift: still open obligations become
  misses, consecutive misses reaching the grade threshold hold the grade.
* ``release_hold`` is the only way out of a hold and needs an explicit,
  audited authorisation.

Every transition is published to the committed stream and raised on the
bus, so the audit trail and restart recovery both follow from it.
"""

from __future__ import annotations

from typing import Any

from ..errors import OrderViolationError, PartialFailureError
from ..service_base import PlantService
from .kinds import DEVICE_KIND, GRADE_KIND, HOLD_KIND, OBLIGATION_KIND, REPORT_KIND, SHIFT_KIND
from .obligations import ObligationRegistry
from .rules import SamplingRuleBook
from .shift import ShiftBook
from .tracking import GradeSamplingState

DEVICE_NAME = "sampler-1"
DEVICE_ONLINE = "online"
DEVICE_OFFLINE = "offline"


class SamplingGovernor(PlantService):
    """Owns the per grade sampling counters, obligations and holds."""

    origin = "sampler"
    line = "batch"
    subject = "sampling"

    def __init__(self, context, rules: SamplingRuleBook | None = None, machine=None) -> None:
        super().__init__(context, machine)
        self.rules = rules or SamplingRuleBook.default()
        self.grades: dict[str, GradeSamplingState] = {}
        self.obligations = ObligationRegistry()
        self.shifts = ShiftBook()
        self.device_online = True
        self._last_report: dict[str, Any] | None = None

    # -- shifts -----------------------------------------------------------

    def open_shift(self, *, shift_id: str, crew: str) -> dict[str, Any]:
        """Open the crew shift that prompts and misses are booked against."""

        tick = self.context.clock.now()
        shift = self.shifts.open_shift(shift_id=shift_id, crew=crew, tick=tick)
        self.publish(SHIFT_KIND, {"active": True, **shift.describe()})
        self.emit("sampling.shift_opened", shift.describe())
        return {"shift": shift.describe()}

    def handover(self) -> dict[str, Any]:
        """Settle the open shift: point out misses and hold graded at limit.

        Misses and holds take effect at the same instant so the timing can
        never be observed out of order.
        """

        tick = self.context.clock.now()
        shift = self.shifts.require_open()
        settled = self.obligations.close_shift_misses(shift.shift_id)

        misses_by_grade: dict[str, list[str]] = {}
        for obligation in settled:
            misses_by_grade.setdefault(obligation.grade, []).append(obligation.batch_id)

        grade_reports: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        for grade in sorted(misses_by_grade):
            state = self._grade(grade)
            rule = self.rules.rule_for(grade)
            missed, decision = state.close_shift(rule, tuple(misses_by_grade[grade]))
            self.publish(GRADE_KIND, {"active": True, **state.describe()})
            for item in missed:
                obligation = self.obligations.get(item.batch_id)
                self.publish(
                    OBLIGATION_KIND,
                    {
                        "active": True,
                        **(obligation.describe() if obligation is not None else {"batch_id": item.batch_id, "grade": grade}),
                        "tick": tick,
                    },
                )
            if decision.hold:
                held_payload = {
                    "active": True,
                    "grade": grade,
                    "consecutive_misses": decision.consecutive_misses,
                    "threshold": decision.threshold,
                    "reason": decision.reason,
                    "tick": tick,
                }
                self.publish(HOLD_KIND, held_payload)
                self.raise_alarm("sampling.grade_held", {"grade": grade, **held_payload})
                held.append(held_payload)
            grade_reports.append(decision.describe())

        closed = self.shifts.close(tick=tick)
        self.publish(SHIFT_KIND, {"active": False, **closed.describe()})

        report = {
            "active": True,
            "shift_id": shift.shift_id,
            "crew": shift.crew,
            "tick": tick,
            "missed": [item.describe() for item in settled],
            "grades": grade_reports,
            "held": held,
            "still_open": [item.describe() for item in self.obligations.prompt()],
        }
        self._last_report = report
        self.publish(REPORT_KIND, report)
        self.emit(
            "sampling.shift_handed_over",
            {"shift_id": shift.shift_id, "missed": report["missed"], "held": [h["grade"] for h in held]},
        )
        return {"shift": closed.describe(), "report": report}

    # -- enrollment and sampling -----------------------------------------

    def enroll(self, *, batch_id: str, grade: str) -> dict[str, Any]:
        """Account for a declared batch and open an obligation when due.

        A held grade is refused before the batch line can proceed.
        """

        tick = self.context.clock.now()
        shift_id = self.shifts.current_id() or ""
        rule = self.rules.rule_for(grade)
        state = self._grade(grade)
        due_next = state.is_next_due(rule)
        # Reserve the obligation before touching the counters so a reused
        # batch identity fails without leaving a partial frequency advance.
        obligation = (
            self.obligations.open(batch_id=batch_id, grade=grade, tick=tick, shift_id=shift_id)
            if due_next
            else None
        )
        decision = state.declare(batch_id, rule)
        self.publish(GRADE_KIND, {"active": True, **state.describe()})
        payload: dict[str, Any] = {"decision": decision.describe()}
        if obligation is not None:
            self.publish(OBLIGATION_KIND, {"active": True, **obligation.describe()})
            self.emit(
                "sampling.sample_due",
                {"batch_id": batch_id, "grade": grade, "shift_id": shift_id, **decision.describe()},
            )
            payload["obligation"] = obligation.describe()
        return payload

    def record_sample(self, *, batch_id: str, grade: str) -> dict[str, Any] | None:
        """Close a due obligation after a draw.

        The execution layer refuses a draw while the sampler device is
        offline, which is what enforces "an outage never counts as sampled".
        A sample for a non due batch satisfies no obligation, so it leaves
        the frequency counters untouched.
        """

        obligation = self.obligations.get(batch_id)
        if obligation is None:
            return None
        if obligation.status == "open":
            sampled = self.obligations.mark_sampled(batch_id)
        else:
            # The obligation is already settled (sampled or counted as a
            # miss at a handover): a late sample cannot rewrite history.
            raise OrderViolationError(
                "the sampling obligation for this batch is already settled",
                batch_id=batch_id,
                status=obligation.status,
            )
        state = self._grade(grade)
        state.sample_taken(batch_id)
        tick = self.context.clock.now()
        self.publish(
            OBLIGATION_KIND,
            {"active": True, **obligation_payload(sampled, tick)},
        )
        self.publish(GRADE_KIND, {"active": True, **state.describe()})
        self.emit(
            "sampling.sample_recorded",
            {"batch_id": batch_id, "grade": grade, "consecutive_misses": 0},
        )
        return {"obligation": obligation_payload(sampled, tick), "grade": state.describe()}

    # -- holds ------------------------------------------------------------

    def release_hold(self, *, grade: str, reason: str) -> dict[str, Any]:
        """Explicitly lift a grade hold. Only a set hold can be released."""

        state = self._grade(grade)
        decision = state.release(reason=reason)
        tick = self.context.clock.now()
        payload = {
            "active": False,
            "grade": grade,
            "consecutive_misses": 0,
            "reason": reason,
            "tick": tick,
        }
        self.publish(HOLD_KIND, payload)
        self.publish(GRADE_KIND, {"active": True, **state.describe()})
        self.emit("sampling.hold_released", {"grade": grade, "reason": reason})
        return {"hold": payload, "decision": decision.describe()}

    # -- device presence --------------------------------------------------

    def device_connected(self) -> None:
        """Bring the sampler online."""

        if self.device_online:
            raise OrderViolationError("the sampling device is already online")
        self.device_online = True
        self._publish_device(DEVICE_ONLINE, "sampling device recovered")
        self.emit("sampling.device_online", {"device": DEVICE_NAME})

    def device_disconnected(self) -> None:
        """Take the sampler offline; pending draws must not count."""

        if not self.device_online:
            raise OrderViolationError("the sampling device is already offline")
        self.device_online = False
        self._publish_device(DEVICE_OFFLINE, "sampling device dropped out")
        self.raise_alarm(
            "sampling.device_offline",
            {"device": DEVICE_NAME, "message": "draws during the outage count as not sampled"},
        )
        self.emit("sampling.device_offline", {"device": DEVICE_NAME})

    def require_device_online(self) -> None:
        """Guard used by the draw path: an outage is a partial failure."""

        if not self.device_online:
            raise PartialFailureError(
                "the sampling device is offline; the draw is not counted",
                device=DEVICE_NAME,
            )

    def _publish_device(self, status: str, reason: str) -> None:
        self.publish(
            DEVICE_KIND,
            {
                "active": True,
                "device": DEVICE_NAME,
                "status": status,
                "online": status == DEVICE_ONLINE,
                "reason": reason,
            },
        )

    # -- recovery ---------------------------------------------------------

    def adopt_obligation(self, document: dict[str, Any]) -> None:
        """Rebuild an obligation from a replayed ``sampling.obligation``."""

        from .obligations import Obligation

        self.obligations.adopt(
            Obligation(
                batch_id=str(document["batch_id"]),
                grade=str(document["grade"]),
                opened_tick=int(document.get("opened_tick", 0)),
                shift_id=str(document.get("shift_id", "")),
                status=str(document.get("status", "open")),
            )
        )

    def adopt_grade(self, document: dict[str, Any]) -> None:
        """Rebuild a grade's counters from a replayed ``sampling.grade``."""

        state = self._grade(str(document["grade"]))
        state.batches_since_sample = int(document.get("batches_since_sample", 0))
        state.consecutive_misses = int(document.get("consecutive_misses", 0))
        state.held = bool(document.get("held", False))

    def adopt_shift(self, document: dict[str, Any]) -> None:
        """Rebuild a shift from a replayed ``sampling.shift``."""

        from .shift import Shift

        self.shifts.adopt(
            Shift(
                shift_id=str(document["shift_id"]),
                crew=str(document.get("crew", "")),
                opened_tick=int(document.get("opened_tick", 0)),
                closed_tick=document.get("closed_tick"),
                status=str(document.get("status", "open")),
            )
        )

    def adopt_device(self, document: dict[str, Any]) -> None:
        self.device_online = bool(document.get("online", True))

    # -- status -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "device": {
                "name": DEVICE_NAME,
                "online": self.device_online,
                "status": DEVICE_ONLINE if self.device_online else DEVICE_OFFLINE,
            },
            "shift": None if self.shifts.current_id() is None else self.shifts.require_open().describe(),
            "rules": self.rules.describe(),
            "grades": [self._grade(code).describe() for code in sorted(self.grades)],
            "held": [state.grade for state in self.grades.values() if state.held],
            "prompt": [item.describe() for item in self.obligations.prompt()],
            "obligations": [item.describe() for item in self.obligations.all()],
            "last_report": None if self._last_report is None else dict(self._last_report),
        }

    def _grade(self, grade: str) -> GradeSamplingState:
        state = self.grades.get(grade)
        if state is None:
            state = GradeSamplingState(grade)
            self.grades[grade] = state
        return state


def obligation_payload(obligation: Any, tick: int) -> dict[str, Any]:
    document = obligation.describe()
    document.setdefault("tick", tick)
    return document
