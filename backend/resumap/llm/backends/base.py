"""What every backend is, and the pieces the CLI backends share.

Every call takes a system prompt, a user prompt and a Pydantic model, and comes
back as one validated instance of that model, so the gateway does not care
which backend is live. No backend lets the model use tools: these are single
structured requests, not agent loops.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel

# How hard the model should think. Each backend maps it onto its own knob.
Effort = Literal["low", "medium", "high", "xhigh", "max"]

# How long one model call may take, whichever backend makes it, before it is
# abandoned and reported as failed.
CALL_TIMEOUT_S = 300.0


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
    the schema. That is not an exception here: the JD parser keeps its points
    untagged and the rewriter reports the bullet as failed, so both callers
    want to log the attempt first.
    """

    parsed: Any | None
    text: str
    usage: Usage
    # Whichever handle finds this call again in the provider's logs: an
    # Anthropic request id, or a Claude Code, Codex or pi session id.
    request_id: str | None = None
    # Backend-specific fields for the prompt archive header.
    meta: dict[str, Any] = field(default_factory=dict)


class Backend(ABC):
    """One way of reaching a model.

    ``name`` is the key it is stored and chosen under, ``label`` what the UI
    calls it, and ``concurrency`` how many calls a batch may run at once. An
    empty ``default_model`` means "whatever that CLI is configured to use".
    """

    name: ClassVar[str]
    label: ClassVar[str]
    concurrency: ClassVar[int] = 1
    # Whether the settings panel offers an API key field for this backend.
    accepts_key: ClassVar[bool] = False

    def __init__(self, default_model: str = "") -> None:
        self.default_model = default_model

    def set_key(self, key: str | None) -> None:  # noqa: B027 - optional, a no-op by default
        """A key saved in the settings panel; None falls back to the environment.
        Ignored by backends that sign in some other way."""

    def key_source(self) -> Literal["settings", "environment"] | None:
        """Where this backend's key comes from, or None if it has none (or needs none)."""
        return None

    @abstractmethod
    def unavailable(self) -> str | None:
        """Why this backend cannot run right now, or None if it is ready."""

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        effort: Effort,
    ) -> Completion:
        """One structured request; ``parsed`` is None if nothing validated."""


class CliBackend(Backend):
    """A backend that drives a local command-line tool: `binary` on PATH, or the
    path its RESUMAP_*_CLI setting names."""

    binary: ClassVar[str]

    def __init__(self, default_model: str, cli_path: str | None) -> None:
        super().__init__(default_model)
        self._cli = cli_path

    def _cli_path(self) -> str | None:
        if self._cli:
            return self._cli if Path(self._cli).exists() else None
        return shutil.which(self.binary)

    def _require_cli(self) -> str:
        cli = self._cli_path()
        if cli is None:
            raise RuntimeError(self.unavailable() or f"Could not find `{self.binary}`.")
        return cli


def json_lines(stdout: str) -> Iterator[dict[str, Any]]:
    """The events of a JSONL stream, skipping lines that are not a JSON object."""
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def stderr_tail(stderr: str) -> str:
    """The last line a CLI wrote to stderr: usually the one that says what went wrong."""
    return (stderr.strip().splitlines() or ["no output"])[-1]


def cli_env(dotenv_keys: Mapping[str, str]) -> dict[str, str]:
    """The environment for a CLI backend's subprocess.

    Credentials that came from Resumap's own ``.env`` are for the ``api``
    backend. A CLI that finds, say, ANTHROPIC_API_KEY in its environment will
    happily bill it per token instead of using its own sign-in, so any such
    key is dropped -- unless the shell set it too, with its own value.
    """
    env = dict(os.environ)
    for key, value in dotenv_keys.items():
        if key.endswith(("_API_KEY", "_AUTH_TOKEN")) and env.get(key) == value:
            env.pop(key, None)
    return env


async def run_cli(
    cmd: list[str],
    stdin_text: str | None,
    *,
    cwd: str,
    env: dict[str, str],
    timeout: float = CALL_TIMEOUT_S,
) -> tuple[int, str, str]:
    """Run a CLI backend once: (exit code, stdout, stderr).

    The child is killed if it outlives `timeout`, and also if the call is
    cancelled (the app quitting mid-batch), so it never keeps spending the
    plan with nobody waiting for the answer.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode() if stdin_text is not None else None), timeout
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"{os.path.basename(cmd[0])} gave no answer within {timeout:.0f}s"
        ) from exc
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
