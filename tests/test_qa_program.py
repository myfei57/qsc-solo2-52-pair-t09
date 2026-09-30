"""Per grade sampling frequency, reminders, handover misses and grade press.

The press is fixed to per grade: a pressed grade only stops further
``batch.declare`` for that grade.  Every reminder, miss, press and release
lands in the record stream, and an offline sampler is a partial failure that
can never be counted as a sample.
"""

from __future__ import annotations

import pytest

from mixplant.errors import InterlockBlockedError, ValidationError
from mixplant.qa.decide import (
    MAKEUP,
    ON_TIME,
    file_sample,
    press,
    register_completion,
    release,
    sweep_grade,
)
from mixplant.qa.ledger import GradeLedger
from mixplant.qa.plan import GradePlan, SamplingPlan
from mixplant.runtime import PlantRuntime
from tests.support import run, run_full_cycle

C30_PLAN = GradePlan(grade="C30", every_batches=1, block_after_misses=2)
C20_PLAN = GradePlan(grade="C20", every_batches=2, block_after_misses=3)
WINDOW = 8


def _complete(ledger, plan, batch_id, tick):
    return register_completion(
        ledger=ledger, plan=plan, batch_id=batch_id, tick=tick, window_ticks=WINDOW
    )


def test_plan_uses_per_grade_frequency_and_threshold() -> None:
    plan = SamplingPlan.default()
    assert plan.for_grade("C30").block_after_misses == 2
    assert plan.for_grade("C20").every_batches == 2
    # an unlisted grade falls back to the station default
    assert plan.for_grade("C99").every_batches == 1
    with pytest.raises(ValidationError):
        GradePlan(grade="C30", every_batches=0, block_after_misses=1)


def test_a_due_sample_opens_then_clears_on_time() -> None:
    ledger = GradeLedger()
    item = _complete(ledger, C30_PLAN, "B-1", tick=0)
    assert item is not None and item.deadline_tick == WINDOW
    state = ledger.require("C30")
    verdict = file_sample(
        ledger=ledger, grade="C30", batch_id="B-1", sample_id="s1", tick=WINDOW
    )
    assert verdict.kind == ON_TIME
    assert state.open_item is None and state.misses_in_a_row == 0


def test_missing_the_window_counts_a_miss_and_presses_at_the_grade_threshold() -> None:
    ledger = GradeLedger()
    _complete(ledger, C30_PLAN, "B-1", tick=0)
    first = sweep_grade(state=ledger.require("C30"), now=WINDOW + 1)
    assert first is not None and first.status == "missed"
    assert ledger.require("C30").misses_in_a_row == 1
    _complete(ledger, C30_PLAN, "B-2", tick=WINDOW + 1)
    state = ledger.require("C30")
    sweep_grade(state=state, now=2 * WINDOW + 2)
    press(state=state, tick=2 * WINDOW + 2)
    assert ledger.require("C30").blocked is True


def test_a_window_not_yet_elapsed_is_not_a_miss() -> None:
    ledger = GradeLedger()
    _complete(ledger, C30_PLAN, "B-1", tick=0)
    assert sweep_grade(state=ledger.require("C30"), now=WINDOW) is None
    assert ledger.require("C30").misses_in_a_row == 0


def test_c20_cadence_only_asks_every_second_batch() -> None:
    ledger = GradeLedger()
    assert _complete(ledger, C20_PLAN, "B-1", tick=0) is None
    second = _complete(ledger, C20_PLAN, "B-2", tick=1)
    assert second is not None and second.batch_id == "B-2"


def test_release_needs_a_makeup_for_the_missed_batch() -> None:
    ledger = GradeLedger()
    _complete(ledger, C30_PLAN, "B-1", tick=0)
    state = ledger.require("C30")
    sweep_grade(state=state, now=WINDOW + 1)
    state.blocked = True
    with pytest.raises(ValidationError):
        release(state=state, tick=20)
    file_sample(ledger=ledger, grade="C30", batch_id="B-1", sample_id="s9", tick=20)
    assert ledger.require("C30").makeup_sample_ids == ["s9"]
    release(state=state, tick=21)
    assert state.blocked is False


def test_runtime_reminds_then_presses_only_that_grade(runtime: PlantRuntime) -> None:
    run_full_cycle(runtime, batch_id="B-001", truck_id="T-01")
    # the filed on time sample cleared the reminder, so nothing is due
    assert runtime.qa.status()["due"] == []

    # force a due item that is then missed for two batches in a row
    runtime.qa.batch_completed(batch_id="B-010", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    runtime.qa.batch_completed(batch_id="B-011", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    assert runtime.qa.status()["ledger"]["blocked"] == ("C30",)

    # a C30 batch can no longer be declared
    with pytest.raises(InterlockBlockedError):
        run(runtime, "batch.declare", {"batch_id": "B-012", "quantity": 6})


def test_press_is_per_grade_other_grades_keep_pouring(runtime: PlantRuntime) -> None:
    runtime.qa.batch_completed(batch_id="X1", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    runtime.qa.batch_completed(batch_id="X2", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    assert runtime.qa.status()["ledger"]["blocked"] == ("C30",)

    # a C20 recipe can still be selected and declared
    run(runtime, "recipe.publish", {"name": "m20", "grade": "C20",
        "factors": {"sand": 700.0, "gravel": 1000.0, "cement": 280.0, "water": 170.0}})
    run(runtime, "recipe.select", {"name": "m20"})
    result = run(runtime, "batch.declare", {"batch_id": "Y1", "quantity": 6})
    assert result["batch"]["grade"] == "C20"


def test_makeup_then_release_reopens_the_grade(runtime: PlantRuntime) -> None:
    runtime.qa.batch_completed(batch_id="P1", grade="C40")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    assert runtime.qa.status()["ledger"]["blocked"] == ("C40",)
    with pytest.raises(ValidationError):
        run(runtime, "qa.release", {"grade": "C40"})
    verdict = runtime.qa.sample_filed(batch_id="P1", grade="C40", sample_id="mk-1")
    assert verdict.kind == MAKEUP
    run(runtime, "qa.release", {"grade": "C40"})
    assert runtime.qa.status()["ledger"]["blocked"] == ()


def test_handover_points_out_the_missed_grades(runtime: PlantRuntime) -> None:
    runtime.qa.batch_completed(batch_id="H1", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    report = run(runtime, "qa.handover", {})
    grades = {row["grade"] for row in report["closed"]["missed_grades"]}
    assert "C30" in grades
    assert report["next"]["shift"] == "shift-000002"


def test_offline_sampler_is_a_partial_failure_never_a_sample(runtime: PlantRuntime) -> None:
    run(runtime, "sampler.online", {"online": "false"})
    with pytest.raises(InterlockBlockedError):
        run(runtime, "sampler.draw", {"litres": 12})
    # the failed attempt is audited and explicitly counted as unsampled
    attempts = [r for r in runtime.store.visible() if r.kind == "qa.attempt"]
    assert attempts and attempts[-1].payload["counted_as"] == "unsampled"
    run(runtime, "sampler.online", {"online": "true"})
    assert runtime.qa.sampler_online() is True


def test_every_qa_transition_is_audited(runtime: PlantRuntime) -> None:
    runtime.qa.batch_completed(batch_id="A1", grade="C30")
    run(runtime, "clock.advance", {"ticks": WINDOW + 1})
    runtime.qa.sample_filed(batch_id="A1", grade="C30", sample_id="mk")
    kinds = {r.kind for r in runtime.store.visible()}
    assert {"qa.completion", "qa.due", "qa.miss", "qa.sampling"} <= kinds


def test_press_survives_a_restart(tmp_path) -> None:
    from mixplant.config import StationConfig

    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    first.qa.batch_completed(batch_id="R1", grade="C30")
    first.advance_ticks(WINDOW + 1)
    first.qa.batch_completed(batch_id="R2", grade="C30")
    first.advance_ticks(WINDOW + 1)
    run(first, "sampler.online", {"online": "false"})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    assert second.qa.status()["ledger"]["blocked"] == ("C30",)
    assert second.qa.sampler_online() is False
    with pytest.raises(InterlockBlockedError):
        second.qa.require_grade_allowed("C30")
    second.close()
