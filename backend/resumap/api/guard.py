"""Keeping the local API local.

Resumap listens on 127.0.0.1 with no login. That stops other machines, but not
other websites: any page open in the same browser can send it requests. Two
checks close that gap:

* Host. A request must be addressed to this machine by a loopback name, or by
  the address the server was told to bind. A DNS-rebinding page reaches the
  port under its own domain name, so its requests carry that name and are
  refused.
* Origin. A browser names the page behind every cross-site POST, PUT, PATCH
  or DELETE. A write whose Origin is not this server is refused. That covers
  the form posts and bodyless POSTs a page can send without a CORS preflight
  (an upload, accept-all, quit, a billed backend test). The UI is same-origin,
  and scripts and curl send no Origin.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def trusted_hosts(bind: str) -> frozenset[str] | None:
    """The host names a request may use when the server binds `bind`.

    None allows any: binding every interface is a deliberate choice to serve
    the network, which no host list can anticipate.
    """
    bind = bind.strip().strip("[]").lower()
    if bind in ("", "0.0.0.0", "::"):
        return None
    return LOOPBACK | {bind}


def install(app: FastAPI, hosts: Collection[str] | None) -> None:
    @app.middleware("http")
    async def _local_only(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        host = request.headers.get("host", "")
        if hosts is not None and (urlsplit(f"//{host}").hostname or "") not in hosts:
            return JSONResponse(
                {"detail": "Resumap only answers requests addressed to this machine."},
                status_code=403,
            )
        origin = request.headers.get("origin")
        if (
            origin is not None
            and request.method not in SAFE_METHODS
            and urlsplit(origin).netloc != host
        ):
            return JSONResponse(
                {"detail": "Refused a request sent by another website."}, status_code=403
            )
        return await call_next(request)
