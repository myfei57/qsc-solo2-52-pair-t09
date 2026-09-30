"""Routes for the sampling governance page: prompts, holds and the device."""

from __future__ import annotations

from typing import Any

from .response import inject_snapshot
from .router import Request, Response, Router

COMMANDS = (
    ("POST", "/api/sampling/open_shift", "sampling.open_shift"),
    ("POST", "/api/sampling/handover", "sampling.handover"),
    ("POST", "/api/sampling/release_hold", "sampling.release_hold"),
    ("POST", "/api/sampler/offline", "sampler.offline"),
    ("POST", "/api/sampler/online", "sampler.online"),
)


def register(router: Router, runtime: Any, pages: Any) -> None:
    """Register the sampling governance routes."""

    router.add(
        "GET",
        "/pages/sampling",
        lambda _: Response.html(inject_snapshot(pages.load("sampling"), runtime.controller.sampling_view())),
    )
    router.add("GET", "/api/sampling", lambda _: Response.json(runtime.controller.sampling_view()))
    for method, path, command in COMMANDS:
        router.add(method, path, _command(runtime, command))


def _command(runtime: Any, command: str):
    def handler(request: Request) -> Response:
        return Response.json(runtime.dispatch(command, dict(request.body)))

    return handler
