"""Record kinds and event names used by the sampling governor."""

from __future__ import annotations

# Committed record kinds, always shaped '<group>.<leaf>' so the state
# projection folds them without a special registry.
OBLIGATION_KIND = "sampling.obligation"
GRADE_KIND = "sampling.grade"
HOLD_KIND = "sampling.hold"
SHIFT_KIND = "sampling.shift"
DEVICE_KIND = "sampling.device"
REPORT_KIND = "sampling.report"
