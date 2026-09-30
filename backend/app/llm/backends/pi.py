"""The Pi backend: the local ``pi`` coding agent, in print mode.

Pi routes the call to whichever provider and model it is configured with
(``~/.pi/agent/settings.json``: ``defaultProvider`` / ``defaultModel``), using
its own stored sign-ins or the provider's API-key environment variable,
unless the settings panel names a ``provider/model``.

Pi has no structured-output option, so the JSON schema goes into the system
prompt and the reply is validated here. A reply that does not validate is sent
back once, with the validation error, before giving up.

Flags that keep it a single answer:

* ``--no-tools`` -- no read, bash, edit or write, and no extension tools.
* ``--no-session`` -- nothing saved.
* ``--no-context-files`` -- no AGENTS.md or CLAUDE.md from the working dir.
* ``--no-skills``, ``--no-prompt-templates``.

Extensions stay loaded, because providers themselves can be extensions (free
model gateways, local models); with tools off they have nothing to act with.

Pi exits 0 even when the provider call fails. The failure is on the final
assistant message -- ``stopReason: "error"`` with an ``errorMessage`` -- so
that is what is checked.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ValidationError

from .base import Backend, Completion, Usage, cli_env, run_cli

TIMEOUT_S = 300

# Resumap's effort levels onto pi's --thinking levels.
THINKING = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh",
            "max": "xhigh"}

JSON_RULES = """

OUTPUT FORMAT
Reply with one JSON object and nothing else: no prose before or after it, no
markdown code fence. It must validate against this JSON Schema:
{schema}"""

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def _final_message(stdout: str) -> tuple[dict, str | None]:
    """The last assistant message and the session id from pi's JSON event stream."""
    message: dict = {}
    session = None
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "session":
            session = ev.get("id")
        elif kind == "agent_end":
            msgs = [m for m in ev.get("messages") or [] if m.get("role") == "assistant"]
            if msgs:
                message = msgs[-1]
        elif kind == "turn_end" and (ev.get("message") or {}).get("role") == "assistant":
            message = ev["message"]
    return message, session


def _error_text(raw: str) -> str:
    """The human part of a provider error.

    Pi passes the provider's error body on as a JSON string, which often holds
    another JSON body in its own "message" field; unwrap until plain text.
    """
    text = raw or ""
    for _ in range(4):
        found = re.findall(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
        if not found:
            break
        try:
            text = json.loads(f'"{found[-1]}"')
        except json.JSONDecodeError:
            text = found[-1]
        if not text.lstrip().startswith("{"):
            break
    return re.sub(r"\s+", " ", text or "unknown error").strip()[:300]


def _json_text(text: str) -> str:
    """The JSON object in a reply that may have wrapped it in a fence or prose."""
    t = _FENCE.sub("", text.strip())
    start, end = t.find("{"), t.rfind("}")
    return t[start:end + 1] if start >= 0 and end > start else t


def _text(message: dict) -> str:
    return "".join(b.get("text", "") for b in message.get("content") or []
                   if b.get("type") == "text")


class PiBackend(Backend):
    name = "pi"
    label = "Pi"
    concurrency = 2

    def __init__(self, cli_path: str | None, dotenv_keys: Mapping[str, str]) -> None:
        """No default model: pi uses its own configured one unless the settings
        panel names a ``provider/model``. `cli_path` is RESUMAP_PI_CLI, if set."""
        super().__init__("")
        self._cli = cli_path
        self._dotenv_keys = dotenv_keys

    def _cli_path(self) -> str | None:
        if self._cli:
            return self._cli if Path(self._cli).exists() else None
        return shutil.which("pi")

    def unavailable(self) -> str | None:
        if self._cli_path() is None:
            return ("Could not find the `pi` command. Install the pi coding agent, "
                    "or point RESUMAP_PI_CLI at the binary.")
        # Whether pi's provider is signed in only shows on a real call; a failure
        # there comes back with the provider's own message.
        return None

    async def _ask(self, cli: str, system: str, prompt: str, model: str,
                   thinking: str) -> tuple[dict, str | None]:
        cmd = [cli, "-p", "--mode", "json", "--no-tools", "--no-session",
               "--no-context-files", "--no-skills", "--no-prompt-templates",
               "--thinking", thinking, "--system-prompt", system]
        if model:
            cmd += ["--model", model]
        # A message starting with "@" is read as a file to attach; ours start with
        # a tag, but a leading newline makes that impossible regardless.
        cmd.append("\n" + prompt)
        with tempfile.TemporaryDirectory(prefix="resumap-pi-") as tmp:
            code, stdout, stderr = await run_cli(cmd, None, cwd=tmp, env=cli_env(self._dotenv_keys),
                                                 timeout=TIMEOUT_S)
        message, session = _final_message(stdout)
        if not message:
            tail = (stderr.strip().splitlines() or ["no output"])[-1]
            raise RuntimeError(f"Pi returned no answer (exit {code}): {tail}")
        if message.get("stopReason") == "error":
            where = "/".join(x for x in (message.get("provider"), message.get("model")) if x)
            raise RuntimeError(f"Pi ({where or 'default model'}): "
                               f"{_error_text(message.get('errorMessage', ''))}")
        return message, session

    async def complete(self, *, model: str, system: str, user: str,
                       schema: type[BaseModel], max_tokens: int,
                       effort: str) -> Completion:
        cli = self._cli_path()
        if cli is None:
            raise RuntimeError(self.unavailable() or "pi not found")
        thinking = THINKING.get(effort, "medium")
        full_system = system + JSON_RULES.format(
            schema=json.dumps(schema.model_json_schema(), separators=(",", ":")))

        totals: dict[str, float] = {"input": 0, "output": 0, "cacheRead": 0, "cost": 0.0}
        parsed, text, session, message = None, "", None, {}
        prompt = user
        attempts = 0
        # max_tokens has no pi flag; the model's own cap applies.
        for attempts in (1, 2):
            message, session = await self._ask(cli, full_system, prompt, model, thinking)
            u = message.get("usage") or {}
            for k in ("input", "output", "cacheRead"):
                totals[k] += int(u.get(k) or 0)
            totals["cost"] += float((u.get("cost") or {}).get("total") or 0.0)
            text = _text(message)
            try:
                parsed = schema.model_validate_json(_json_text(text))
                break
            except ValidationError as exc:
                # No session is kept, so the retry has to carry its own context.
                prompt = (f"{user}\n\n<previous_reply>\n{text}\n</previous_reply>\n"
                          f"That reply could not be used: {exc.errors()[:3]}\n"
                          "Reply again with only the corrected JSON object.")

        used = "/".join(x for x in (message.get("provider"), message.get("model")) if x)
        return Completion(
            parsed=parsed,
            text=text,
            usage=Usage(input_tokens=int(totals["input"]),
                        output_tokens=int(totals["output"]),
                        cache_read_input_tokens=int(totals["cacheRead"])),
            request_id=session,
            meta={"effort": thinking, "model": used or model or "pi default",
                  "attempts": attempts, "reported_cost_usd": round(totals["cost"], 6)},
        )
