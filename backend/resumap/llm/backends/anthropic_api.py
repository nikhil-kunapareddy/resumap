"""The Anthropic API backend: one ``messages.parse`` call per request."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anthropic
from pydantic import BaseModel

from .base import Backend, Completion, Effort, Usage


class AnthropicApiBackend(Backend):
    name = "api"
    label = "Anthropic API key"
    # Plain HTTP requests, so the ceiling is the account's rate limit rather
    # than anything local.
    concurrency = 4

    def __init__(self, default_model: str, api_key: Callable[[], str | None]) -> None:
        """`default_model` is Resumap's own choice (RESUMAP_MODEL), overridable in
        the settings panel. `api_key` is read at call time, so a key added to the
        environment later is picked up."""
        super().__init__(default_model)
        self._api_key = api_key

    def unavailable(self) -> str | None:
        if self._api_key() is None:
            return (
                "No ANTHROPIC_API_KEY found. Add it to the .env file in the "
                "project root, then restart the app. To bill a Claude "
                "subscription through your local Claude Code install instead, "
                "set RESUMAP_BACKEND=claude_code."
            )
        return None

    @staticmethod
    def _response_text(resp: Any) -> str:
        """The raw text block, for the prompt archive."""
        try:
            return next((b.text for b in resp.content if b.type == "text"), "")
        except Exception:  # noqa: BLE001
            return ""

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
        client = anthropic.AsyncAnthropic(api_key=self._api_key(), max_retries=3)
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
            text=self._response_text(resp),
            usage=Usage(
                input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            ),
            request_id=getattr(resp, "_request_id", None),
            meta={"effort": effort},
        )
