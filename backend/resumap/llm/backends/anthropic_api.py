"""The Anthropic API backend: one ``messages.parse`` call per request."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from .base import Backend, Completion, Effort, Usage


class AnthropicApiBackend(Backend):
    name = "api"
    label = "Anthropic API key"
    # Plain HTTP requests, so the ceiling is the account's rate limit rather
    # than anything local.
    concurrency = 4
    accepts_key = True

    def __init__(self, default_model: str, api_key: Callable[[], str | None]) -> None:
        """`default_model` is Resumap's own choice (RESUMAP_MODEL), overridable in
        the settings panel. `api_key` (the environment's ANTHROPIC_API_KEY) is
        read at call time, so a key added to the environment later is picked
        up; a key saved in the settings panel wins over it."""
        super().__init__(default_model)
        self._api_key = api_key
        self._saved_key: str | None = None

    def set_key(self, key: str | None) -> None:
        # Held here only, never put in os.environ, so no CLI backend's
        # subprocess can inherit it and bill it.
        self._saved_key = (key or "").strip() or None

    def key_source(self) -> Literal["settings", "environment"] | None:
        if self._saved_key:
            return "settings"
        return "environment" if self._api_key() else None

    def _key(self) -> str | None:
        return self._saved_key or self._api_key()

    def unavailable(self) -> str | None:
        if self._key() is None:
            return (
                "No API key yet. Paste one into the API key field in Settings, "
                "or set ANTHROPIC_API_KEY in .env. To use a Claude subscription "
                "through your local Claude Code install instead, choose Claude Code."
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
        client = anthropic.AsyncAnthropic(api_key=self._key(), max_retries=3)
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
