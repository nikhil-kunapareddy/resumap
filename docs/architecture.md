# Resumap architecture

Resumap is a local, single-user web app. One Python process serves a small
browser UI and a JSON API on `127.0.0.1`, keeps everything in one SQLite file,
and reaches a language model through whichever backend the user picked. This
document is the design: the high-level shape first, then the classes, the data
and the two request paths that matter most, then the rules the code keeps.

![The request flow, step by step](system-design.png)

## 1. Context

```mermaid
flowchart LR
    user([User]) -- browser --> ui["frontend/<br/>vanilla JS UI"]
    ui -- JSON over HTTP --> app["Resumap process<br/>FastAPI on 127.0.0.1"]
    app --> db[("SQLite<br/>data/resumap.db")]
    app --> files[/"data/uploads, data/exports,<br/>data/logs"/]
    app -- one of --> cc["Claude Code CLI<br/>(Agent SDK)"]
    app -- one of --> codex["Codex CLI"]
    app -- one of --> pi["pi CLI"]
    app -- one of --> api["Anthropic API"]
```

Nothing is multi-user: there are no accounts and no authentication, and the
server binds to loopback. The model is optional for the first three steps
(upload, job description, map) and required for the rest.

## 2. Layers

```mermaid
flowchart TB
    main["main.py<br/>composition root"]
    api["api/<br/>routers, request bodies, error mapping"]
    services["services/<br/>use cases, response models"]
    llm["llm/<br/>gateway, prompts, backends"]
    storage["storage/<br/>Database, repositories"]
    rendering["rendering/<br/>PDF renderer, self-check"]
    parsing["parsing/<br/>resume + JD parsers"]
    domain["domain/<br/>enums, models, keys"]

    main --> api --> services
    services --> llm & storage & rendering & parsing
    llm --> parsing
    rendering --> parsing
    llm & storage & services --> domain
```

Each package may import only the packages below it. `tests/test_architecture.py`
reads every module's imports and fails the build on a crossing, and on any
`fastapi` or `starlette` import outside `api/`.

| Package | Owns | Knows nothing about |
|---|---|---|
| `domain/` | the vocabulary: `str` enums, entity and document models, the experience-key rule | everything else |
| `parsing/` | PDF/DOCX/TXT to classified blocks; JD to verbatim points; the parse report | storage, models, HTTP |
| `rendering/` | the parsed document plus overrides to a one-page PDF; the export self-check | storage, models, HTTP |
| `storage/` | the schema, connections, transactions, one repository per table group | HTTP, models |
| `llm/` | prompts, response schemas, the `ModelGateway`, the backends | storage, HTTP |
| `services/` | every use case and what it returns; refusals as `ServiceError`s | HTTP |
| `api/` | turning HTTP into service calls and service errors into status codes | SQL, prompts |
| `main.py` | reading configuration and wiring one of everything together | business rules |

## 3. Classes

### Services and storage

```mermaid
classDiagram
    direction TB
    class Service {
        <<base>>
        store: Store
        _job(id) Job
        _resume(id) Resume
        _rewrite(job_id, block_id) Rewrite
    }
    Service <|-- SettingsService
    Service <|-- ResumeService
    Service <|-- ExperienceService
    Service <|-- JobService
    Service <|-- RewriteService
    Service <|-- ExportService
    Service <|-- CorrectionService
    Service <|-- OutreachService
    CorrectionService --> ExportService : renders the preview with
    ExportService --> SettingsService : reads the filename prefix from

    class Store {
        resumes: ResumeRepository
        jobs: JobRepository
        mappings: MappingRepository
        rewrites: RewriteRepository
        chat, layouts, outreach, ...
        transaction()
    }
    class Database {
        path: Path
        init()
        connect()
        transaction()
    }
    Service --> Store
    Store *-- Database
```

### The model layer

```mermaid
classDiagram
    direction LR
    class ModelGateway {
        registry: BackendRegistry
        prompts: PromptStore
        status() str
        parse_jd(text) dict
        rewrite_many(calls) list
        draft_outreach(...) dict
        revise_pdf(...) dict
        request(kind, args) tuple
    }
    class BackendRegistry {
        get(name) Backend
        select(name, models)
        current_model() str
        describe() list
    }
    class PromptStore {
        text(key) str
        set_overrides(edits)
        mode_rules(mode) str
    }
    class Backend {
        <<abstract>>
        name
        label
        concurrency
        default_model
        unavailable()* str
        complete(...)* Completion
    }
    ModelGateway *-- BackendRegistry
    ModelGateway *-- PromptStore
    BackendRegistry o-- "4" Backend
    Backend <|-- ClaudeCodeBackend
    Backend <|-- CodexBackend
    Backend <|-- PiBackend
    Backend <|-- AnthropicApiBackend
```

`JobService`, `RewriteService`, `CorrectionService` and `OutreachService` are
the services that hold the `ModelGateway`. `Services` (in `services/__init__.py`)
is a frozen dataclass holding one of each service, built by `Services.build`.

Every collaborator arrives through a constructor. `create_app(paths, registry)`
builds one `Store`, one `ModelGateway` and one of each service; a test passes
its own `Paths` and a registry of fakes, and nothing is monkeypatched.

The domain models split in two:

* **Entities** (`Resume`, `Job`, `Rewrite`, `Experience`, `OutreachDraft`,
  `OutreachExample`, `ChatMessage`, `Layout`) mirror table rows and are strict:
  an unexpected field means the table and the model have drifted.
* **Documents** (`ResumeDoc`, `StoredBlock`, `Run`, the `*Meta` blobs) are
  stored as JSON and are tolerant readers: unknown keys pass through, so a
  blob written by another version round-trips byte for byte.

Behaviour lives with the data it is about: `RewriteStatus.included`,
`Rewrite.in_export`, `Job.summary()`, `ResumeDoc.as_dict()`.

## 4. Data

```mermaid
erDiagram
    resumes ||--o{ jobs : "tailored for"
    jobs ||--o{ mappings : "box answers point"
    jobs ||--o{ box_modes : "box replaced"
    jobs ||--o{ rewrites : "box rewritten"
    jobs ||--o| outreach : "drafted"
    jobs ||--o| layouts : "adjusted"
    jobs ||--o{ chat : "corrected"

    resumes {
        int id PK
        text label
        text stored_path
        json doc_json "ResumeDoc: blocks keyed bul001, sec003"
    }
    jobs {
        int id PK
        int resume_id FK
        text raw_text
        json points_json "JDPoint list, verbatim"
        text mode
    }
    mappings {
        int job_id FK
        text block_id "block id or exp012"
        text point_id "jd01"
    }
    rewrites {
        int job_id FK
        text block_id
        text original
        text rewritten
        text status "pending, accepted, rejected or edited"
        json meta_json
    }
    experiences {
        int id PK
        text kind
        text text
    }
    outreach_examples {
        int id PK
        text kind
        text body
    }
    settings {
        text key PK
        text value
    }
```

`experiences`, `outreach_examples` and `settings` are global, not per job.
Deleting a resume cascades to its jobs, and a job to everything filed under
it. An experience is mapped and rewritten under `experience_key(id)`
(`exp012`), which cannot collide with a parsed block id because no block kind
starts with "exp".

## 5. Two request paths

### Transform: mapped boxes become rewrites

```mermaid
sequenceDiagram
    participant UI as Browser
    participant R as api/routers/rewrites
    participant S as RewriteService
    participant St as Store
    participant G as ModelGateway
    participant B as live Backend

    UI->>R: POST /api/jobs/{id}/transform
    R->>S: transform(job_id, block_ids, extra)
    S->>St: job, resume, mappings, box modes, layout
    S->>S: one call per mapped box, with its line budget
    S->>G: rewrite_many(calls)
    par one per box, up to backend.concurrency
        G->>B: complete(system, user, schema)
        B-->>G: Completion (parsed or None)
        G->>G: archive prompt, record usage, check length and glyphs
    end
    G-->>S: results (a failed box is ok: false)
    S->>St: save every good rewrite as pending, in one transaction
    S-->>R: TransformOut
    R-->>UI: 200 JSON
```

### Export: the original document plus accepted wording

```mermaid
sequenceDiagram
    participant UI as Browser
    participant S as ExportService
    participant St as Store
    participant D as services/documents
    participant P as rendering/pdfgen
    participant C as rendering/selfcheck

    UI->>S: POST /api/jobs/{id}/export
    S->>St: job, resume, rewrites, layout
    S->>D: render_doc(doc, rewrites)
    Note right of D: splices accepted experience bullets<br/>into a copy of the document
    S->>D: overrides(rewrites)
    Note right of D: block id to text, accepted and edited only
    S->>P: render_resume(doc, out, overrides, layout)
    P->>P: reflow, walk FIT_LADDER until one page
    S->>C: verify_pdf(out, doc, source)
    S-->>UI: ExportOut (file name, fit, self-check)
```

## 6. Rules the code keeps

These are the invariants the rest of the app depends on. Each one is tested.

1. **JD points are verbatim.** `parsing/jd.py` splits one line per point and
   never rewords, merges, reorders or de-duplicates. The model only labels
   points; `JDTags` has no text field, and a keyword that is not a substring of
   its point is dropped.
2. **Block ids are the join key.** Mappings, rewrites and export overrides are
   all keyed by the parsed block id, so re-parsing a resume invalidates them.
3. **Export applies overrides; it never edits the stored document.** Only
   `accepted` and `edited` rewrites reach the PDF. Every other block keeps its
   source text byte for byte, so a half-finished review still exports.
4. **An untouched upload re-renders line for line.** Every block on the same
   number of lines as the source, one page, at most a trace of compression.
5. **Anything a rewrite introduces is declared.** A claim not in the original
   must appear in `substitutions` with `grounded: false`, which the review step
   shows as "Introduced, not in your original".
6. **Prompts do not change by accident.** `tests/golden/prompts/` pins every
   (system, user) pair byte for byte, and the Map step's preview is built by the
   same function as the call, so it cannot drift from what is sent.
7. **Failure is data.** A backend returns `parsed=None` rather than raising;
   one failed bullet is reported and never blocks its siblings; with no backend,
   upload, JD parsing and mapping still work.
8. **Resume and JD text never reach the metadata log.** `data/logs/resumap.jsonl`
   gets lengths and hashes (`obs.tmeta`); verbatim prompts go only to the
   size-capped prompt archive, which can be switched off.

## 7. Errors

Services raise one of five refusals, and `api/errors.py` alone decides the
status. Every error body is `{"detail": "<one readable sentence>"}`, which the
UI shows as the message.

| Raised by a service | Status | Meaning |
|---|---|---|
| `NotFound` | 404 | the job, resume, rewrite or example does not exist |
| `Invalid` | 400 | well-formed, but cannot be done as asked |
| `Conflict` | 409 | would undo a later change (undoing an older correction) |
| `Unavailable` | 503 | needs a model and the live backend cannot run |
| `UpstreamFailed` | 502 | the model answered with nothing usable |
| request validation | 422 | a body failed its schema, e.g. an unknown status |

## 8. Consistency

Each repository call opens a connection, commits and closes it, unless it runs
inside `store.transaction()`. There, every call joins one shared connection
(held in a `ContextVar`, so per thread and per task), and the block commits
together or not at all; a nested transaction joins the outer one. Every
multi-step write uses one: saving a transform, dropping points and unwiring
them, accept-all, applying or undoing a correction, deleting an experience.

## 9. Testing

The suite is characterization-first: it pins what the app does, so a change
has to show what it changed.

| Kind | Where | What it guards |
|---|---|---|
| snapshots | `tests/golden/` | parser output per format, every prompt byte for byte |
| workflow | `test_api_workflow.py` | every route, driven the way the UI drives it |
| degradation | `test_degradation.py` | no backend, bad replies, a write failing halfway |
| contract | `test_contract.py` | routes declare responses; stored rows fit the models |
| architecture | `test_architecture.py` | the layering in section 2 |
| units | `test_llm.py`, `test_render.py`, `test_storage.py`, `test_jd_split.py` | each layer on its own |

Every model call in the suite goes to `tests/fakes.FakeBackend`, and every test
gets its own storage from `create_app(paths, registry)`. The sample resume is
fictional and generated at test time. Known defects are strict `xfail`s, so
fixing one fails the run until its marker is removed.

## 10. Extending

* **A new route**: a method on the right service returning a model from
  `services/results.py`, then a thin handler in `api/routers/`. Refuse with a
  `ServiceError`, never an HTTP exception.
* **A new model call**: a response schema in `llm/schemas.py`, a pure builder
  for its message, a `ModelGateway` method that validates what comes back, and
  a snapshot in `tests/test_prompts_golden.py`.
* **A new backend**: a `Backend` subclass in `llm/backends/` that takes its
  configuration in its constructor, registered in `default_registry()`.
* **A new column**: add it to the table in `storage/schema.py` and to
  `ADDED_COLUMNS`, since `CREATE TABLE IF NOT EXISTS` never alters an existing
  table; then the entity model, which is strict and will fail until it matches.
