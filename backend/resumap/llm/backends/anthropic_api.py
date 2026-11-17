"""The Anthropic API backend: one structured ``messages.create`` call per request.

The reply is validated here rather than by the SDK's ``messages.parse``, which
raises on a reply that does not fit the schema (cut off at max_tokens, say).
That exception would lose the call's usage and prompt archive, and its message
echoes the reply, so resume text would reach the metadata-only log.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from .base import CALL_TIMEOUT_S, Backend, Completion, Effort, Usage


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
        # One client, and so one connection pool, per key in use.
        self._client: tuple[str | None, anthropic.AsyncAnthropic] | None = None

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

    def _client_for(self, key: str | None) -> anthropic.AsyncAnthropic:
        if self._client is None or self._client[0] != key:
            self._client = (
                key,
                anthropic.AsyncAnthropic(api_key=key, max_retries=3, timeout=CALL_TIMEOUT_S),
            )
        return self._client[1]

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
        resp = await self._client_for(self._key()).messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            output_config={
                "effort": effort,
                "format": {
                    "type": "json_schema",
                    "schema": anthropic.transform_schema(schema.model_json_schema()),
                },
            },
            messages=[{"role": "user", "content": user}],
        )
        text = self._response_text(resp)
        try:
            parsed: BaseModel | None = schema.model_validate_json(text)
        except ValidationError:
            parsed = None
        u = resp.usage
        return Completion(
            parsed=parsed,
            text=text,
            usage=Usage(
                input_tokens=int(getattr(u, "input_tokens", 0) or 0),
                output_tokens=int(getattr(u, "output_tokens", 0) or 0),
                cache_read_input_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            ),
            request_id=getattr(resp, "_request_id", None),
            meta={"effort": effort},
        )
