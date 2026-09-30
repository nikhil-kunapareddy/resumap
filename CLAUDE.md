# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Resumap is a local, single-user tool: upload a reference resume once, paste a job description, map resume bullets onto the JD points they should answer, let Claude realign the wording, export a layout-mirroring one-page PDF, then draft the recruiter outreach that goes with it. Everything runs on `127.0.0.1` against a local SQLite file.

## Commands

```bash
# setup (once)
uv venv && uv pip install -r backend/requirements.txt

# run: re-execs into .venv, serves API and UI on one port, opens a browser
python3 run.py                    # --no-browser to suppress; RESUMAP_PORT=8077 to move it

# dev with reload (the `app` package is only importable from backend/)
cd backend && ../.venv/bin/uvicorn app.main:app --reload
```

When running anything directly rather than through `run.py` (which re-execs itself), invoke `.venv/bin/python`, not `python3`: here `.venv/bin/python` symlinks to a conda interpreter, so a bare `python3` silently picks up the wrong site-packages.

There is no test suite, no linter config and no build step. Verify changes by running the app, or drive the API directly against throwaway storage so the real DB is untouched — set the env vars *before* importing `app.config`, which reads them at import time:

```python
os.environ["RESUMAP_DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, "<repo>/backend")
from fastapi.testclient import TestClient   # then TestClient(app.main.app)
```

Diagnostics: `GET /api/debug/parse/{resume_id}` is the block-classification table — every block with its kind, section, flags and the rule that excluded it from being a point (also written to `data/logs/parse/<id>.txt` on upload). `GET /api/debug/usage` gives tokens and cost by operation.

## Architecture

**One process, two directories.** `backend/app` is a FastAPI app that mounts `frontend/` as static files at `/`. The only paths crossing the line are `FRONTEND_DIR` in `backend/app/config.py` and the `app.mount(...)` at the bottom of `backend/app/main.py`. `run.py` stays at the repo root and puts `backend/` on `sys.path`; `.env` and `data/` are addressed from the root, not from `backend/`.

**Layering is strict.** `main.py` owns HTTP and orchestration, `claude.py` owns every prompt and every Pydantic response schema, `backends/` owns transport only. A new model call means a schema plus a prompt in `claude.py` — never a prompt inside a backend module. A backend is any module exposing `NAME`, `LABEL`, `DEFAULT_MODEL`, `CONCURRENCY`, `unavailable()` and `complete()` (the `Backend` Protocol in `backends/__init__.py`). There are four: `claude_code` (the local Claude Code install via the Agent SDK), `codex` (`codex exec --output-schema`, strict-mode schema from `codex._strict`), `pi` (`pi -p --mode json --no-tools`; no schema support, so the schema goes in the system prompt and the reply is validated, with one retry) and `api` (Anthropic key, billed per token). The live one is chosen in the Settings panel: it is stored in `settings` (`backend`, `model.<name>`) and applied with `backends.select()` at startup and on save. `RESUMAP_BACKEND` is only the default before anything is saved. So `claude.py` must read `backends.current_model()` and `current_name()` at call time, never import-time constants. An empty `DEFAULT_MODEL` means "the CLI's own configured model". CLI subprocesses get `backends.cli_env()`, which drops API keys that came from `.env` so a CLI cannot silently bill them. Pi exits 0 on a failed provider call, so its errors are read off the final message's `stopReason`.

**Failure is data, not an exception.** `complete()` returns a `Completion` whose `parsed` is `None` when nothing validated, so the caller can log the attempt before degrading. The degradation ladder is deliberate: with no working backend, upload, parse and map still work because JD points come from `parsing/jd.py` rather than from a model, and the model call they lose only supplied their labels; transform and outreach hard-require a backend and return 503. Within a batch, `rewrite_many` gathers per-bullet results and marks failures `ok: false` so one bad bullet never blocks its siblings.

**Block ids are the join key.** `parsing/resume.py` turns a document into blocks with ids like `bul001` / `sec001`, stored whole as a JSON blob in `resumes.doc_json`. `mappings`, `rewrites` and the renderer's `overrides` dict are all keyed by those ids, so re-parsing a resume invalidates everything hanging off it. Only blocks with `mappable: true` are offered for mapping; the rule that rejected each of the others is recorded and shows up in the parse report.

**Experiences are mapped under keys of their own.** The `experiences` table is a global library of things that are not on the resume (step 1). Each one maps and rewrites under `db.experience_key(id)` (`exp012`), which shares the `mappings` and `rewrites` tables with block ids because no block kind starts with "exp". Transform sends a mapped experience to `claude.write_experience_bullet`, which returns a new bullet plus a `placement`: an entry id, a section id, or `new:<heading>`. The prompt allows an entry only if the notes say the work happened in that role; a side project under an employer reads as the employer's work. Deleting an experience deletes its mappings and rewrites in every job.

**A resume box can be replaced instead of realigned.** `box_modes` (per job, keyed by block id like `mappings`) marks a box `replace` from the Map step's toggle. Transform then sends it to `claude.replace_bullet`, which writes a new bullet for the mapped points even when the original is about unrelated work. It skips the job's mode rules, because every mode keeps the original's accomplishment. Its context is the original's length budget and style, its entry (`_entry_context` in `main.py`) and that entry's other bullets. The result is an ordinary rewrite with `meta.replaced`, so review, export and chat need no special case, and every claim it adds is expected in `substitutions` with `grounded: false`.

**JD points are verbatim.** `parsing/jd.py` splits a pasted JD one line per point and never rewords, merges, reorders, sentence-splits or de-duplicates them: the point count matches the posting, so the user can check the list against the tab they copied from. The only edits are rejoining hard-wrapped lines, dropping a leading bullet glyph, collapsing runs of spaces and discarding recognised headings and boilerplate (EEO, benefits, "about us", salary, location). `claude.parse_jd` then asks the model for `category` and `keywords` *on the points it was given* -- `JDTags` has no text field, so rewriting is not expressible, and `_apply_tags` drops any keyword that is not a literal substring of its point.

**Export applies overrides, it does not rewrite the document.** `_build_pdf` collects only rewrites whose status is in `INCLUDED_STATUSES` (`accepted`, `edited`) into `{block_id: text}`, then hands the *original* parsed doc plus that dict to `render_resume`. Every other block keeps its source text byte for byte, so a partial review still exports a valid resume. Accepted bullets written from experiences are spliced into a *copy* of the doc by `pdfgen.insert_blocks` (via `_render_doc` in `main.py`) on every render. They borrow a neighbouring bullet's geometry and carry their spacing in `_extra`; the renderer measures every other gap from the last *source* block, so an insertion never shifts the spacing around it. `pdfgen.py` reflows top-to-bottom rather than replaying absolute positions — rewritten text has a different length — and walks `FIT_LADDER`, tightening leading and block spacing first and scaling the font only if that fails, until it is one page. `report.verify_pdf` self-checks every export: page count, rows past the right margin, whether the text layer has spaces, dingbat substitution, similarity to the source. There is no LaTeX or HTML stage: ReportLab draws straight onto a canvas in base-14 Times/Helvetica/Courier. The yardstick for renderer changes is that an *untouched* upload re-renders with every block on the same line count as the source and fits one page with at most a trace of compression (`ResumeRenderer.stats` reports lines vs `source_lines` per block).

**The corrections chat edits the same two things export reads.** On step 5, `POST /api/jobs/{id}/chat` sends every block (rendered wording, original, line counts, chars per line) to `claude.revise_pdf`, which can only return block edits and page-wide layout numbers. Edits are saved as `edited` rewrites, so they show in the review step with their substitutions; layout goes to the `layouts` table and into `render_resume(layout=...)`, clamped by `pdfgen.clean_layout`. Each assistant turn stores the state it replaced, and only the latest applied turn can be undone, so an undo never reverts a later change to the same block.

**Outreach quotes the exported resume, not the stored one.** `_highlights` in `main.py` prefers accepted/edited rewrites, then mapped-but-unaccepted bullets, then untouched points, so the LinkedIn note and follow-up email can only claim what the PDF actually says. Liked messages in `outreach_examples` (global, like experiences; the newest `claude.OUTREACH_EXAMPLE_LIMIT` per kind go into each draft) are style references only: the prompt forbids taking a name, metric or tool from them. `db.ADDED_COLUMNS` is how a column reaches a table that already exists, since `CREATE TABLE IF NOT EXISTS` never alters one.

**Theme is client-only.** `localStorage["resumap.theme"]` holds light, dark or system. An inline script in `index.html` sets `data-theme` on `<html>` before first paint, and `:root[data-theme="dark"]` overrides the CSS variables. Colours in `styles.css` go through variables (`--surface`, `--surface-2`, `--line-strong`…); the top bar and toasts are dark in both themes and keep fixed colours.

**Leaving is saving.** The settings dialog saves a changed backend or model when it is dismissed (backdrop, Esc, Close), after `POST /api/backends` has made one real test call; a failure keeps it open, and a second dismiss discards. Text fields save on `change`. `localStorage["resumap.session"]` keeps the resume, job and step across a browser refresh, and the top bar's Reload re-fetches the current step.

**The frontend is one file with no framework.** `frontend/app.js` keeps all state in the `S` object, toggles `.panel[data-panel=N]` per step, and re-renders through per-step `renderX()` functions. `api()` throws on a non-2xx so callers `try/catch` into `setMsg`/`toast`. Watch `busy(btn, on)`: it stashes and restores `innerHTML`, so a custom button label must be re-applied *after* `busy(btn, false)`.

## Conventions

**Logging is two stores with different privacy profiles** (`obs.py`). `data/logs/resumap.jsonl` is metadata only — never log resume or JD text there. Describe text with `obs.tmeta(text, prefix)` (length, word count, short hash) and paths with `obs.path_str()` (masks the account name). Verbatim prompts go only to `data/logs/prompts/` via `obs.archive_prompt`, which is size-capped and can be switched off. Every model call goes through `obs.record_usage(op, ...)` so `/api/debug/usage` stays complete; add an entry to `PRICES` when introducing a Claude model (non-Claude models are logged at cost 0).

**The transform instructions are user-editable.** The Map step's Prompt… dialog edits `claude.EDITABLE_PROMPTS` (the rewrite, experience and replace instructions and the three mode rules). Edits are stored as `prompt.<key>` settings and applied with `claude.set_prompt_overrides()`, and prompts are always read through `claude.prompt_text()`, never the constants. `rewrite_request` / `experience_request` / `replace_request` build the exact `(system, user)` pair for both the call and `GET /api/jobs/{id}/prompt`, so the preview cannot drift from what is sent. The defaults below are what the app is tuned for, but the response schema is the only guarantee: code must still validate lengths and substitutions itself rather than trust that the prompt said so.

**Prompt rules the rest of the app depends on.** A rewrite must stay within ±15% of the original's character count or the fixed layout reflows (a breach logs `rewrite.length_breach`), and within `pdfgen.line_budget`, the characters the original's lines actually hold (`rewrite.line_budget_breach`). The LinkedIn note has a hard 300-character cap (`claude.LINKEDIN_LIMIT`) and is never auto-truncated — going over is surfaced for the user to trim, because cutting it here severs it mid-sentence exactly as LinkedIn would. No em dashes in generated text. Anything a rewrite introduces that was not in the original must land in `substitutions` with `grounded: false`; that is what the review step renders as "Introduced, not in your original", and it is the app's whole honesty story.

**This file does not reach Resumap's own model calls.** The `claude_code` backend runs with `setting_sources=[]`, `strict_mcp_config=True` and `tools=[]`, so CLAUDE.md, hooks, skills, MCP servers and claude.ai connectors are deliberately not loaded — guidance here applies to Claude Code working *on* this repo, not to the prompts the app sends at runtime.

**Configuration lives in `.env`** (see `.env.example`), read by `config.py`. The exception is `obs.py` and `report.py`, which read their own logging switches directly: `RESUMAP_LOG_TEXT`, `RESUMAP_LOG_PROMPTS`, `RESUMAP_PROMPT_ARCHIVE_MB`. `RESUMAP_DATA_DIR` and `RESUMAP_LOG_DIR` point an instance at storage of its own.
