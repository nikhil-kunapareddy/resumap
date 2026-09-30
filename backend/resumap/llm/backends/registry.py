"""The backends on offer, and which one is live.

A fresh install starts on ``RESUMAP_BACKEND``; the settings panel switches it
at runtime, and ``SettingsService`` applies the saved choice at startup. The
registry is the only holder of that choice, so the gateway reads the live
backend and model from it at call time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .base import Backend


class BackendRegistry:
    def __init__(self, backends: Mapping[str, Backend], selected: str) -> None:
        """`backends` in settings-panel order, keyed by the name each is chosen under."""
        self._backends = dict(backends)
        self._selected = selected
        self._models: dict[str, str] = {}

    @property
    def names(self) -> list[str]:
        return list(self._backends)

    def __contains__(self, name: object) -> bool:
        return name in self._backends

    def get(self, name: str | None = None) -> Backend:
        """The live backend, or the one called `name`."""
        name = name or self._selected
        backend = self._backends.get(name)
        if backend is None:
            raise RuntimeError(
                f"Unknown backend {name!r}. Choose one of: {', '.join(self._backends)}."
            )
        return backend

    def select(self, name: str, models: Mapping[str, str] | None = None) -> None:
        """Make `name` the live backend; `models` maps backend -> model override."""
        self._selected = name
        if models:
            self._models.update(models)

    def current_name(self) -> str:
        return self._selected

    def current_model(self, name: str | None = None) -> str:
        """The model to request: the user's override, else the backend's default
        (which is "" for a CLI that should use its own configured model)."""
        name = name or self._selected
        override = (self._models.get(name) or "").strip()
        if override:
            return override
        try:
            return self.get(name).default_model
        except Exception:  # noqa: BLE001 - an unknown name; get() reports it
            return ""

    def model_label(self) -> str:
        """The model for logs and the UI badge, never blank."""
        return self.current_model() or f"{self._selected} default"

    def describe(self) -> list[dict[str, Any]]:
        """Every backend with its readiness, for the settings panel."""
        out = []
        for name, b in self._backends.items():
            why = b.unavailable()
            out.append(
                {
                    "name": name,
                    "label": b.label,
                    "ready": why is None,
                    "detail": why,
                    "default_model": b.default_model,
                    "model": (self._models.get(name) or "").strip(),
                    "selected": name == self._selected,
                }
            )
        return out


# A Claude model id: family, then version numbers joined by hyphens, with an
# optional date ("claude-opus-5-5", "claude-haiku-4-5-20251001"), or one of
# Claude Code's aliases.
_CLAUDE_ID = re.compile(r"^claude-[a-z]+(?:-\d+)+$|^(?:opus|sonnet|haiku|fable)$")


def suggest_model(name: str, model: str) -> str | None:
    """A likely intended id when `model` is a malformed Claude one, else None.

    "claude-opus5.5" is the common slip: a missing hyphen and a dot where the
    API wants "claude-opus-5-5". Other backends' names are left alone -- Codex
    and pi model names have no single shape to check against.
    """
    if name not in ("claude_code", "api") or not model or _CLAUDE_ID.match(model):
        return None
    fixed = re.sub(r"(?<=[a-z])(?=\d)", "-", model.lower().replace(".", "-").replace("_", "-"))
    fixed = re.sub(r"-+", "-", fixed)
    return fixed if fixed != model and _CLAUDE_ID.match(fixed) else None
