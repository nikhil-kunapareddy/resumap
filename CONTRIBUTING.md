# Contributing to Resumap

Thanks for taking a look. Resumap is small and deliberately so: one process,
one SQLite file, no build step. This page is what you need to change it
safely. The design itself is in [docs/architecture.md](docs/architecture.md).

## Set up

```bash
uv sync --extra dev          # creates .venv from uv.lock, installs Resumap editable
python3 run.py               # serves the API and UI on 127.0.0.1:8000
```

Without uv: `python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"`.

A model backend is only needed from the Transform step on. Copy `.env.example`
to `.env` and pick one, or leave it and use the first three steps without one.

## Check your change

These are what CI runs, on Python 3.10 to 3.13:

```bash
uv run pytest                # the whole suite, a few seconds
uv run ruff check            # lint
uv run mypy                  # types
```

The tests never touch your `data/`, your `.env` settings or a real model:
every model call goes to a scripted fake backend, and every test gets its own
storage. The sample resume is fictional and generated at test time; please
keep it that way, and never commit a real resume, job description or `.env`.

## Where things go

Dependencies point one way, and `tests/test_architecture.py` enforces it:

```
api  ->  services  ->  llm, storage, rendering, parsing  ->  domain
```

* A **route** is a thin handler in `backend/resumap/api/routers/` that calls a
  service method and returns a model from `services/results.py`.
* A **use case** is a service method. Refuse a request by raising one of the
  errors in `services/errors.py`, never an HTTP exception; `api/errors.py`
  picks the status code.
* A **model call** is a response schema in `llm/schemas.py`, a pure function
  that builds its message, and a `ModelGateway` method that checks the reply.
* **SQL** lives only in `storage/repositories.py`. Wrap writes that must land
  together in `store.transaction()`.
* A **backend** is a `Backend` subclass in `llm/backends/`, registered in
  `default_registry()`.

## Things that must not change by accident

The suite pins these, and a failure there is information, not noise:

* **Prompts.** `tests/golden/prompts/` holds every prompt byte for byte. If you
  meant to change one, regenerate and read the diff:
  `RESUMAP_UPDATE_GOLDEN=1 uv run pytest tests/test_prompts_golden.py`.
* **How resumes are parsed.** `tests/golden/sample_resume_*.json` holds every
  block's id, kind and classification. Block ids key stored mappings and
  rewrites, so a change here is a change to users' data. Regenerate the same
  way, with `tests/test_resume_parse.py`.
* **The honesty rules.** JD points stay verbatim; anything a rewrite introduces
  is listed in `substitutions` with `grounded: false`; only accepted or edited
  wording reaches the PDF; the LinkedIn note is flagged when over 300
  characters, never cut. See section 6 of the architecture doc.
* **Privacy in logs.** `data/logs/resumap.jsonl` gets metadata only. Describe
  text with `obs.tmeta()`; never log resume or JD text.

Known defects are strict `xfail` tests. If your change fixes one, the run fails
until you remove its marker, which is the point: the fix gets noticed.

## Pull requests

* One change per PR, with the reason in the description.
* Add or update a test for any behaviour you change.
* Keep CI green: tests, ruff and mypy.

By contributing you agree that your work is released under the
[MIT License](LICENSE).
