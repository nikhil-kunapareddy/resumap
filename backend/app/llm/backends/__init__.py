"""Model backends.

Resumap reaches a model one of four ways. ``RESUMAP_BACKEND`` picks the one a
fresh install starts with; the settings panel switches it at runtime (stored
in the ``settings`` table and applied to the ``BackendRegistry``):

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

A backend is a ``Backend`` subclass: ``name``, ``label``, ``concurrency``,
``default_model``, ``unavailable()`` and ``complete()``. Each takes its
configuration in its constructor rather than reading it from the environment.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from .anthropic_api import AnthropicApiBackend
from .base import Backend, Completion, Usage, cli_env, run_cli
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .pi import PiBackend
from .registry import BackendRegistry, suggest_model

# Settings-panel order.
NAMES = ("claude_code", "codex", "pi", "api")


def default_registry(*, selected: str, model: str, api_key: Callable[[], str | None],
                     claude_cli: str | None, codex_cli: str | None, pi_cli: str | None,
                     dotenv_keys: Mapping[str, str], cwd: Path) -> BackendRegistry:
    """The four real backends, with `selected` live until settings say otherwise."""
    backends: list[Backend] = [
        ClaudeCodeBackend(model, claude_cli, cwd),
        CodexBackend(codex_cli, dotenv_keys),
        PiBackend(pi_cli, dotenv_keys),
        AnthropicApiBackend(model, api_key),
    ]
    return BackendRegistry({b.name: b for b in backends}, selected)


__all__ = ["NAMES", "AnthropicApiBackend", "Backend", "BackendRegistry", "ClaudeCodeBackend",
           "CodexBackend", "Completion", "PiBackend", "Usage", "cli_env", "default_registry",
           "run_cli", "suggest_model"]
