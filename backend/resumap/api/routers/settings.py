"""Status, the backend choice and its key, the filename prefix, the editable
prompts, and quitting."""

from __future__ import annotations

from fastapi import APIRouter

from ...services.results import (
    BackendsOut,
    BackendTestOut,
    Ok,
    PromptInfo,
    PromptsOut,
    SettingsOut,
    StatusOut,
)
from ..deps import ServicesDep
from ..schemas import BackendBody, KeyBody, ModelBody, PromptBody, SettingsBody

router = APIRouter(prefix="/api", tags=["settings"])


@router.get("/status")
def status(services: ServicesDep) -> StatusOut:
    return services.settings.status()


@router.get("/backends")
def list_backends(services: ServicesDep) -> BackendsOut:
    """Every backend and whether it can run, for the settings panel."""
    return services.settings.backends()


@router.post("/backends/{name}/test")
async def test_backend(
    name: str, services: ServicesDep, body: ModelBody | None = None
) -> BackendTestOut:
    """Make one tiny real call through a backend, without switching to it."""
    return await services.settings.test_backend(name, body.model.strip() if body else "")


@router.post("/backends")
async def choose_backend(body: BackendBody, services: ServicesDep) -> StatusOut:
    """Switch the live backend, after one real test call."""
    return await services.settings.choose_backend(body.backend, body.model)


@router.put("/backends/{name}/key")
def save_key(name: str, body: KeyBody, services: ServicesDep) -> BackendsOut:
    """Save a backend's API key (blank removes it). The key is never sent back."""
    return services.settings.save_key(name, body.key)


@router.post("/quit")
def quit_app(services: ServicesDep) -> Ok:
    """Stop the server, when whoever started it allows that (the packaged app)."""
    return services.lifecycle.quit()


@router.post("/settings")
def save_settings(body: SettingsBody, services: ServicesDep) -> SettingsOut:
    return services.settings.set_first_name(body.first_name)


@router.get("/prompts")
def prompts(services: ServicesDep) -> PromptsOut:
    return services.settings.prompts()


@router.put("/prompts/{key}")
def save_prompt(key: str, body: PromptBody, services: ServicesDep) -> PromptInfo:
    """Replace the instructions for `key`; the default text, or blank, resets it."""
    return services.settings.save_prompt(key, body.text)


@router.delete("/prompts/{key}")
def reset_prompt(key: str, services: ServicesDep) -> PromptInfo:
    return services.settings.reset_prompt(key)
