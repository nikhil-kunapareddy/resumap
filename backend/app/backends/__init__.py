"""Model backends.

Resumap reaches a model one of four ways. ``RESUMAP_BACKEND`` picks the one a
fresh install starts with; the settings panel switches it at runtime (stored
in the ``settings`` table and applied through ``select()``):

* ``claude_code`` -- the local Claude Code install, driven through the Agent
  SDK. Credentials come from that install, so a Claude subscription login
  works and no API key is needed; in exchange the work counts against that
  subscription's rate limits, shared with interactive Claude Code use.
* ``codex`` -- the local OpenAI Codex CLI (``codex exec``), signed in with
  ``codex login``; spends that ChatGPT plan's limits.
* ``pi`` -- the local ``pi`` coding agent, routed to whichever provider and
  model pi is configured with.
* ``api`` -- the Anthropic API with an ``ANTHROPIC_API_KEY`` from ``.env``.
  Billed per token.

Every call takes a system prompt, a user prompt and a Pydantic model, and comes
back as one validated instance of that model, so ``app.claude`` does not care
which backend is live. No backend lets the model use tools: these are single
structured requests, not agent loops.

A backend is a module exposing ``NAME``, ``LABEL``, ``DEFAULT_MODEL``,
``CONCURRENCY``, ``unavailable()`` and ``complete()``. An empty
``DEFAULT_MODEL`` means "whatever that CLI is configured to use".
"""
from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from ..config import BACKEND, DOTENV_KEYS

# Settings-panel order.
MODULES = {"claude_code": "claude_code", "codex": "codex", "pi": "pi",
           "api": "anthropic_api"}


@dataclass(frozen=True)
class Usage:
    """Token counts, shaped for ``obs.record_usage`` to read by attribute."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Completion:
    """One structured answer from a backend.

    ``parsed`` is None when the model produced nothing that validates against
    the schema. That is not an exception here: the JD parser degrades to its
    heuristic splitter and the rewriter reports the bullet as failed, so both
    callers want to log the attempt first.
    """

    parsed: Any | None
    text: str
    usage: Usage
    # An Anthropic request id, or a Claude Code session id -- whichever handle
    # finds this call again in the provider's logs.
    request_id: str | None = None
    # Backend-specific fields for the prompt archive header.
    meta: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    NAME: str
    LABEL: str
    DEFAULT_MODEL: str
    CONCURRENCY: int

    def unavailable(self) -> str | None:
        """Why this backend cannot run right now, or None if it is ready."""

    async def complete(self, *, model: str, system: str, user: str,
                       schema: type[BaseModel], max_tokens: int,
                       effort: str) -> Completion: ...


# The live choice. Starts from RESUMAP_BACKEND; main.py applies the stored
# settings at startup and whenever the settings panel saves.
_selected: dict[str, Any] = {"name": BACKEND, "models": {}}


def select(name: str, models: dict[str, str] | None = None) -> None:
    """Make `name` the live backend; `models` maps backend -> model override."""
    _selected["name"] = name
    if models:
        _selected["models"].update(models)


def current_name() -> str:
    return _selected["name"]


def current_model(name: str | None = None) -> str:
    """The model to request: the user's override, else the backend's default
    (which is "" for a CLI that should use its own configured model)."""
    name = name or current_name()
    override = (_selected["models"].get(name) or "").strip()
    if override:
        return override
    try:
        return get(name).DEFAULT_MODEL
    except Exception:  # noqa: BLE001 - an unknown name; get() reports it
        return ""


def model_label() -> str:
    """The model for logs and the UI badge, never blank."""
    return current_model() or f"{current_name()} default"


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


def get(name: str | None = None) -> Backend:
    """The live backend, or the one called `name`."""
    name = name or current_name()
    module = MODULES.get(name)
    if module is None:
        raise RuntimeError(
            f"Unknown backend {name!r}. Choose one of: {', '.join(MODULES)}."
        )
    return importlib.import_module(f".{module}", __package__)  # type: ignore[return-value]


def describe() -> list[dict[str, Any]]:
    """Every backend with its readiness, for the settings panel."""
    out = []
    for name in MODULES:
        b = get(name)
        why = b.unavailable()
        out.append({
            "name": name, "label": b.LABEL, "ready": why is None, "detail": why,
            "default_model": b.DEFAULT_MODEL,
            "model": (_selected["models"].get(name) or "").strip(),
            "selected": name == current_name(),
        })
    return out


def cli_env() -> dict[str, str]:
    """The environment for a CLI backend's subprocess.

    Credentials that came from Resumap's own ``.env`` are for the ``api``
    backend. A CLI that finds, say, ANTHROPIC_API_KEY in its environment will
    happily bill it per token instead of using its own sign-in, so any such
    key is dropped -- unless the shell set it too, with its own value.
    """
    env = dict(os.environ)
    for key, value in DOTENV_KEYS.items():
        if key.endswith(("_API_KEY", "_AUTH_TOKEN")) and env.get(key) == value:
            env.pop(key, None)
    return env


async def run_cli(cmd: list[str], stdin_text: str | None, *, cwd: str,
                  env: dict[str, str], timeout: float) -> tuple[int, str, str]:
    """Run a CLI backend once: (exit code, stdout, stderr), killed on timeout."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None),
            timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"{os.path.basename(cmd[0])} gave no answer within {timeout:.0f}s")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
