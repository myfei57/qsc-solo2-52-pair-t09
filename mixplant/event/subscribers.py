"""Subscribers that give the bus its two jobs."""

from __future__ import annotations

from typing import Any

from ..store.repository import RecordRepository
from .bus import Event
from .topics import ALARM

AUDIT_KIND = "audit.event"


class AuditSubscriber:
    """Copies every topic event into the record stream.

    When handed the in memory audit ledger it also books sampling relevant
    events (prompts, holds, releases, outages) there, so the shift ledger
    carries the same evidence the journal does.
    """

    AUDITED_NAMES = frozenset(
        {
            "sampling.sample_due",
            "sampling.sample_recorded",
            "sampling.shift_opened",
            "sampling.shift_handed_over",
            "sampling.hold_released",
            "sampling.device_offline",
            "sampling.device_online",
            "sampling.grade_held",
        }
    )

    __slots__ = ("_repository", "_seen", "_ledger")

    def __init__(self, repository: RecordRepository, ledger: Any = None) -> None:
        self._repository = repository
        self._seen = 0
        self._ledger = ledger

    def attach_ledger(self, ledger: Any) -> None:
        """Also book audited domain events into the in memory shift ledger."""

        self._ledger = ledger

    def __call__(self, event: Event) -> None:
        self.handle(event)

    def handle(self, event: Event) -> None:
        self._repository.publish(
            AUDIT_KIND,
            event.origin,
            int(event.payload.get("generation", 0) or 0),
            {"topic": event.topic, "name": event.name, "origin": event.origin, "tick": event.tick},
        )
        self._seen += 1
        if self._ledger is not None and event.name in self.AUDITED_NAMES:
            from ..audit.record import audit_entry

            self._ledger.record(
                audit_entry(
                    name=event.name,
                    origin=event.origin,
                    tick=event.tick,
                    detail=dict(event.payload),
                )
            )

    def seen(self) -> int:
        """Return how many events were copied into the stream."""

        return self._seen


class AlarmCollector:
    """Keeps the newest alarms so the overview page can list them."""

    __slots__ = ("_capacity", "_alarms")

    def __init__(self, capacity: int = 32) -> None:
        if capacity < 1:
            raise ValueError("alarm capacity must be positive")
        self._capacity = capacity
        self._alarms: list[dict[str, Any]] = []

    def __call__(self, event: Event) -> None:
        self.handle(event)

    def handle(self, event: Event) -> None:
        if event.topic != ALARM:
            return
        self._alarms.append(event.describe())
        if len(self._alarms) > self._capacity:
            self._alarms = self._alarms[-self._capacity :]

    def recent(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._alarms]

    def count(self) -> int:
        return len(self._alarms)

    def clear(self) -> None:
        """Drop every collected alarm."""

        self._alarms.clear()
