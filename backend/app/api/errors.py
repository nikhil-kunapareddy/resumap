"""Turning a refusal into an HTTP response.

Every error body is ``{"detail": "<one readable sentence>"}``: the UI shows
``detail`` as the message. Services raise ``ServiceError`` subclasses and this
is the one place that knows which status each one is.
"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ..llm import BackendUnavailable
from ..services.errors import (Conflict, Invalid, NotFound, ServiceError, Unavailable,
                               UpstreamFailed)
from .schemas import ErrorOut

STATUS: dict[type[ServiceError], int] = {
    NotFound: 404,
    Invalid: 400,
    Conflict: 409,
    Unavailable: 503,
    UpstreamFailed: 502,
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ServiceError)
    async def _refused(_req: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=STATUS.get(type(exc), 500))

    @app.exception_handler(BackendUnavailable)
    async def _backend_unavailable(_req: Request, exc: BackendUnavailable) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_req: Request, exc: RequestValidationError) -> JSONResponse:
        """422, with the per-field errors joined into one readable `detail`."""
        errors = jsonable_encoder(exc.errors())
        detail = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'][1:]) or e['loc'][0]}: {e['msg']}"
            for e in errors)
        return JSONResponse(ErrorOut(detail=detail, errors=errors).model_dump(),
                            status_code=422)
