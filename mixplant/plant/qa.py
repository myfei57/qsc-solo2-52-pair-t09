"""Read model behind the quality assurance page."""

from __future__ import annotations

from typing import Any


def qa_view(*, qa_service: Any, alarms: Any) -> dict[str, Any]:
    """Build the sampling reminder and grade press view for the console."""

    status = qa_service.status()
    return {
        "sampler": status["sampler"],
        "shift": status["shift"],
        "plan": status["plan"],
        "due": status["due"],
        "blocked": status["ledger"]["blocked"],
        "grades": status["ledger"]["grades"],
        "alarms": alarms,
    }
