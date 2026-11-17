"""Resumap: the FastAPI app, assembled.

``create_app`` is the composition root: the one place that reads configuration
and wires the Store, the model backends, the gateway and the services
together. Everything it builds is passed in, never imported, so a test (or a
second instance) can hand it storage and backends of its own.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from . import obs
from .api import errors
from .api.routers import ALL as ROUTERS
from .api.schemas import ErrorOut
from .config import (
    BACKEND,
    CLAUDE_CLI,
    CODEX_CLI,
    DEFAULT_FIRST_NAME,
    DOTENV_KEYS,
    FRONTEND_DIR,
    LOG_LEVEL,
    MODEL,
    PI_CLI,
    WORK_DIR,
    Paths,
    api_key,
)
from .llm import BackendRegistry, ModelGateway, PromptStore, default_registry
from .services import Services
from .storage import Database, Store


@asynccontextmanager
async def lifespan(app: FastAPI):
    services: Services = app.state.services
    services.paths.ensure()
    obs.setup(services.paths.logs, LOG_LEVEL)
    services.store.init()
    services.settings.apply_saved()
    registry = services.gateway.registry
    obs.event(
        "app.start",
        model=registry.model_label(),
        backend=registry.current_name(),
        backend_ready=services.gateway.status() is None,
        data_dir=obs.path_str(services.paths.uploads.parent),
        log_dir=obs.path_str(services.paths.logs),
        log_level=LOG_LEVEL,
        prompts_archived=obs.prompts_enabled(),
        text_in_logs=obs.log_text_enabled(),
        resumes=len(services.store.resumes.all()),
    )
    yield
    obs.event("app.stop", **obs.totals())


async def _log_requests(request: Request, call_next):
    run = obs.new_run_id()
    if request.url.path.startswith(("/styles.css", "/app.js")) or request.url.path == "/":
        return await call_next(request)  # static assets are not worth an event
    with obs.timer() as t:
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            obs.error("http.error", exc=exc, method=request.method, path=request.url.path)
            raise
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    obs.event(
        "http.request",
        level=level,
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        ms=t.ms,
    )
    if t.ms > 15000:
        obs.warn("http.slow", path=request.url.path, ms=t.ms)
    response.headers["X-Resumap-Run"] = run
    return response


class _NoCacheStatic(StaticFiles):
    """Serve the UI with revalidation forced.

    The frontend is three files on 127.0.0.1, so caching buys nothing, while a
    browser holding yesterday's `app.js` silently hides whatever changed.
    `no-cache` still allows a 304 against the ETag; it only bans using the copy
    without asking.
    """

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def create_app(paths: Paths | None = None, registry: BackendRegistry | None = None) -> FastAPI:
    """The app, over `paths` (default: the configured data dir) and `registry`
    (default: the four real backends, RESUMAP_BACKEND live until settings say
    otherwise)."""
    paths = paths or Paths.from_env()
    registry = registry or default_registry(
        selected=BACKEND,
        model=MODEL,
        api_key=api_key,
        claude_cli=CLAUDE_CLI,
        codex_cli=CODEX_CLI,
        pi_cli=PI_CLI,
        dotenv_keys=DOTENV_KEYS,
        cwd=WORK_DIR,
    )
    gateway = ModelGateway(registry, PromptStore(), paths.logs)
    services = Services.build(
        Store(Database(paths.db)),
        gateway,
        paths,
        default_backend=BACKEND,
        default_first_name=DEFAULT_FIRST_NAME,
    )

    # Declaring 422 here replaces FastAPI's default validation-error schema,
    # which this app does not send (see api/errors.py).
    app = FastAPI(
        title="Resumap",
        lifespan=lifespan,
        responses={422: {"model": ErrorOut, "description": "Invalid request"}},
    )
    app.state.services = services
    app.middleware("http")(_log_requests)
    errors.install(app)
    for router in ROUTERS:
        app.include_router(router)
    # Mounted last so every /api route wins over a static file.
    app.mount("/", _NoCacheStatic(directory=str(FRONTEND_DIR), html=True), name="frontend")
    return app


app = create_app()
