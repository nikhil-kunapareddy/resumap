"""The Claude Code backend: the local ``claude`` install, via the Agent SDK.

Credentials come from that install rather than from ``.env``, so a Claude
subscription login works and no API key is needed. The subscription's rate
limits are shared with interactive Claude Code use, so a large transform here
spends the same budget as a session in the terminal.

Five options keep this a single structured request instead of a coding agent:

* ``tools=[]`` -- no file, shell or web access. Claude Code otherwise prepends
  its tool definitions to every request, measured at ~11k extra input tokens
  per bullet against ~1k with them off.
* ``setting_sources=[]`` -- ignore CLAUDE.md, hooks, skills and MCP servers,
  both the project's and ``~/.claude``. None of them have anything to add to a
  bullet rewrite, and they would vary the prompt from machine to machine.
* ``strict_mcp_config`` -- load no MCP servers at all. ``setting_sources=[]``
  skips the ones in settings files but not the account's claude.ai
  connectors, whose sign-in status the CLI then injects into the
  conversation; a free-text field once came back ending with a note that the
  "PDF Viewer" connector needed authorizing.
* ``system_prompt`` as a plain string -- replaces Claude Code's own prompt
  rather than appending to it.
* ``env`` -- blanks out ``ANTHROPIC_API_KEY``, which ``load_dotenv`` puts in
  this process's environment. The subprocess inherits our environment, so
  without this the CLI would find that key and bill per token, defeating the
  point of choosing this backend. An empty value reads as unset, leaving the
  CLI to fall back to its own stored credentials.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .base import CALL_TIMEOUT_S, CliBackend, Completion, Effort, Usage

# One turn to answer plus one to emit the structured output, and a little room
# for the SDK to re-prompt itself after a schema mismatch.
MAX_TURNS = 4

# The CLI would otherwise inherit these from us and prefer them over the
# subscription login the user chose this backend to get.
_SUPPRESS_ENV = {"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""}


class ClaudeCodeBackend(CliBackend):
    name = "claude_code"
    label = "Claude Code"
    binary = "claude"
    # Every call spawns a `claude` subprocess, so the ceiling here is local
    # memory rather than an HTTP rate limit. Deliberately lower than the API
    # backend's.
    concurrency = 2

    def __init__(self, default_model: str, cli_path: str | None, cwd: Path) -> None:
        """`default_model` is Resumap's own choice (RESUMAP_MODEL), overridable in
        the settings panel; `cli_path` is RESUMAP_CLAUDE_CLI, if set."""
        super().__init__(default_model, cli_path)
        self._cwd = cwd

    def unavailable(self) -> str | None:
        if importlib.util.find_spec("claude_agent_sdk") is None:
            return (
                "The claude-agent-sdk package is not installed. Run `uv sync` "
                "(or `pip install -e .`), or set RESUMAP_BACKEND=api "
                "to use an Anthropic API key instead."
            )
        if self._cli_path() is None:
            return (
                "Could not find the `claude` command. Install Claude Code and "
                "sign in, point RESUMAP_CLAUDE_CLI at the binary if it is not "
                "on this process's PATH, or set RESUMAP_BACKEND=api to use an "
                "Anthropic API key instead."
            )
        return None

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
        from claude_agent_sdk import ClaudeAgentOptions

        # max_tokens has no Agent SDK equivalent -- the CLI applies the model's own
        # output cap, which is well above anything one bullet needs.
        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            output_format={"type": "json_schema", "schema": schema.model_json_schema()},
            effort=effort,
            tools=[],
            setting_sources=[],
            strict_mcp_config=True,
            max_turns=MAX_TURNS,
            cwd=self._cwd,
            cli_path=self._cli_path(),
            env=dict(_SUPPRESS_ENV),
        )

        try:
            result = await asyncio.wait_for(_result(user, options), CALL_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(f"Claude Code gave no answer within {CALL_TIMEOUT_S:.0f}s") from exc
        if result is None:
            raise RuntimeError("Claude Code exited without returning a result.")

        parsed = None
        if result.subtype == "success" and result.structured_output is not None:
            try:
                parsed = schema.model_validate(result.structured_output)
            except ValidationError:
                # The SDK validates against the JSON schema and re-prompts on a
                # mismatch, so reaching here means it gave up. Leave parsed None
                # and let the caller log the failed attempt.
                parsed = None

        u = result.usage or {}
        return Completion(
            parsed=parsed,
            # Structured runs put the JSON in `result`; fall back to the object so
            # the archive is never blank.
            text=result.result or json.dumps(result.structured_output, indent=2, default=str),
            usage=Usage(
                # Claude Code caches its prompt prefix, which lands nearly all the
                # input under cache_creation on a first call. Folding it in keeps
                # the logged token count comparable to the API backend's.
                input_tokens=(
                    int(u.get("input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)
                ),
                output_tokens=int(u.get("output_tokens") or 0),
                cache_read_input_tokens=int(u.get("cache_read_input_tokens") or 0),
            ),
            # `claude --resume <id>` reopens this exact call.
            request_id=result.session_id,
            meta={
                "effort": effort,
                "subtype": result.subtype,
                "turns": result.num_turns,
                "reported_cost_usd": result.total_cost_usd,
                "errors": result.errors or None,
            },
        )


async def _result(user: str, options: Any) -> Any:
    """The run's ResultMessage, or None if the CLI ended without one.

    The stream is closed however the loop ends, so a timeout or cancellation
    shuts the `claude` subprocess down rather than leaving it running.
    """
    from claude_agent_sdk import ResultMessage, query

    result = None
    messages = query(prompt=user, options=options)
    try:
        async for message in messages:
            if isinstance(message, ResultMessage):
                result = message
    finally:
        aclose = getattr(messages, "aclose", None)
        if aclose is not None:
            await aclose()
    return result
