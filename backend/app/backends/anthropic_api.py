"""The Anthropic API backend: one ``messages.parse`` call per request."""
from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel

from ..config import MODEL, api_key
from . import Completion, Usage

NAME = "api"
LABEL = "Anthropic API key"
# Resumap's own choice (RESUMAP_MODEL), overridable in the settings panel.
DEFAULT_MODEL = MODEL

# Plain HTTP requests, so the ceiling is the account's rate limit rather than
# anything local.
CONCURRENCY = 4


def unavailable() -> str | None:
    if api_key() is None:
        return ("No ANTHROPIC_API_KEY found. Add it to the .env file in the "
                "project root, then restart the app. To bill a Claude "
                "subscription through your local Claude Code install instead, "
                "set RESUMAP_BACKEND=claude_code.")
    return None


def _response_text(resp: Any) -> str:
    """The raw text block, for the prompt archive."""
    try:
        return next((b.text for b in resp.content if b.type == "text"), "")
    except Exception:  # noqa: BLE001
        return ""


async def complete(*, model: str, system: str, user: str,
                   schema: type[BaseModel], max_tokens: int,
                   effort: str) -> Completion:
    client = anthropic.AsyncAnthropic(api_key=api_key(), max_retries=3)
    resp = await client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        system=system,
        output_config={"effort": effort},
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    u = resp.usage
    return Completion(
        parsed=resp.parsed_output,
        text=_response_text(resp),
        usage=Usage(
            input_tokens=int(getattr(u, "input_tokens", 0) or 0),
            output_tokens=int(getattr(u, "output_tokens", 0) or 0),
            cache_read_input_tokens=int(
                getattr(u, "cache_read_input_tokens", 0) or 0),
        ),
        request_id=getattr(resp, "_request_id", None),
        meta={"effort": effort},
    )
