"""Structured logging, cost accounting and prompt archiving.

Two separate stores, because the operational log and the model audit trail have
different privacy profiles:

* ``data/logs/resumap.jsonl`` -- every event, strictly metadata. Text is reduced
  to a length, a word count and a short hash. No resume content, no contact
  details. Safe to paste into a bug report.
* ``data/logs/prompts/`` -- one file per API call with the prompt and completion
  verbatim. Unavoidably contains resume and JD text, so it is a separate,
  size-capped directory that can be pruned or switched off outright.

Set ``RESUMAP_LOG_TEXT=1`` to also include truncated text samples in the JSONL
while debugging locally.
"""
from __future__ import annotations

import contextvars
import getpass
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("run_id", default="-")

LOGGER_NAME = "resumap"
_log = logging.getLogger(LOGGER_NAME)
_configured = False

# Anthropic list prices, USD per token. Used for local cost accounting only.
PRICES = {
    "claude-opus-5-5": (4.0 / 1e6, 20.0 / 1e6),
    "claude-opus-5": (5.0 / 1e6, 25.0 / 1e6),
    "claude-opus-4-8": (5.0 / 1e6, 25.0 / 1e6),
    "claude-sonnet-5": (2.0 / 1e6, 10.0 / 1e6),
    "claude-haiku-4-5": (1.0 / 1e6, 5.0 / 1e6),
    "claude-fable-5-1": (10.0 / 1e6, 50.0 / 1e6),
}
_DEFAULT_PRICE = (5.0 / 1e6, 25.0 / 1e6)

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
URL_RE = re.compile(r"https?://\S+|www\.\S+")


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------

def _log_text_enabled() -> bool:
    return os.getenv("RESUMAP_LOG_TEXT", "0").strip() not in ("", "0", "false", "no")


def _prompts_enabled() -> bool:
    return os.getenv("RESUMAP_LOG_PROMPTS", "1").strip() not in ("0", "false", "no")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds"),
            "lvl": record.levelname,
            "ev": getattr(record, "ev", "log"),
            "run": getattr(record, "run_id", "-"),
        }
        out.update(getattr(record, "fields", {}) or {})
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        msg = record.getMessage()
        if msg and msg != getattr(record, "ev", None):
            out["msg"] = msg
        return json.dumps(out, default=str, ensure_ascii=False)


_LEVEL_TAG = {"DEBUG": "DBG ", "INFO": "INFO", "WARNING": "WARN",
              "ERROR": "ERR ", "CRITICAL": "CRIT"}


class ConsoleFormatter(logging.Formatter):
    """One compact line per event: time, level, area, then the fields."""

    NOISY = {"run_id", "ev", "fields"}

    def format(self, record: logging.LogRecord) -> str:
        ev = getattr(record, "ev", "log")
        area = ev.split(".")[0]
        fields = dict(getattr(record, "fields", {}) or {})
        fields.pop("run", None)
        parts = " ".join(f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None)
        stamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        tag = _LEVEL_TAG.get(record.levelname, record.levelname)
        line = f"{stamp} {tag}  {area:<7} {ev.split('.', 1)[-1]:<12} {parts}".rstrip()
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        # Plain digits: scientific notation for a millisecond count ("1.034e+04")
        # is unreadable in a log you are skimming.
        return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}"
    if isinstance(v, (list, tuple)):
        return "[" + ",".join(str(x) for x in v[:6]) + ("…]" if len(v) > 6 else "]")
    if isinstance(v, dict):
        return "{" + ",".join(f"{k}:{_fmt(x)}" for k, x in list(v.items())[:4]) + "}"
    return str(v)


def setup(log_dir: Path, level: str = "INFO") -> None:
    """Attach a rotating JSONL handler and a readable console handler."""
    global _configured
    if _configured:
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    _log.setLevel(getattr(logging, level.upper(), logging.INFO))
    _log.propagate = False

    jsonl = RotatingFileHandler(
        log_dir / "resumap.jsonl", maxBytes=8_000_000, backupCount=5,
        encoding="utf-8",
    )
    jsonl.setFormatter(JsonFormatter())
    _log.addHandler(jsonl)

    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    _log.addHandler(console)
    _configured = True


# --------------------------------------------------------------------------
# emitting events
# --------------------------------------------------------------------------

def event(ev: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event. Never raises -- logging must not break a request."""
    try:
        _log.log(level, ev, extra={
            "ev": ev,
            "run_id": run_id_var.get(),
            "fields": {k: v for k, v in fields.items() if v is not None},
        })
    except Exception:  # noqa: BLE001 - a broken log line is not worth a 500
        pass


def warn(ev: str, **fields: Any) -> None:
    event(ev, logging.WARNING, **fields)


def error(ev: str, exc: BaseException | None = None, **fields: Any) -> None:
    try:
        _log.error(ev, exc_info=exc, extra={
            "ev": ev, "run_id": run_id_var.get(),
            "fields": {k: v for k, v in fields.items() if v is not None},
        })
    except Exception:  # noqa: BLE001
        pass


def new_run_id() -> str:
    rid = uuid.uuid4().hex[:12]
    run_id_var.set(rid)
    return rid


class timer:
    """Context manager that reports elapsed milliseconds."""

    def __init__(self) -> None:
        self.ms = 0.0
        self._t = 0.0

    def __enter__(self) -> "timer":
        self._t = time.perf_counter()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.ms = round((time.perf_counter() - self._t) * 1000, 1)


# --------------------------------------------------------------------------
# metadata-only text description
# --------------------------------------------------------------------------

def tmeta(text: str | None, prefix: str = "text") -> dict[str, Any]:
    """Describe text without reproducing it.

    The hash makes it possible to tell "the same bullet" from "a different
    bullet" across events without ever writing the bullet down.
    """
    s = text or ""
    out: dict[str, Any] = {
        f"{prefix}_len": len(s),
        f"{prefix}_words": len(s.split()),
        f"{prefix}_sha": hashlib.sha256(s.encode("utf-8")).hexdigest()[:8],
    }
    if _log_text_enabled():
        out[f"{prefix}_sample"] = scrub(s)[:80]
    return out


def path_str(path: Any) -> str:
    """Log paths relative to home.

    An absolute path leaks the account name -- "/Users/<firstname><lastname>/..."
    put the user's name into a log that is otherwise free of personal data.
    """
    try:
        text = str(path)
        home = str(Path.home())
        if text.startswith(home):
            text = "~" + text[len(home):]
        # The account name can also appear mid-path (a temp dir named after the
        # project location, a mounted volume), so mask it wherever it occurs.
        user = getpass.getuser()
        if user and len(user) > 2:
            text = text.replace(user, "<user>")
        return text
    except Exception:  # noqa: BLE001
        return str(path)


def scrub(text: str) -> str:
    """Mask contact details, for the rare case text is logged deliberately."""
    s = EMAIL_RE.sub("<EMAIL>", text or "")
    s = URL_RE.sub("<URL>", s)
    return PHONE_RE.sub("<PHONE>", s)


# --------------------------------------------------------------------------
# cost accounting
# --------------------------------------------------------------------------

class _Totals:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cached_tokens = 0
        self.cost = 0.0
        self.failures = 0
        self.by_op: dict[str, dict[str, Any]] = {}


_totals = _Totals()


def price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Anthropic list price for the call; 0 for another provider's model.

    An unlisted Claude model is priced at the default rather than skipped, so a
    new one still shows up in the totals until PRICES learns it. Codex and pi
    models are not priced here: their usage is logged, their cost is not.
    """
    rates = PRICES.get(model)
    if rates is None:
        rates = _DEFAULT_PRICE if model.startswith("claude") else (0.0, 0.0)
    rin, rout = rates
    return input_tokens * rin + output_tokens * rout


def record_usage(op: str, model: str, usage: Any, ms: float,
                 request_id: str | None = None, **extra: Any) -> dict[str, Any]:
    """Log one API call's tokens, cost and latency; update running totals."""
    tin = int(getattr(usage, "input_tokens", 0) or 0)
    tout = int(getattr(usage, "output_tokens", 0) or 0)
    cached = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    cost = price(model, tin, tout)

    with _totals.lock:
        _totals.calls += 1
        _totals.input_tokens += tin
        _totals.output_tokens += tout
        _totals.cached_tokens += cached
        _totals.cost += cost
        slot = _totals.by_op.setdefault(op, {"calls": 0, "cost": 0.0,
                                             "in": 0, "out": 0})
        slot["calls"] += 1
        slot["cost"] += cost
        slot["in"] += tin
        slot["out"] += tout
        running = _totals.cost
        ncalls = _totals.calls

    event("api.call", op=op, model=model, input_tokens=tin, output_tokens=tout,
          cached_tokens=cached, cost_usd=round(cost, 5), ms=ms,
          request_id=request_id, session_calls=ncalls,
          session_cost_usd=round(running, 4), **extra)
    return {"input_tokens": tin, "output_tokens": tout, "cost_usd": cost,
            "ms": ms, "request_id": request_id}


def record_failure(op: str, exc: BaseException, ms: float,
                   request_id: str | None = None, **extra: Any) -> None:
    with _totals.lock:
        _totals.failures += 1
    error("api.failed", exc=exc, op=op, error=type(exc).__name__,
          detail=str(exc)[:300], ms=ms, request_id=request_id, **extra)


def totals() -> dict[str, Any]:
    with _totals.lock:
        return {
            "calls": _totals.calls,
            "failures": _totals.failures,
            "input_tokens": _totals.input_tokens,
            "output_tokens": _totals.output_tokens,
            "cached_tokens": _totals.cached_tokens,
            "cost_usd": round(_totals.cost, 4),
            "by_op": {k: {**v, "cost": round(v["cost"], 4)}
                      for k, v in _totals.by_op.items()},
        }


# --------------------------------------------------------------------------
# prompt archive (contains resume/JD text by nature -- kept separate)
# --------------------------------------------------------------------------

def _archive_dir(log_dir: Path) -> Path:
    d = log_dir / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune(directory: Path, cap_mb: float) -> None:
    files = sorted(directory.glob("*.txt"), key=lambda p: p.stat().st_mtime)
    total = sum(f.stat().st_size for f in files)
    cap = cap_mb * 1024 * 1024
    while files and total > cap:
        victim = files.pop(0)
        total -= victim.stat().st_size
        victim.unlink(missing_ok=True)


def archive_prompt(log_dir: Path, op: str, system: str, user: str,
                   response: str, meta: dict[str, Any] | None = None) -> str | None:
    """Write one call's full prompt and completion. Returns the filename."""
    if not _prompts_enabled():
        return None
    try:
        directory = _archive_dir(log_dir)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}_{op}_{run_id_var.get()}_{uuid.uuid4().hex[:6]}.txt"
        body = [
            f"# op: {op}",
            f"# run: {run_id_var.get()}",
            f"# time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        ]
        for k, v in (meta or {}).items():
            body.append(f"# {k}: {v}")
        body += ["", "=" * 70, "SYSTEM", "=" * 70, system or "",
                 "", "=" * 70, "USER", "=" * 70, user or "",
                 "", "=" * 70, "RESPONSE", "=" * 70, response or ""]
        path = directory / name
        path.write_text("\n".join(body), encoding="utf-8")
        _prune(directory, float(os.getenv("RESUMAP_PROMPT_ARCHIVE_MB", "64")))
        return name
    except Exception:  # noqa: BLE001 - archiving is best-effort
        return None
