"""Why a service refused a request, in words the user can act on.

Services know nothing about HTTP. They raise one of these with the sentence to
show, and the API layer decides the status code (``api/errors.py``).
"""

from __future__ import annotations


class ServiceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    """The thing named does not exist."""


class Invalid(ServiceError):
    """The request is well-formed but cannot be done as asked."""


class Conflict(ServiceError):
    """Doing this now would undo or contradict something done later."""


class Unavailable(ServiceError):
    """This needs a model and the live backend cannot run."""


class UpstreamFailed(ServiceError):
    """The model answered, but not with anything usable."""
