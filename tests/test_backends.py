"""The backends' own plumbing: what reaches the provider, and what comes back.

The gateway tests run against tests/fakes.FakeBackend; these cover the parts
of the real backends that need no provider at all.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from types import SimpleNamespace

import pytest

from resumap import obs
from resumap.llm.backends import AnthropicApiBackend, CodexBackend
from resumap.llm.backends.base import run_cli
from resumap.llm.schemas import Ping


def test_a_cancelled_cli_call_kills_its_process(tmp_path):
    pidfile = tmp_path / "pid"
    script = (
        "import os, pathlib, time\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )

    async def go() -> None:
        task = asyncio.create_task(
            run_cli([sys.executable, "-c", script], None, cwd=str(tmp_path), env=dict(os.environ))
        )
        deadline = time.monotonic() + 10
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_a_signed_out_codex_is_asked_again_and_a_signed_in_one_is_remembered(tmp_path):
    flag = tmp_path / "signed-in"
    cli = tmp_path / "codex"
    cli.write_text(f'#!/bin/sh\n[ -f "{flag}" ] && exit 0 || exit 1\n')
    cli.chmod(0o755)
    codex = CodexBackend(str(cli), {})

    assert "not signed in" in (codex.unavailable() or "")
    flag.touch()
    assert codex.unavailable() is None
    flag.unlink()
    assert codex.unavailable() is None


class _Messages:
    def __init__(self, text: str) -> None:
        self.text = text
        self.sent: dict = {}

    async def create(self, **kwargs):
        self.sent = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=11, output_tokens=7, cache_read_input_tokens=0),
            _request_id="req_1",
        )


def _api_call(monkeypatch, reply: str):
    api = AnthropicApiBackend("fake-model", lambda: "sk-test")
    messages = _Messages(reply)
    monkeypatch.setattr(api, "_client_for", lambda key: SimpleNamespace(messages=messages))
    out = asyncio.run(
        api.complete(
            model="fake-model", system="s", user="u", schema=Ping, max_tokens=50, effort="low"
        )
    )
    return out, messages.sent


def test_an_api_reply_is_validated_against_the_schema_it_was_asked_for(monkeypatch):
    out, sent = _api_call(monkeypatch, '{"ok": true, "model": "m"}')
    assert out.parsed == Ping(ok=True, model="m")
    assert sent["output_config"]["format"]["type"] == "json_schema"
    assert sent["output_config"]["effort"] == "low"


def test_an_api_reply_that_does_not_validate_is_data_not_an_exception(monkeypatch):
    out, _ = _api_call(monkeypatch, '{"ok": tru')  # cut off at max_tokens
    assert out.parsed is None
    assert out.text == '{"ok": tru'
    assert (out.usage.input_tokens, out.usage.output_tokens) == (11, 7)
    assert out.request_id == "req_1"


def test_the_api_backend_keeps_one_client_per_key():
    api = AnthropicApiBackend("fake-model", lambda: None)
    assert api._client_for("sk-a") is api._client_for("sk-a")
    assert api._client_for("sk-b") is not api._client_for("sk-a")


def test_the_prompt_archive_stays_under_its_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUMAP_LOG_PROMPTS", "1")
    monkeypatch.setenv("RESUMAP_PROMPT_ARCHIVE_MB", "0.01")
    for _ in range(40):
        assert obs.archive_prompt(tmp_path, "rewrite", "system", "u" * 1000, "reply")
    files = list((tmp_path / "prompts").iterdir())
    assert 0 < len(files) < 40
    assert sum(f.stat().st_size for f in files) <= 0.01 * 1024 * 1024


def test_a_malformed_archive_cap_falls_back_to_the_default(tmp_path, monkeypatch):
    monkeypatch.setenv("RESUMAP_LOG_PROMPTS", "1")
    monkeypatch.setenv("RESUMAP_PROMPT_ARCHIVE_MB", "64MB")
    assert obs.archive_prompt(tmp_path, "rewrite", "system", "user", "reply")
    assert obs._archive_cap("64MB") == 64 * 1024 * 1024
