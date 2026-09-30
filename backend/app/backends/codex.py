"""The Codex backend: the local OpenAI Codex CLI, via ``codex exec``.

Credentials come from that install (``codex login``), so a ChatGPT plan works
and no API key is needed; the work counts against that plan's limits, shared
with interactive Codex use. The model is whatever Codex is configured with
(``~/.codex/config.toml``) unless the settings panel names one.

``codex exec`` is a coding agent, so the flags below are what keep it to a
single structured answer:

* ``--output-schema`` -- Codex constrains its final message to the schema. It
  uses OpenAI's strict mode, which needs every object closed and every property
  required, so ``_strict()`` rewrites the Pydantic schema into that form.
* ``--sandbox read-only`` and an empty temporary working directory -- Codex
  keeps its shell tool whatever we do; this leaves it nothing to change and
  nothing to read, and no AGENTS.md to pick up.
* ``--ephemeral`` -- no session files.
* There is no system-prompt flag, so the system prompt leads the prompt text.

The answer, the usage and any error all arrive as JSONL events on stdout
(``--json``). A failed turn -- an expired login, say -- is a ``turn.failed``
event, which is raised with Codex's own message.
"""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from ..config import CODEX_CLI
from . import Completion, Usage, cli_env, run_cli

NAME = "codex"
LABEL = "Codex"
# Codex's own configured model.
DEFAULT_MODEL = ""

# One `codex` process per call, as with Claude Code.
CONCURRENCY = 2

TIMEOUT_S = 300

# Resumap's effort levels onto Codex's model_reasoning_effort.
EFFORT = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high",
          "max": "high"}

# `codex login status` spawns a process; the status is asked for on every
# request, so it is remembered briefly.
_LOGIN_TTL_S = 60.0
_login_cache: dict[str, Any] = {"at": 0.0, "why": None}


def _cli_path() -> str | None:
    if CODEX_CLI:
        return CODEX_CLI if Path(CODEX_CLI).exists() else None
    return shutil.which("codex")


def unavailable() -> str | None:
    cli = _cli_path()
    if cli is None:
        return ("Could not find the `codex` command. Install the Codex CLI and "
                "run `codex login`, or point RESUMAP_CODEX_CLI at the binary.")
    now = time.monotonic()
    if now - _login_cache["at"] > _LOGIN_TTL_S:
        try:
            res = subprocess.run([cli, "login", "status"], capture_output=True,
                                 text=True, timeout=15, env=cli_env())
            why = None if res.returncode == 0 else (
                "Codex is not signed in. Run `codex login` in a terminal.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            why = f"Could not ask Codex whether it is signed in: {exc}"
        _login_cache.update(at=now, why=why)
    return _login_cache["why"]


def _strict(schema: dict) -> dict:
    """The Pydantic schema in the shape OpenAI's strict mode accepts.

    References are inlined (a ``$ref`` may not carry the description Pydantic
    puts beside it), every object is closed, and every property is required --
    all our fields are required already, so this changes no meaning.
    """
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(v) for v in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            target = defs[node["$ref"].rsplit("/", 1)[-1]]
            rest = {k: v for k, v in node.items() if k != "$ref"}
            return walk({**copy.deepcopy(target), **rest})
        out = {k: walk(v) for k, v in node.items() if k not in ("$defs", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(schema)


def _read_events(stdout: str) -> dict[str, Any]:
    """The answer, usage, thread id and error from a `codex exec --json` stream."""
    found: dict[str, Any] = {"text": "", "usage": {}, "thread": None, "error": None}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "thread.started":
            found["thread"] = ev.get("thread_id")
        elif kind == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
            found["text"] = ev["item"].get("text") or ""
        elif kind == "turn.completed":
            found["usage"] = ev.get("usage") or {}
        elif kind == "turn.failed":
            found["error"] = (ev.get("error") or {}).get("message") or "turn failed"
        elif kind == "error" and not found["error"]:
            found["error"] = ev.get("message") or "error"
    return found


async def complete(*, model: str, system: str, user: str,
                   schema: type[BaseModel], max_tokens: int,
                   effort: str) -> Completion:
    cli = _cli_path()
    if cli is None:
        raise RuntimeError(unavailable() or "codex not found")
    reasoning = EFFORT.get(effort, "medium")
    prompt = (f"{system}\n\n---\n\n{user}\n\n---\nAnswer directly with the JSON "
              "object. Do not run commands or read files: everything you need "
              "is above.")

    # max_tokens has no `codex exec` equivalent; the model's own cap applies.
    with tempfile.TemporaryDirectory(prefix="resumap-codex-") as tmp:
        schema_path = Path(tmp) / "schema.json"
        schema_path.write_text(json.dumps(_strict(schema.model_json_schema())))
        cmd = [cli, "exec", "--json", "--output-schema", str(schema_path),
               "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
               "--color", "never", "-C", tmp,
               "-c", f'model_reasoning_effort="{reasoning}"']
        if model:
            cmd += ["-m", model]
        cmd.append("-")  # the prompt comes on stdin
        code, stdout, stderr = await run_cli(cmd, prompt, cwd=tmp, env=cli_env(),
                                             timeout=TIMEOUT_S)

    ev = _read_events(stdout)
    if ev["error"] and not ev["text"]:
        raise RuntimeError(f"Codex: {ev['error']}")
    if code != 0 and not ev["text"]:
        tail = (stderr.strip().splitlines() or ["no output"])[-1]
        raise RuntimeError(f"Codex exited with status {code}: {tail}")

    parsed = None
    try:
        parsed = schema.model_validate_json(ev["text"])
    except ValidationError:
        # Strict mode makes this rare; the caller logs the failed attempt.
        parsed = None

    u = ev["usage"]
    cached = int(u.get("cached_input_tokens") or 0)
    return Completion(
        parsed=parsed,
        text=ev["text"],
        usage=Usage(
            # OpenAI counts cached tokens inside input_tokens.
            input_tokens=max(0, int(u.get("input_tokens") or 0) - cached),
            output_tokens=int(u.get("output_tokens") or 0),
            cache_read_input_tokens=cached,
        ),
        # `codex exec resume <id>` would reopen it, were it not ephemeral.
        request_id=ev["thread"],
        meta={"effort": reasoning, "model": model or "codex default",
              "exit_code": code},
    )
