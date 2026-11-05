"""Stopping the server from the page.

The packaged app has no terminal to press Ctrl-C in, so the page offers a Quit
button. Whoever starts the server (``resumap.launcher``) hands over how to stop
it; an app built without that, such as the test suite's, refuses to quit.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import obs
from .errors import Conflict
from .results import Ok


class LifecycleService:
    def __init__(self) -> None:
        self._on_quit: Callable[[], None] | None = None

    def allow_quit(self, on_quit: Callable[[], None]) -> None:
        """Let the page stop this server by calling `on_quit`."""
        self._on_quit = on_quit

    @property
    def can_quit(self) -> bool:
        return self._on_quit is not None

    def quit(self) -> Ok:
        if self._on_quit is None:
            raise Conflict(
                "This server was not started with a way to stop it from the page. "
                "Stop it where it was started, with Ctrl-C."
            )
        obs.event("app.quit_requested")
        self._on_quit()
        return Ok()
