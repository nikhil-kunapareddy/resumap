"""The settings panel: which backend is live, the filename prefix, prompt edits."""
from __future__ import annotations

import re

from .. import obs
from ..llm import EDITABLE_PROMPTS, ModelGateway
from ..llm.backends import suggest_model
from ..storage import Store
from .base import Service
from .errors import Invalid, NotFound
from .results import BackendsOut, BackendTestOut, PromptInfo, PromptsOut, SettingsOut, StatusOut

PROMPT_MAX_CHARS = 20000


class SettingsService(Service):
    def __init__(self, store: Store, gateway: ModelGateway, *, default_backend: str,
                 default_first_name: str) -> None:
        super().__init__(store)
        self.gateway = gateway
        self.default_backend = default_backend
        self.default_first_name = default_first_name

    def apply_saved(self) -> None:
        """Put the saved backend, models and prompt edits live (at startup)."""
        registry = self.gateway.registry
        name = self.store.settings.get("backend", self.default_backend)
        if name not in registry:
            name = self.default_backend
        registry.select(name, {b: self.store.settings.get(f"model.{b}", "")
                               for b in registry.names})
        self._apply_prompts()

    def _apply_prompts(self) -> None:
        self.gateway.prompts.set_overrides(self.store.settings.with_prefix("prompt."))

    def first_name(self) -> str:
        return self.store.settings.get("first_name", self.default_first_name)

    # -- backend ------------------------------------------------------------

    def status(self) -> StatusOut:
        registry = self.gateway.registry
        why = self.gateway.status()
        try:
            label = registry.get().label
        except Exception:  # noqa: BLE001 - an unknown name; `detail` says so
            label = registry.current_name()
        return StatusOut(
            backend=registry.current_name(),
            label=label,
            ready=why is None,
            detail=why,
            model=registry.model_label(),
            first_name=self.first_name(),
        )

    def backends(self) -> BackendsOut:
        """Every backend and whether it can run, for the settings panel."""
        registry = self.gateway.registry
        return BackendsOut(selected=registry.current_name(), backends=registry.describe())

    async def test_backend(self, name: str, model: str) -> BackendTestOut:
        """Make one tiny real call through a backend, without switching to it.
        `model`, when given, is tried instead of the saved one."""
        if name not in self.gateway.registry:
            raise NotFound(f"Unknown backend '{name}'.")
        out = await self.gateway.ping(name, model or None)
        hint = suggest_model(name, model)
        if not out["ok"] and hint:
            out["detail"] += f" Did you mean {hint}?"
        return BackendTestOut(**out)

    async def choose_backend(self, name: str, model: str) -> StatusOut:
        """Switch the live backend. Takes effect on the next model call; nothing
        already stored (rewrites, drafts) is touched.

        The choice is tested with one real call before it is saved. Saving a model
        name with a typo in it used to succeed, and the first sign of it was every
        later call failing.
        """
        if name not in self.gateway.registry:
            raise Invalid(f"Unknown backend '{name}'.")
        model = model.strip()
        if len(model) > 120 or any(c.isspace() for c in model):
            raise Invalid("A model name is a single word, e.g. gpt-5.5 or google/gemini-2.5-pro.")
        check = await self.gateway.ping(name, model or None)
        if not check["ok"]:
            hint = suggest_model(name, model)
            obs.warn("settings.backend_rejected", backend=name,
                     model=model or "default", suggested=hint)
            raise Invalid(f"Not switched: {check['detail']}"
                          + (f" Did you mean {hint}?" if hint else ""))
        with self.store.transaction():
            self.store.settings.set("backend", name)
            self.store.settings.set(f"model.{name}", model)
        self.gateway.registry.select(name, {name: model})
        out = self.status()
        obs.event("settings.backend", backend=name,
                  model=self.gateway.registry.model_label(), ready=out.ready)
        return out

    def set_first_name(self, first_name: str) -> SettingsOut:
        name = re.sub(r"[^A-Za-z0-9]", "", first_name).lower() or "resume"
        self.store.settings.set("first_name", name)
        return SettingsOut(first_name=name)

    # -- editable prompts ---------------------------------------------------

    def prompts(self) -> PromptsOut:
        return PromptsOut(prompts=self.gateway.prompts.describe())

    def _prompt_info(self, key: str) -> PromptInfo:
        return PromptInfo(**next(x for x in self.gateway.prompts.describe() if x["key"] == key))

    def save_prompt(self, key: str, text: str) -> PromptInfo:
        """Replace the instructions for `key`. Text identical to the default, or
        blank, is a reset: there is then nothing custom left to keep."""
        if key not in EDITABLE_PROMPTS:
            raise NotFound(f"No editable prompt called '{key}'.")
        text = text.strip()
        if len(text) > PROMPT_MAX_CHARS:
            raise Invalid(f"Keep a prompt under {PROMPT_MAX_CHARS:,} characters.")
        if not text or text == self.gateway.prompts.default(key).strip():
            self.store.settings.delete(f"prompt.{key}")
        else:
            self.store.settings.set(f"prompt.{key}", text)
        self._apply_prompts()
        obs.event("prompt.saved", key=key, custom=self.gateway.prompts.is_custom(key),
                  **obs.tmeta(text, "prompt"))
        return self._prompt_info(key)

    def reset_prompt(self, key: str) -> PromptInfo:
        if key not in EDITABLE_PROMPTS:
            raise NotFound(f"No editable prompt called '{key}'.")
        self.store.settings.delete(f"prompt.{key}")
        self._apply_prompts()
        obs.event("prompt.reset", key=key)
        return self._prompt_info(key)
