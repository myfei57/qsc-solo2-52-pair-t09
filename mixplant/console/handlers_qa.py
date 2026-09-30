"""Routes for the sampling reminder, grade press and handover page."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/qa/release", "qa.release"),
    ("POST", "/api/qa/handover", "qa.handover"),
    ("POST", "/api/qa/sweep", "qa.sweep"),
    ("POST", "/api/sampler/online", "sampler.online"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the quality assurance routes."""

    router.add(
        "GET",
        "/pages/qa",
        lambda _: Response.html(inject_snapshot(pages.load("qa"), runtime.controller.qa_view())),
    )
    router.add("GET", "/api/qa", lambda _: Response.json(runtime.controller.qa_view()))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
