"""Sampling governance: frequency prompts, shift misses and grade holds.

The rules nailed down in the design:

* frequency and the hold threshold are per grade,
* a held grade is stopped on its own, the crew is not held wholesale,
* holds engage only at a handover, after the grade's missed run reaches its
  per grade threshold,
* an offline sampler makes a draw a partial failure and never counts,
* every prompt, hold, release, miss and outage is audited and survives a
  restart.
"""

from __future__ import annotations

import pytest

from mixplant.errors import (
    InterlockBlockedError,
    NotFoundError,
    OrderViolationError,
    PartialFailureError,
)
from mixplant.runtime import PlantRuntime
from mixplant.sampling.rules import GradeSamplingRule, SamplingRuleBook
from tests.support import (
    advance_to_mixed,
    publish_and_select_grade,
    run,
    run_full_cycle,
)


def declare(runtime: PlantRuntime, batch_id: str, *, grade: str = "C30", quantity: float = 6.0) -> dict:
    """Select a grade, declare one batch and return the governor payload."""

    publish_and_select_grade(runtime, grade)
    result = run(runtime, "batch.declare", {"batch_id": batch_id, "quantity": quantity})
    return result["sampling"]


# -- frequency table -------------------------------------------------------


def test_due_frequency_is_configured_per_grade(runtime: PlantRuntime) -> None:
    # C20 is due every second batch in the default table.
    first = declare(runtime, "B-1", grade="C20")
    assert first["decision"]["due"] is False
    assert "sampling" not in first
    second = declare(runtime, "B-2", grade="C20")
    assert second["decision"]["due"] is True
    assert second["obligation"]["batch_id"] == "B-2"
    prompt = runtime.sampling.status()["prompt"]
    assert [item["batch_id"] for item in prompt] == ["B-2"]


def test_c30_is_due_on_every_batch(runtime: PlantRuntime) -> None:
    first = declare(runtime, "B-1", grade="C30")
    second = declare(runtime, "B-2", grade="C30")
    assert first["decision"]["due"] is True
    assert second["decision"]["due"] is True


def test_a_reused_due_batch_identity_does_not_advance_the_counter(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    publish_and_select_grade(runtime, "C20")
    # First C20 batch is not due (frequency two), so the second declaration
    # is the one that would open an obligation.
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 6})
    run(runtime, "batch.declare", {"batch_id": "B-2", "quantity": 6})
    before = next(g for g in runtime.sampling.status()["grades"] if g["grade"] == "C20")
    assert before["batches_since_sample"] == 2
    # Re-declaring B-2 fails at the registry and leaves counters untouched.
    with pytest.raises(Exception):
        run(runtime, "batch.declare", {"batch_id": "B-2", "quantity": 6})
    after = next(g for g in runtime.sampling.status()["grades"] if g["grade"] == "C20")
    assert after["batches_since_sample"] == 2


def test_unknown_grade_uses_the_fallback_rule() -> None:
    book = SamplingRuleBook(
        (GradeSamplingRule("C30", every_n_batches=2, block_after_misses=2),),
        fallback=GradeSamplingRule("*", every_n_batches=1, block_after_misses=1),
    )
    rule = book.rule_for("C99")
    assert rule.every_n_batches == 1
    assert rule.block_after_misses == 1


def test_rule_book_rejects_non_positive_values() -> None:
    with pytest.raises(Exception):
        GradeSamplingRule("C30", every_n_batches=0, block_after_misses=1)
    with pytest.raises(Exception):
        GradeSamplingRule("C30", every_n_batches=1, block_after_misses=0)


# -- shift handover and misses --------------------------------------------


def test_handover_points_out_the_shift_that_missed_a_due_sample(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "night"})
    declare(runtime, "B-1", grade="C30")
    report = run(runtime, "sampling.handover")["report"]
    assert [item["batch_id"] for item in report["missed"]] == ["B-1"]
    assert report["missed"][0]["grade"] == "C30"
    grade = next(item for item in report["grades"] if item["grade"] == "C30")
    assert grade["consecutive_misses"] == 1
    # C30 holds after two misses, so one miss does not hold it yet.
    assert report["held"] == []


def test_a_sampled_due_batch_is_not_counted_as_a_miss(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    run_full_cycle(runtime, batch_id="B-1", truck_id="T-1")
    report = run(runtime, "sampling.handover")["report"]
    assert report["missed"] == []


def test_a_late_sample_after_the_miss_cannot_rewrite_history(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C30")
    run(runtime, "sampling.handover")
    obligation = runtime.sampling.obligations.get("B-1")
    assert obligation is not None and obligation.status == "missed"
    with pytest.raises(OrderViolationError):
        runtime.sampling.record_sample(batch_id="B-1", grade="C30")


def test_handover_without_an_open_shift_is_refused(runtime: PlantRuntime) -> None:
    with pytest.raises(NotFoundError):
        run(runtime, "sampling.handover")


def test_a_shift_cannot_be_handed_over_twice(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    run(runtime, "sampling.handover")
    with pytest.raises(NotFoundError):
        run(runtime, "sampling.handover")


def test_two_shifts_cannot_be_open_at_once(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    with pytest.raises(OrderViolationError):
        run(runtime, "sampling.open_shift", {"shift_id": "S-2", "crew": "night"})


# -- grade holds -----------------------------------------------------------


def test_consecutive_misses_hold_the_grade_at_its_threshold(runtime: PlantRuntime) -> None:
    # Shift one: miss B-1 (C30 threshold is two).
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C30")
    run(runtime, "sampling.handover")
    assert runtime.sampling.status()["held"] == []

    # Shift two: miss B-2, the run now reaches the C30 threshold.
    run(runtime, "sampling.open_shift", {"shift_id": "S-2", "crew": "night"})
    declare(runtime, "B-2", grade="C30")
    report = run(runtime, "sampling.handover")["report"]
    assert [item["grade"] for item in report["held"]] == ["C30"]
    assert runtime.sampling.status()["held"] == ["C30"]


def test_a_held_grade_blocks_only_its_own_next_declaration(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")  # C40 threshold is one
    run(runtime, "sampling.handover")

    # C40 is held; another C40 batch is refused at declaration...
    with pytest.raises(InterlockBlockedError):
        declare(runtime, "B-2", grade="C40")
    # ...but another grade keeps producing.
    other = declare(runtime, "B-3", grade="C20")
    assert other["decision"]["grade"] == "C20"


def test_sampling_clears_the_miss_run_but_does_not_release_a_hold(runtime: PlantRuntime) -> None:
    # Drive C30 to a hold (two consecutive misses).
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C30")
    run(runtime, "sampling.handover")
    run(runtime, "sampling.open_shift", {"shift_id": "S-2", "crew": "night"})
    declare(runtime, "B-2", grade="C30")
    run(runtime, "sampling.handover")
    assert runtime.sampling.status()["held"] == ["C30"]

    # The only documented way out is an explicit authorised release.
    with pytest.raises(NotFoundError):
        run(runtime, "sampling.release_hold", {"grade": "C20", "reason": "not held"})
    released = run(runtime, "sampling.release_hold", {"grade": "C30", "reason": "QA authorised"})
    assert released["hold"]["active"] is False
    assert runtime.sampling.status()["held"] == []

    # After release the miss run is reset.
    run(runtime, "sampling.open_shift", {"shift_id": "S-3", "crew": "day"})
    declare(runtime, "B-3", grade="C30")
    run(runtime, "sampling.handover")
    assert runtime.sampling.status()["held"] == []


def test_hold_threshold_is_per_grade(runtime: PlantRuntime) -> None:
    # C40 holds after one miss while C30 needs two.
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")
    declare(runtime, "B-2", grade="C30")
    held = run(runtime, "sampling.handover")["report"]["held"]
    assert [item["grade"] for item in held] == ["C40"]


def test_crew_is_not_held_wholesale(runtime: PlantRuntime) -> None:
    # Hold C40, then prove a different grade in the same next shift runs.
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")
    run(runtime, "sampling.handover")
    run(runtime, "sampling.open_shift", {"shift_id": "S-2", "crew": "day"})
    c20 = declare(runtime, "B-9", grade="C20")
    assert c20["decision"]["grade"] == "C20"


def test_a_held_grade_declaration_is_audited(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")
    run(runtime, "sampling.handover")
    with pytest.raises(InterlockBlockedError):
        declare(runtime, "B-2", grade="C40")
    refusal = runtime.audit.recent(50)[-1]
    assert refusal["name"] == "sampling.action_refused"
    assert refusal["detail"]["command"] == "batch.declare"
    assert refusal["detail"]["code"] == "interlock_blocked"


# -- device outage as a partial failure -----------------------------------


def test_a_draw_while_offline_is_a_partial_failure_and_not_counted(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    run_full_cycle_setup(runtime)
    run(runtime, "sampler.offline")
    with pytest.raises(PartialFailureError):
        run(runtime, "sampler.draw", {"litres": 12})
    # Nothing latched and no draw record landed.
    assert runtime.sampler.latch.latched is False
    assert runtime.sampler.status()["last_draw"] is None
    # The obligation is still open, so the handover counts the miss.
    report = run(runtime, "sampling.handover")["report"]
    assert [item["batch_id"] for item in report["missed"]] == ["B-1"]


def test_offline_then_online_allows_the_draw_and_clears_the_obligation(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    run_full_cycle_setup(runtime)
    run(runtime, "sampler.offline")
    with pytest.raises(PartialFailureError):
        run(runtime, "sampler.draw", {"litres": 12})
    run(runtime, "sampler.online")
    run(runtime, "sampler.draw", {"litres": 12})
    run(runtime, "sampler.release")
    filed = run(runtime, "sampler.file", {"litres": 12})
    assert filed["governance"]["obligation"]["status"] == "sampled"
    assert runtime.sampling.status()["prompt"] == []


def test_device_transitions_must_be_ordered(runtime: PlantRuntime) -> None:
    with pytest.raises(OrderViolationError):
        run(runtime, "sampler.online")
    run(runtime, "sampler.offline")
    with pytest.raises(OrderViolationError):
        run(runtime, "sampler.offline")


def run_full_cycle_setup(runtime: PlantRuntime) -> None:
    """Reach the point a sample can be drawn without filing one."""

    advance_to_mixed(runtime, batch_id="B-1")
    run(runtime, "mix.schedule")
    run(runtime, "chute.arrive", {"truck_id": "T-1"})
    run(runtime, "chute.open")
    run(runtime, "chute.load", {"truck_id": "T-1", "volume_m3": 6})
    run(runtime, "chute.confirm", {"truck_id": "T-1", "volume_m3": 6})
    run(runtime, "chute.close")
    run(runtime, "water.prime", {"litres": 60})
    run(runtime, "pump.prime")
    run(runtime, "pump.start")
    run(runtime, "pump.stroke", {"volume_m3": 0.32})
    run(runtime, "pump.pressure", {"reading_mpa": 5.4})
    run(runtime, "pump.finish")


# -- audit -----------------------------------------------------------------


def test_every_governor_transition_is_audited(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")
    run(runtime, "sampler.offline")
    run(runtime, "sampler.online")
    run(runtime, "sampling.handover")
    names = {entry["name"] for entry in runtime.audit.recent(100)}
    assert "sampling.sample_due" in names
    assert "sampling.device_offline" in names
    assert "sampling.device_online" in names
    assert "sampling.shift_handed_over" in names
    # The held alarm is copied into the ledger.
    assert "sampling.grade_held" in names


def test_a_refused_draw_is_booked_into_the_audit_ledger(runtime: PlantRuntime) -> None:
    run_full_cycle_setup(runtime)
    run(runtime, "sampler.offline")
    with pytest.raises(PartialFailureError):
        run(runtime, "sampler.draw", {"litres": 12})
    names = [entry["name"] for entry in runtime.audit.recent(100)]
    refusal = runtime.audit.recent(100)[-1]
    assert refusal["name"] == "sampling.action_refused"
    assert refusal["detail"]["command"] == "sampler.draw"
    assert refusal["detail"]["code"] == "partial_failure"
    assert "sampling.action_refused" in names


def test_prompt_and_hold_are_published_to_the_stream(runtime: PlantRuntime) -> None:
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C40")
    kinds = {record.kind for record in runtime.store.visible()}
    assert "sampling.obligation" in kinds
    assert "sampling.grade" in kinds
    assert "sampling.shift" in kinds
    run(runtime, "sampling.handover")
    kinds = {record.kind for record in runtime.store.visible()}
    assert "sampling.hold" in kinds
    assert "sampling.report" in kinds


# -- recovery --------------------------------------------------------------


def test_prompts_and_holds_survive_a_restart(tmp_path) -> None:
    from mixplant.config import StationConfig

    config = StationConfig(data_dir=tmp_path / "state", web_dir=tmp_path / "web")
    first = PlantRuntime(config, storage="file")
    first.bootstrap()
    run(first, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(first, "B-1", grade="C40")
    run(first, "sampling.handover")
    assert first.sampling.status()["held"] == ["C40"]
    first.close()

    second = PlantRuntime(config, storage="file")
    second.bootstrap()
    status = second.sampling.status()
    assert status["held"] == ["C40"]
    # The obligation is remembered as missed.
    obligation = next(item for item in status["obligations"] if item["batch_id"] == "B-1")
    assert obligation["status"] == "missed"
    assert status["device"]["online"] is True
    # The held grade is still blocked after recovery.
    with pytest.raises(InterlockBlockedError):
        declare(second, "B-2", grade="C40")


def test_device_outage_survives_a_restart(tmp_path) -> None:
    from mixplant.config import StationConfig

    config = StationConfig(data_dir=tmp_path / "state2", web_dir=tmp_path / "web")
    first = PlantRuntime(config, storage="file")
    first.bootstrap()
    run(first, "sampler.offline")
    first.close()

    second = PlantRuntime(config, storage="file")
    second.bootstrap()
    assert second.sampling.status()["device"]["online"] is False


def test_a_custom_rule_book_drives_the_threshold(runtime: PlantRuntime) -> None:
    book = SamplingRuleBook(
        (GradeSamplingRule("C30", every_n_batches=1, block_after_misses=3),),
        fallback=GradeSamplingRule("*", every_n_batches=1, block_after_misses=1),
    )
    runtime.sampling.rules = book
    run(runtime, "sampling.open_shift", {"shift_id": "S-1", "crew": "day"})
    declare(runtime, "B-1", grade="C30")
    run(runtime, "sampling.handover")
    run(runtime, "sampling.open_shift", {"shift_id": "S-2", "crew": "day"})
    declare(runtime, "B-2", grade="C30")
    run(runtime, "sampling.handover")
    # Two misses are below the custom threshold of three.
    assert runtime.sampling.status()["held"] == []
