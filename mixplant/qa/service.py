"""Quality assurance service.

It owns the per grade sampling cadence, the reminders the console shows, the
miss accounting at handover and the press that stops one grade (and only
that grade) from continuing to pour.

Every transition publishes a record carrying the basis it was decided from
and emits a domain event, which the audit subscriber copies into the stream;
the alarm topic is raised for the conditions the console must highlight.
"""

from __future__ import annotations

from typing import Any

from ..errors import InterlockBlockedError
from ..service_base import PlantService
from .decide import (
    EXTRA,
    MAKEUP,
    ON_TIME,
    SampleVerdict,
    adopt_completion,
    adopt_makeup,
    adopt_miss,
    adopt_open_item,
    adopt_press,
    adopt_release,
    adopt_sampled,
    assert_not_pressed,
    file_sample,
    press,
    register_completion,
    release,
    sweep_grade,
)
from .ledger import DueItem, GradeLedger
from .plan import SamplingPlan
from .qa import (
    ATTEMPT_KIND,
    BLOCK_KIND,
    COMPLETION_KIND,
    DEVICE_KIND,
    DUE_KIND,
    MISS_KIND,
    SAMPLE_KIND,
    SAMPLER_DEVICE,
    SHIFT_KIND,
)
from .shift import ShiftBook


class QAService(PlantService):
    """Drives sampling reminders, miss accounting and grade press control."""

    origin = "qa"
    line = "batch"
    subject = "qa"

    def __init__(self, context, plan: SamplingPlan | None = None, machine=None) -> None:
        super().__init__(context, machine)
        self.plan = plan or SamplingPlan.default()
        self.ledger = GradeLedger()
        self.shifts = ShiftBook()
        self._sampler_online = True
        self._sampler_since = 0

    # -- production entry points -----------------------------------------

    def require_grade_allowed(self, grade: str) -> None:
        """Refuse a new batch for a pressed grade."""

        assert_not_pressed(self.ledger, grade)

    def batch_completed(self, *, batch_id: str, grade: str) -> dict[str, Any]:
        """Account a completed load and open a reminder when one is due."""

        state_plan = self.plan.for_grade(grade)
        now = self.context.clock.now()
        window = self.context.config.limits.sample_window_ticks
        state = self.ledger.ensure(
            grade,
            every_batches=state_plan.every_batches,
            block_after_misses=state_plan.block_after_misses,
        )
        self.publish(
            COMPLETION_KIND,
            {
                "active": True,
                "batch_id": batch_id,
                "grade": grade,
                "completed": state.completed + 1,
                "every_batches": state_plan.every_batches,
                "block_after_misses": state_plan.block_after_misses,
                "window_ticks": window,
            },
        )
        item = register_completion(
            ledger=self.ledger,
            plan=state_plan,
            batch_id=batch_id,
            tick=now,
            window_ticks=window,
        )
        if item is None:
            return {"due": False, "grade": grade}
        self.shifts.current.due_opened += 1
        self._publish_due(item)
        self.emit("qa.sample_due", item.describe())
        return {"due": True, "grade": grade, "due_item": item.describe()}

    # -- the time based sweep --------------------------------------------

    def sweep(self) -> dict[str, Any]:
        """Close every overdue window, press grades that reached their limit.

        Idempotent: a sweep with no overdue windows changes nothing and a
        grade that is already pressed is not pressed twice.
        """

        now = self.context.clock.now()
        missed: list[dict[str, Any]] = []
        pressed: list[dict[str, Any]] = []
        for grade in self.ledger.grades():
            state = self.ledger.require(grade)
            item = sweep_grade(state=state, now=now)
            if item is None:
                continue
            self.shifts.current.missed += 1
            self._publish_miss(item, state)
            self.emit("qa.sample_missed", item.describe())
            self.raise_alarm("qa.sample_missed", item.describe())
            missed.append(item.describe())
            if state.misses_in_a_row >= state.block_after_misses and not state.blocked:
                press(state=state, tick=now)
                self.shifts.current.pressed += 1
                basis = self._block_basis(grade, state)
                self.publish(BLOCK_KIND, basis)
                self.emit("qa.grade_pressed", basis)
                self.raise_alarm("qa.grade_pressed", basis)
                pressed.append(basis)
        return {"tick": now, "missed": missed, "pressed": pressed}

    # -- the laboratory side ---------------------------------------------

    def sample_filed(self, *, batch_id: str, grade: str, sample_id: str) -> SampleVerdict:
        """Apply a filed sample and publish what it did to the grade."""

        now = self.context.clock.now()
        verdict = file_sample(
            ledger=self.ledger,
            grade=grade,
            batch_id=batch_id,
            sample_id=sample_id,
            tick=now,
        )
        payload = {"active": True, **verdict.describe(), "tick": now}
        self.publish(SAMPLE_KIND, payload)
        if verdict.kind == ON_TIME:
            self.shifts.current.sampled_on_time += 1
            self.emit("qa.due_cleared", verdict.describe())
        elif verdict.kind == MAKEUP:
            self.emit("qa.makeup_filed", verdict.describe())
        return verdict

    def release_grade(self, grade: str) -> dict[str, Any]:
        """Release a pressed grade after its makeup sample was filed."""

        state = self.ledger.require(grade)
        release(state=state, tick=self.context.clock.now())
        payload = {
            "active": False,
            "grade": grade,
            "released_tick": state.released_tick,
            "makeup_samples": list(state.makeup_sample_ids),
        }
        self.publish(BLOCK_KIND, payload)
        self.emit("qa.grade_released", {"grade": grade, **payload})
        return {"grade": grade, "blocked": False}

    # -- handover ---------------------------------------------------------

    def handover(self) -> dict[str, Any]:
        """Sweep one last time, point out every miss and open the next shift."""

        swept = self.sweep()
        tick = self.context.clock.now()
        closed = self.shifts.handover(tick)
        missed_grades = self._missed_grades()
        payload = {
            "active": False,
            **closed.describe(),
            "missed_grades": missed_grades,
            "blocked_grades": list(self.ledger.blocked_grades()),
            "sampler_online": self._sampler_online,
        }
        self.publish(SHIFT_KIND, payload)
        self.emit("qa.shift_handover", payload)
        if missed_grades or payload["blocked_grades"]:
            self.raise_alarm("qa.handover_with_misses", payload)
        return {"closed": payload, "next": self.shifts.current.describe(), "sweep": swept}

    # -- sampler device health -------------------------------------------

    def sampler_online(self) -> bool:
        return self._sampler_online

    def set_sampler_online(self, online: bool) -> dict[str, Any]:
        """Record a sampler connect or disconnect.

        A disconnect is a partial failure: while it stands a draw cannot be
        accepted, and nothing offline can ever be counted as sampled.
        """

        if online == self._sampler_online:
            return {"online": online, "changed": False}
        now = self.context.clock.now()
        self._sampler_online = online
        self._sampler_since = now
        payload = {
            "active": True,
            "online": online,
            "device": SAMPLER_DEVICE,
            "since": now,
        }
        self.publish(DEVICE_KIND, payload)
        name = "qa.sampler_online" if online else "qa.sampler_offline"
        self.emit(name, payload)
        if not online:
            self.raise_alarm("qa.sampler_offline", payload)
        return {"online": online, "changed": True}

    def require_sampler_online(self, *, batch_id: str) -> None:
        """Refuse a draw against an offline sampler and log the failed try."""

        if self._sampler_online:
            return
        payload = {
            "active": True,
            "device": SAMPLER_DEVICE,
            "batch_id": batch_id,
            "tick": self.context.clock.now(),
            "counted_as": "unsampled",
        }
        self.publish(ATTEMPT_KIND, payload)
        self.emit("qa.draw_while_offline", payload)
        self.raise_alarm("qa.draw_while_offline", payload)
        raise InterlockBlockedError(
            "the sampling device is offline; the batch stays unsampled",
            device=SAMPLER_DEVICE,
            batch_id=batch_id,
        )

    # -- restart hydration -----------------------------------------------

    def adopt_record(self, kind: str, payload: dict[str, Any]) -> None:
        """Rebuild the ledger and shift state from one stored record."""

        now = self.context.clock.now()
        window = self.context.config.limits.sample_window_ticks
        if kind == COMPLETION_KIND:
            adopt_completion(
                ledger=self.ledger,
                grade=str(payload["grade"]),
                every_batches=int(payload.get("every_batches", 1)),
                block_after_misses=int(payload.get("block_after_misses", 1)),
                completed=int(payload.get("completed", 1)),
            )
        elif kind == DUE_KIND and payload.get("status", "due") == "due":
            adopt_open_item(ledger=self.ledger, item=self._due_from_payload(payload, now, window))
        elif kind == SAMPLE_KIND:
            self._adopt_sample(payload, now)
        elif kind == MISS_KIND:
            self._adopt_miss(payload, now, window)
        elif kind == BLOCK_KIND:
            self._adopt_block(payload)
        elif kind == DEVICE_KIND:
            self._sampler_online = bool(payload.get("online", True))
            self._sampler_since = int(payload.get("since", 0))
        elif kind == SHIFT_KIND:
            self.shifts.hydrate(
                code=str(payload["shift"]),
                started_tick=int(payload.get("started_tick", 0)),
                closed_tick=payload.get("closed_tick"),
            )

    # -- read side --------------------------------------------------------

    def due_items(self) -> list[dict[str, Any]]:
        now = self.context.clock.now()
        items: list[dict[str, Any]] = []
        for grade in self.ledger.grades():
            state = self.ledger.require(grade)
            if state.open_item is not None:
                items.append(state.open_item.describe())
        return items

    def status(self) -> dict[str, Any]:
        now = self.context.clock.now()
        return {
            "plan": self.plan.describe(),
            "sampler": {
                "device": SAMPLER_DEVICE,
                "online": self._sampler_online,
                "since": self._sampler_since,
            },
            "shift": self.shifts.current.describe(),
            "ledger": self.ledger.describe(now),
            "due": self.due_items(),
        }

    # -- internals --------------------------------------------------------

    def _publish_due(self, item: DueItem) -> None:
        self.publish(DUE_KIND, {"active": True, **item.describe()})

    def _publish_miss(self, item: DueItem, state: Any) -> None:
        self.publish(
            MISS_KIND,
            {
                "active": True,
                **item.describe(),
                "misses_in_a_row": state.misses_in_a_row,
                "block_after_misses": state.block_after_misses,
            },
        )

    def _block_basis(self, grade: str, state: Any) -> dict[str, Any]:
        return {
            "active": True,
            "grade": grade,
            "blocked": True,
            "blocked_tick": state.blocked_tick,
            "misses_in_a_row": state.misses_in_a_row,
            "block_after_misses": state.block_after_misses,
            "basis": [item.describe() for item in state.history[-state.block_after_misses :]],
        }

    def _missed_grades(self) -> list[dict[str, Any]]:
        now = self.context.clock.now()
        rows: list[dict[str, Any]] = []
        for grade in self.ledger.grades():
            state = self.ledger.require(grade)
            misses = [item for item in state.history if item.status == "missed"]
            if misses:
                rows.append(
                    {
                        "grade": grade,
                        "misses_in_a_row": state.misses_in_a_row,
                        "blocked": state.blocked,
                        "overdue": state.open_item is not None and state.open_item.is_overdue(now),
                    }
                )
        return rows

    def _due_from_payload(self, payload: dict[str, Any], now: int, window: int) -> DueItem:
        return DueItem(
            batch_id=str(payload["batch_id"]),
            grade=str(payload["grade"]),
            position=int(payload.get("position", 0)),
            opened_tick=int(payload.get("opened_tick", now)),
            deadline_tick=int(payload.get("deadline_tick", now)),
            window_ticks=int(payload.get("window_ticks", window)),
        )

    def _adopt_sample(self, payload: dict[str, Any], now: int) -> None:
        kind = str(payload.get("kind", EXTRA))
        grade = str(payload["grade"])
        tick = int(payload.get("tick", now))
        if kind == ON_TIME:
            adopt_sampled(
                ledger=self.ledger,
                grade=grade,
                batch_id=str(payload["batch_id"]),
                sample_id=str(payload["sample_id"]),
                tick=tick,
            )
        elif kind == MAKEUP:
            adopt_makeup(ledger=self.ledger, grade=grade, sample_id=str(payload["sample_id"]))

    def _adopt_miss(self, payload: dict[str, Any], now: int, window: int) -> None:
        item = self._due_from_payload(payload, now, window)
        adopt_miss(
            ledger=self.ledger,
            grade=item.grade,
            item=item.close(status="missed", tick=int(payload.get("closed_tick", now))),
            misses_in_a_row=int(payload.get("misses_in_a_row", 1)),
        )

    def _adopt_block(self, payload: dict[str, Any]) -> None:
        grade = str(payload["grade"])
        if payload.get("active", True):
            adopt_press(
                ledger=self.ledger,
                grade=grade,
                tick=int(payload.get("blocked_tick", 0)),
                misses_in_a_row=int(payload.get("misses_in_a_row", 1)),
            )
        else:
            adopt_release(
                ledger=self.ledger,
                grade=grade,
                tick=int(payload.get("released_tick", self.context.clock.now())),
            )
