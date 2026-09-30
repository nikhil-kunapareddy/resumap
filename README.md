# Resumap

A local, browser-based resume mapper. Upload your reference resume once, paste a
job description, map each resume bullet onto the JD points it should answer, and
let Claude realign the wording, tools and tone. Export a PDF that keeps your
original layout, named `sai_companyname_mmdd.pdf`, then draft the LinkedIn note
and follow-up email that go with it.

Everything runs on your machine. Your resume never leaves it except as the text
of individual bullets sent to Claude during a rewrite, and the tailored bullets
sent again when you ask for the outreach messages.

## Setup

```bash
uv sync                      # or: python3 -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env         # then pick a backend, below
python3 run.py
```

`run.py` re-execs into `.venv` automatically, so you never need to activate it.
The browser opens at http://127.0.0.1:8000. `--no-browser` suppresses that.

## Choosing a backend

`RESUMAP_BACKEND` in `.env` decides who serves the model calls. Both produce
the same validated structures, so you can switch at any time by restarting.

| | `api` (default) | `claude_code` |
|---|---|---|
| Credentials | `ANTHROPIC_API_KEY` in `.env` | whatever your local Claude Code install is signed in with |
| Cost | billed per token | spends your Claude subscription's rate limits, shared with interactive Claude Code use |
| Needs | a key from https://console.anthropic.com/settings/keys | Claude Code installed and signed in (`claude` on your PATH, or set `RESUMAP_CLAUDE_CLI`) |

The `claude_code` backend runs the local `claude` binary through the Claude
Agent SDK with its tools, settings and CLAUDE.md loading switched off, so each
bullet is a single structured request rather than a coding agent. An
`ANTHROPIC_API_KEY` left in `.env` is blanked out for that subprocess, so
choosing this backend cannot silently bill you per token.

Fair warning: this is fine for a tool you run on `127.0.0.1` for yourself, but
Anthropic does not permit third-party products to offer claude.ai login or
subscription rate limits to *other* users. Ship Resumap to anyone else and it
needs the `api` backend.

With no working backend you can still upload, parse and map — the JD splitter
falls back to a built-in heuristic. Only the rewrite step hard-requires one.

## The flow

1. **Resume** — drop in a PDF, DOCX, TXT or MD. It is parsed into blocks and
   stored in `data/resumap.db`. Only **accomplishments** become mappable points
   (see below). Upload once, reuse for every application.
2. **Job description** — paste the whole posting. Claude splits it into atomic
   requirements, pulls out the ATS keywords, and drops boilerplate. Company and
   role are auto-detected and editable; the company name drives the filename.
3. **Map** — click a resume box, then tick every JD point it should answer.
   Mapping is many-to-many: one bullet can target several requirements, and
   several bullets can target the same one. *Show skipped lines* reveals
   everything the parser passed over, so you can map one anyway.
4. **Review** — original and rewrite side by side. Accept, reject, hand-edit, or
   re-roll any bullet (optionally with a note like *"lead with the cost saving"*).
   Anything introduced that was not in your original is listed explicitly.
5. **Export** — preview the rendered PDF inline, then download
   `resume_companyname_mmdd.pdf`. Set your own prefix, such as your first name,
   in the filename field.
6. **Outreach** — draft the two messages that go out with the application: a
   LinkedIn connection note and a follow-up email reiterating interest (see
   below).

Only **accepted** and **edited** bullets are applied. Pending, rejected and
unmapped bullets keep their original wording, so a partial review still exports
a valid resume.

## Recruiter outreach

Step 6 writes two messages from the resume you just exported, not from the
original one: the same tailored bullets, the same JD vocabulary.

- **LinkedIn note** — one paragraph sized for a connection request. LinkedIn
  truncates those at 300 characters, so the prompt targets 240-290 and the box
  shows a live count that turns red if a draft comes back over. Nothing is
  auto-truncated; an over-long note is flagged for you to trim, because cutting
  it here would sever it mid-sentence exactly as LinkedIn would.
- **Follow-up email** — a subject line plus a three-paragraph body: the
  application is in, the single strongest piece of evidence, and a low-pressure
  ask about next steps. *Open in mail app* hands it to your mail client with
  the subject and body prefilled; the recipient is left blank.

A recruiter name is optional — leave it blank and both messages open neutrally.
The free-text instruction box is passed through to the prompt, for things the
resume cannot know (*"mention I met them at the career fair"*).

Both drafts are grounded in the resume points that will actually be in the PDF:
accepted and edited rewrites first, then anything mapped but not yet accepted,
falling back to your strongest untouched points so a draft is possible before
the review step is done. Which ones were used is listed under the email. Claude
is told not to invent a metric, tool, employer or prior conversation, but the
claims are still yours to check before sending.

Edits save automatically per job, so a note you reworded is still there when you
come back to that application.

## What counts as a point

Only things you **did** become mappable boxes. Your name, contact line, section
headings, **job titles, employers, locations, date ranges**, skills inventories,
degrees, coursework, certifications and interests are parsed and preserved in
the exported PDF, but they are never offered for mapping — there is nothing to
retarget in "Senior Data Engineer, Northwind Analytics Inc.".

A block qualifies when all of these hold:

- it is a bullet, or a paragraph of 60+ characters that opens with an action verb
- it sits in a section describing work — Experience, Employment, Projects,
  Research, Internships, Leadership, Volunteer, Teaching
- it is not a comma-separated inventory (`Languages: Python, SQL, R`)
- it does not look like a header line

Section names are matched in three tiers, so *Academic Projects* counts while
*Academic Background* does not, and *Project Management Skills* does not despite
containing "project".

Header detection is what catches titles and employers, because plenty of resumes
put the title, the company and the dates on separate lines, or leave the title
unbolded — so no single line is ever a bold line with a date on it. A short line
is treated as a header when it contains a **date range**, is 70%+ title-cased,
is pipe/dash separated, or carries a company suffix (Inc, LLC, Labs, Group,
Technologies, Pvt Ltd, University, …).

Precedence matters, and two rules earn their place:

- A **date range outranks everything.** Job titles double as verbs, so
  `Engineer, Contoso Group March 2019 - June 2021` opens with what looks like
  an action verb. A range (`Sept 2020 - Dec 2022`, `2020 to 2022`,
  `Jun 2023 - Present`) settles it. A bare year does not, so
  `Led the 2021 migration of twelve services` stays a point.
- Otherwise an **action-verb opening wins**, so `Managed AWS, GCP And Azure
  Migration Projects` survives despite being title-cased.

Two traps this had to be taught: *engineer, architect, lead, design, partner*
and *support* are job titles at least as often as verbs, so they are not action
verbs (their past-tense forms still register); and words like *United,
Advanced, Applied, Limited, Integrated* end in "-ed" without being verbs, so
`United Technologies Advanced Systems Division` does not read as an
accomplishment.

Two safety valves: if section detection finds nothing (an unusual layout, a
resume with no headings), it falls back to shape alone rather than showing you
an empty screen; and **Show skipped lines** on the Map step lets you map
anything it passed over. If a real accomplishment is filed under Education, that
toggle is how you pull it in.

## Complete sentences

Every point — resume or JD — is reassembled into a whole thought and ends in a
full stop. Two different things break this, so both parsers share
`backend/resumap/parsing/text.py`:

- **A PDF bullet wrapping inside its column.** Deciding whether the next line is
  a continuation is geometric: the line above had *no room left for this line's
  first word*. Asking instead whether it reached the right margin (the obvious
  test) truncates every bullet whose wrap point left a wide ragged gap.
- **A pasted JD arriving hard-wrapped** at a fixed width, which turns one
  requirement into three fragments. Lines are rejoined before anything is split
  into points.

Rejoining needs positive evidence, never just a missing full stop — plenty of
complete lines have none (resume bullets, job titles). A line joins the one
above it when it cannot open a sentence (lower-case or digit start, e.g. `2.4
billion events per day`) and the line above never closed one, or when the line
above stops on a connector, comma, or bare participle (`...now handling`).

Repairs peel only words that **cannot** end an English sentence. Prepositions
that can are left alone, so `models the ML engineers depend on` keeps its `on`
instead of becoming `...depend`.

A missing full stop is restored only when most of a resume's own points already
have one, so the exporter never quietly restyles a resume that omits them. Any
point that still stops on a connector is flagged **may be cut off** in the UI
rather than shipped as half a sentence.

## Rewrite modes

| Mode | What Claude may change |
|---|---|
| **Aggressive keyword alignment** (default) | Maximises ATS overlap. May introduce tools and terminology from the JD that your original bullet did not name. |
| **Realign + bridge synonyms** | Re-expresses tools you *did* use in the JD's vocabulary (`cron` → `DAG-style orchestration`), phrased to stay literally true. |
| **Truthful realign** | Wording, emphasis and tone only. Nothing enters a bullet that was not already in it. |

In every mode, **numbers are facts**: metrics, percentages, durations, dollar
figures, dates, team sizes and company names are carried over untouched and are
never invented or inflated.

Aggressive mode is the default you chose, and it will put tools in your resume
that you did not write there. Each one is flagged in the review screen under
*"Introduced, not in your original"*, and a bullet Claude judges you could not
defend in an interview is tagged **not fully defensible**. Read those flags
before you export — you are the one who has to answer for the claims.

## Layout mirroring

The exporter does not use a fixed template. It reads your original PDF's
geometry and typography and replays them while reflowing the text:

- page size and all four margins, **measured** (assuming a conventional bottom
  margin threw away two usable lines and forced a second page)
- font family, size and weight, including **weight changes inside a line** —
  `**Senior Engineer**, Contoso Group`, `**Languages:** Python, SQL`
- the document's own **line spacing**, measured as the median baseline step
- indents, hanging bullet indents, and section rules
- **right-aligned tab stops**, detected geometrically, so they survive whether
  the source positions the date or pads it with literal spaces
- **justification**, reproduced with the PDF word-spacing operator

Rewrites are length-constrained to ±15% of the original so the page does not
shift.

**A resume is always one page.** If the faithful layout overflows, line spacing
and inter-block spacing are tightened first — whitespace is far less noticeable
than shrunken type — and only then is the font scaled. If the ladder still
overflows, the renderer measures the height the content actually wants and
solves for the type size that fits. Below 62% it stops: at that point the honest
answer is that there is too much content, and the export says so rather than
shipping something unreadable.

Limits: it is an approximation, not a pixel copy. Multi-column layouts, text
boxes, tables, icons and logos are flattened to a single column in the nearest
base font (Times / Helvetica / Courier). Scanned PDFs have no text layer and are
rejected — run OCR first. Always eyeball the step-5 preview.

### ATS-readable text layer

Justifying by placing each word at a computed x looks correct but produces a PDF
containing no space glyphs at all: extraction — and therefore every ATS — reads
`JaneMarieDoe`. Lines are drawn as real strings instead, justified with
the `Tw` operator. Two traps live here, both caught by checking the extracted
text rather than the picture:

- `Tw` is *text state* in PDF and survives `BT`/`ET`, so it must be written
  explicitly on every line. Leaving it unset let one justified line stretch
  every line after it and push 22 rows past the right margin.
- A round bullet is absent from the base-14 fonts, so ReportLab substitutes
  ZapfDingbats, which extracts as a stray `l` at the head of every bullet. The
  bullet is drawn in Vera (bundled with ReportLab) instead, and extracts as `•`.

## Project layout

```
run.py                    launcher (re-execs into .venv, opens the browser)
pyproject.toml            dependencies and tool config (locked in uv.lock)
backend/
  resumap/
    main.py               create_app(): wires everything below into one FastAPI app
    config.py             env config; Paths (where an instance keeps its files)
    obs.py                structured logging, cost accounting, prompt archive
    api/                  HTTP only
      routers/            one router per area: resumes, jobs, rewrites, export, ...
      schemas.py          request bodies
      errors.py           service errors -> status codes; readable 422s
    services/             every use case, no HTTP: jobs, rewrites, exports, ...
      results.py          what each service returns (the API's response models)
    llm/                  model calls
      gateway.py          ModelGateway: every call, its bookkeeping and checks
      prompts.py          user-editable instructions and the PromptStore
      schemas.py          what each call must answer with
      tagging.py bullets.py outreach.py corrections.py   the message each call sends
      backends/           claude_code, codex, pi, api; BackendRegistry
    storage/              SQLite schema, Database, one repository per table group
    rendering/            pdfgen.py (layout-mirroring renderer), selfcheck.py
    parsing/
      layout.py           PDF/DOCX/TXT -> style-aware lines
      resume.py           lines -> sections, entries, bullets
      jd.py               verbatim JD splitter: one line, one point
      report.py           the parse report
    domain/               enums and models for what is stored; imports nothing else
frontend/                 browser UI (vanilla JS, no build step)
  index.html  app.js  styles.css
tests/                    pytest suite: fake backend, generated sample resume
docs/                     architecture.md (the design), system-design.png
data/                     SQLite db, uploads, exports  (gitignored)
.env                      backend choice, model, overrides
```

Dependencies point one way: `api` → `services` → `llm`, `storage`,
`rendering`, `parsing` → `domain`. `tests/test_architecture.py` fails on any
import that crosses a layer.

The two halves are separate directories but not separate processes: FastAPI
mounts `frontend/` as static files at `/`, so there is still one server, one
port and no build step. `run.py` stays at the root and puts `backend/` on
`sys.path`; to drive uvicorn yourself, run it from inside `backend/`:

```bash
cd backend && ../.venv/bin/uvicorn resumap.main:app --reload
```

The only paths that cross the line are `FRONTEND_DIR` in `backend/resumap/config.py`
and the `app.mount` at the end of `create_app` in `backend/resumap/main.py`. Everything else the
backend touches (`.env`, `data/`) is addressed from the project root, so a
future move of either half is a two-line change.

## Logging

Two stores, because the operational log and the model audit trail have
different privacy profiles.

**`data/logs/resumap.jsonl`** — every event, rotating, strictly metadata. Text is
reduced to a length, a word count and an 8-character hash; paths have the
account name masked. No resume content, no contact details, verified by an audit
over the log. Safe to paste into a bug report. A readable line also goes to the
console you launched from:

```
01:50:57 INFO  parse   done   pages=1 lines=56 blocks=31 points=12
                              tab_stops=7 merges=25 label_guard_blocks=5
01:51:07 INFO  api     call   op=jd_parse in=1761 out=965 cost_usd=0.03293
                              ms=9,682 request_id=req_011Cf6qEnKdCD9rC5yyENRFL
01:51:18 WARN  rewrite not_defensible introduced=1 prompt_file=…
01:51:18 INFO  render  done   pages=1 ladder=[0.95/0.55/1.0:2p, 0.93/0.45/1.0:1p]
```

**`data/logs/prompts/`** — one file per API call with the prompt and completion
verbatim. This unavoidably contains resume and JD text, which is exactly why it
is a separate directory: size-capped (`RESUMAP_PROMPT_ARCHIVE_MB`, oldest
pruned) and switchable with `RESUMAP_LOG_PROMPTS=0`. The JSONL references the
filename rather than inlining the content.

The one identifier that does reach the JSONL is the export filename, and with it
the company name — it is the artifact's name, and without it "which export was
this?" is unanswerable.

### What is logged, and why

Most of it exists because a specific bug was hard to find without it:

| Area | Notable fields |
|---|---|
| Parse | geometry and leading **as measured**, tab stops, `(cid:NNN)` glyph repairs, every wrapped-line merge **with its deciding reason** (`continuation` / `could-not-fit`), `label_guard_blocks`, and for each block **why it is not a point** (`section-excluded`, `list-shaped`, `header-shaped`, `para-no-action-verb`, …) |
| JD | hard-wrapped lines rejoined, claude vs heuristic path **and why**, points missing a full stop |
| API | which `backend` served the call, tokens in/out, cost, latency, `request_id` (a Claude Code session id on that backend), running session total, retries, failures |
| Rewrite | length delta against the ±15% rule (warns on breach), keywords, substitutions split grounded vs **introduced**, `defensible` (warns when false) |
| Render | every fit-ladder probe and its page count, analytic-fit fallback, chosen scales |
| Export | self-check: pages, rows past the right margin, **whether the text layer has spaces**, dingbat-font substitution, similarity to source |
| Review | mappings added/removed, accept / reject / edit status transitions |
| Outreach | highlights fed in, LinkedIn character count and whether it breached the 300 limit, later hand-edits |
| HTTP | method, path, status, duration, `X-Resumap-Run` correlation id; slow-request warnings |

### Diagnostics

- **`GET /api/debug/parse/{resume_id}`** — the block-classification table: every
  block with its kind, section, length, flags (right column / justified /
  truncated) and the rule that excluded it. Also written to
  `data/logs/parse/<id>.txt` on upload. This is the view that makes "why is my
  skills line a point?" a five-second question.
- **`GET /api/debug/usage`** — token and cost totals for the process, by operation.
- `RESUMAP_LOG_TEXT=1` adds truncated, contact-scrubbed text samples to the
  JSONL and the parse report when you are debugging locally.
- `RESUMAP_LOG_LEVEL=DEBUG` for per-block parser detail.

The export self-check is the automated form of the checks that caught the
shipped defects described above — a spaceless text layer, 22 rows past the right
margin, a dingbat substitution putting a stray `l` at the head of every bullet.
It runs on every export and warns rather than silently passing.

## Tests

```bash
uv sync --extra dev
uv run pytest                # tests
uv run ruff check            # lint
uv run mypy                  # types
```

CI runs all three on Python 3.10 to 3.13 (`.github/workflows/ci.yml`). The suite runs in a few seconds and never touches `data/`, your `.env` settings
or a real model: every model call goes to a scripted fake backend, and the
sample resume is a fictional one generated as PDF, DOCX and TXT. Parser output
is pinned in `tests/golden/`; after an intended change to how resumes are
classified, re-pin it with `RESUMAP_UPDATE_GOLDEN=1 uv run pytest
tests/test_resume_parse.py` and review the diff; prompts are pinned the same way
in `tests/golden/prompts/`. Known defects are marked as
strict expected failures, so fixing one fails the run until the marker goes.

## Notes

- Model is `claude-opus-5`; override with `RESUMAP_MODEL` in `.env`.
- Rewrites run concurrently and a failed bullet never blocks the rest of the
  batch: 4 at a time on the `api` backend, 2 on `claude_code`, where each call
  is a `claude` subprocess.
- Logged costs are local list-price estimates. On the `claude_code` backend
  nothing is billed per token, so treat that column as "what this would have
  cost through the API".
- If Claude's JD parse fails, the app falls back to a built-in splitter and
  tells you it did.
- Port and host are `RESUMAP_PORT` / `RESUMAP_HOST`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, checks and where changes go,
[docs/architecture.md](docs/architecture.md) for the design, and
[SECURITY.md](SECURITY.md) for how Resumap is meant to be run and how to report
a problem.

## License

MIT. See [LICENSE](LICENSE).
