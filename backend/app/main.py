"""Resumap: FastAPI backend + static browser UI."""
from __future__ import annotations

import logging
import re
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import backends, claude, db, obs, report
from .api.schemas import (BackendBody, BackendsOut, BackendTestOut, BoxModeBody, BoxModeSaved,
                          ChatBody, ChatMessageOut, ChatState, ErrorOut, ExperienceCreate,
                          ExperienceDeleted, ExportOut, FitOut, JobCreate, JobCreated,
                          JobDetail, JobPatch, JobSummary, MappingBody, MappingSaved, ModelBody,
                          Ok, OutreachBody, OutreachExampleCreate, OutreachOut, OutreachPatch,
                          PointsKept, PointsKeep, PromptBody, PromptInfo, PromptPreview,
                          PromptsOut, ResumeOut, ResumeSummary, RewritePatch, SettingsBody,
                          SettingsOut, StatusOut, TransformBody, TransformOut, UsageOut)
from .config import (BACKEND, DEFAULT_FIRST_NAME, EXPORT_DIR, FRONTEND_DIR,
                     LOG_DIR, LOG_LEVEL, UPLOAD_DIR, ensure_dirs)
from .domain import INCLUDED_STATUSES, Experience, OutreachExample, Rewrite
from .parsing.resume import parse_resume
from .pdfgen import (clean_layout, export_filename, insert_blocks, line_budget,
                     new_bullet_budget, render_resume)

ALLOWED_EXT = {".pdf", ".docx", ".txt", ".md"}

@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_dirs()
    obs.setup(LOG_DIR, LOG_LEVEL)
    db.init()
    _apply_backend_settings()
    claude.set_prompt_overrides(db.settings_with_prefix("prompt."))
    obs.event("app.start", model=backends.model_label(),
              backend=backends.current_name(),
              backend_ready=claude.backend_status() is None,
              data_dir=obs.path_str(UPLOAD_DIR.parent),
              log_dir=obs.path_str(LOG_DIR),
              log_level=LOG_LEVEL,
              prompts_archived=obs._prompts_enabled(),
              text_in_logs=obs._log_text_enabled(),
              resumes=len(db.list_resumes()))
    yield
    obs.event("app.stop", **obs.totals())


# Every error body is {"detail": "<one readable sentence>"}: the UI shows
# `detail` as the message. Declaring 422 here replaces FastAPI's default
# validation-error schema, which this app does not send.
app = FastAPI(title="Resumap", lifespan=lifespan,
              responses={422: {"model": ErrorOut, "description": "Invalid request"}})


@app.middleware("http")
async def _log_requests(request: Request, call_next):
    run = obs.new_run_id()
    if request.url.path.startswith(("/styles.css", "/app.js")) or request.url.path == "/":
        return await call_next(request)   # static assets are not worth an event
    with obs.timer() as t:
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            obs.error("http.error", exc=exc, method=request.method,
                      path=request.url.path)
            raise
    level = logging.WARNING if response.status_code >= 400 else logging.INFO
    obs.event("http.request", level=level, method=request.method,
              path=request.url.path, status=response.status_code, ms=t.ms)
    if t.ms > 15000:
        obs.warn("http.slow", path=request.url.path, ms=t.ms)
    response.headers["X-Resumap-Run"] = run
    return response


# --------------------------------------------------------------------------
# status & settings
# --------------------------------------------------------------------------

@app.get("/api/status")
def status() -> StatusOut:
    why = claude.backend_status()
    try:
        label = backends.get().LABEL
    except Exception:  # noqa: BLE001 - an unknown name; `detail` says so
        label = backends.current_name()
    return StatusOut(
        backend=backends.current_name(),
        label=label,
        ready=why is None,
        detail=why,
        model=backends.model_label(),
        first_name=db.get_setting("first_name", DEFAULT_FIRST_NAME),
    )


def _apply_backend_settings() -> None:
    """Put the settings panel's saved choice live (RESUMAP_BACKEND until then)."""
    name = db.get_setting("backend", BACKEND)
    if name not in backends.MODULES:
        name = BACKEND
    backends.select(name, {b: db.get_setting(f"model.{b}", "") for b in backends.MODULES})


@app.get("/api/backends")
def list_backends() -> BackendsOut:
    """Every backend and whether it can run, for the settings panel."""
    return BackendsOut(selected=backends.current_name(), backends=backends.describe())


@app.post("/api/backends/{name}/test")
async def test_backend(name: str, body: ModelBody | None = None) -> BackendTestOut:
    """Make one tiny real call through a backend, without switching to it.
    `body.model`, when given, is tried instead of the saved one."""
    if name not in backends.MODULES:
        raise HTTPException(404, f"Unknown backend '{name}'.")
    model = (body.model.strip() if body else "") or None
    out = await claude.ping(name, model)
    hint = backends.suggest_model(name, model or "")
    if not out["ok"] and hint:
        out["detail"] += f" Did you mean {hint}?"
    return BackendTestOut(**out)


@app.post("/api/backends")
async def choose_backend(body: BackendBody) -> StatusOut:
    """Switch the live backend. Takes effect on the next model call; nothing
    already stored (rewrites, drafts) is touched.

    The choice is tested with one real call before it is saved. Saving a model
    name with a typo in it used to succeed, and the first sign of it was every
    later call failing.
    """
    if body.backend not in backends.MODULES:
        raise HTTPException(400, f"Unknown backend '{body.backend}'.")
    model = body.model.strip()
    if len(model) > 120 or any(c.isspace() for c in model):
        raise HTTPException(400, "A model name is a single word, e.g. gpt-5.5 or google/gemini-2.5-pro.")
    check = await claude.ping(body.backend, model or None)
    if not check["ok"]:
        hint = backends.suggest_model(body.backend, model)
        obs.warn("settings.backend_rejected", backend=body.backend,
                 model=model or "default", suggested=hint)
        raise HTTPException(400, f"Not switched: {check['detail']}"
                                 + (f" Did you mean {hint}?" if hint else ""))
    db.set_setting("backend", body.backend)
    db.set_setting(f"model.{body.backend}", model)
    backends.select(body.backend, {body.backend: model})
    out = status()
    obs.event("settings.backend", backend=body.backend,
              model=backends.model_label(), ready=out.ready)
    return out


@app.post("/api/settings")
def save_settings(body: SettingsBody) -> SettingsOut:
    name = re.sub(r"[^A-Za-z0-9]", "", body.first_name).lower() or "resume"
    db.set_setting("first_name", name)
    return SettingsOut(first_name=name)


# --------------------------------------------------------------------------
# editable prompts
# --------------------------------------------------------------------------

PROMPT_MAX_CHARS = 20000


@app.get("/api/prompts")
def prompts() -> PromptsOut:
    return PromptsOut(prompts=claude.list_prompts())


def _prompt_info(key: str) -> PromptInfo:
    return PromptInfo(**next(x for x in claude.list_prompts() if x["key"] == key))


@app.put("/api/prompts/{key}")
def save_prompt(key: str, body: PromptBody) -> PromptInfo:
    """Replace the instructions for `key`. Text identical to the default, or
    blank, is a reset: there is then nothing custom left to keep."""
    if key not in claude.EDITABLE_PROMPTS:
        raise HTTPException(404, f"No editable prompt called '{key}'.")
    text = body.text.strip()
    if len(text) > PROMPT_MAX_CHARS:
        raise HTTPException(400, f"Keep a prompt under {PROMPT_MAX_CHARS:,} characters.")
    if not text or text == claude.default_prompt(key).strip():
        db.delete_setting(f"prompt.{key}")
    else:
        db.set_setting(f"prompt.{key}", text)
    claude.set_prompt_overrides(db.settings_with_prefix("prompt."))
    custom = key in claude._prompt_overrides
    obs.event("prompt.saved", key=key, custom=custom, **obs.tmeta(text, "prompt"))
    return _prompt_info(key)


@app.delete("/api/prompts/{key}")
def reset_prompt(key: str) -> PromptInfo:
    if key not in claude.EDITABLE_PROMPTS:
        raise HTTPException(404, f"No editable prompt called '{key}'.")
    db.delete_setting(f"prompt.{key}")
    claude.set_prompt_overrides(db.settings_with_prefix("prompt."))
    obs.event("prompt.reset", key=key)
    return _prompt_info(key)


# --------------------------------------------------------------------------
# resumes
# --------------------------------------------------------------------------

@app.get("/api/resumes")
def resumes() -> list[ResumeSummary]:
    return [ResumeSummary(**r) for r in db.list_resumes()]


@app.post("/api/resumes")
async def upload_resume(file: UploadFile = File(...), label: str = Form("")) -> ResumeOut:
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Use PDF, DOCX, TXT or MD.")

    ensure_dirs()
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "resume").name)
    stored = UPLOAD_DIR / f"{int(time.time())}_{safe}"
    with stored.open("wb") as out:
        shutil.copyfileobj(file.file, out)

    obs.event("upload.received", filename_ext=ext,
              bytes=stored.stat().st_size)
    try:
        with obs.timer() as t:
            doc = parse_resume(str(stored))
        obs.event("parse.ok", ms=t.ms, blocks=len(doc["blocks"]))
    except Exception as exc:  # noqa: BLE001
        stored.unlink(missing_ok=True)
        obs.error("parse.failed", exc=exc, ext=ext)
        raise HTTPException(400, f"Could not parse that resume: {type(exc).__name__}: {exc}")

    if not doc["blocks"]:
        stored.unlink(missing_ok=True)
        raise HTTPException(400, "No text found in that file. If it is a scanned PDF, it needs OCR first.")

    resume_label = label.strip() or Path(file.filename or "resume").stem
    filename = file.filename or safe
    rid = db.add_resume(
        label=resume_label,
        filename=filename,
        stored_path=str(stored),
        person=doc.get("name", ""),
        doc=doc,
    )
    written = report.write_parse_report(LOG_DIR, rid, resume_label, doc)
    obs.event("resume.stored", resume_id=rid,
              points=sum(1 for b in doc["blocks"] if b["mappable"]),
              report=str(written.name) if written else None)
    return _resume_out(rid, resume_label, filename, doc)


def _resume_out(rid: int, label: str, filename: str, doc: dict) -> ResumeOut:
    """A stored resume for the browser, without the geometry only the renderer reads."""
    return ResumeOut(
        id=rid, label=label, filename=filename,
        diagnostics=doc.get("diagnostics") or {},
        name=doc.get("name", ""),
        sections=doc.get("sections", []),
        blocks=[{k: v for k, v in b.items() if k != "_line"} for b in doc.get("blocks", [])],
    )


@app.get("/api/resumes/{resume_id}")
def resume_detail(resume_id: int) -> ResumeOut:
    r = db.get_resume(resume_id)
    if not r:
        raise HTTPException(404, "Resume not found")
    return _resume_out(r["id"], r["label"], r["filename"], r["doc"])


@app.delete("/api/resumes/{resume_id}")
def resume_delete(resume_id: int) -> Ok:
    r = db.get_resume(resume_id)
    if not r:
        raise HTTPException(404, "Resume not found")
    Path(r["stored_path"]).unlink(missing_ok=True)
    db.delete_resume(resume_id)
    return Ok()


# --------------------------------------------------------------------------
# experiences (things that are not on the resume)
# --------------------------------------------------------------------------

@app.get("/api/experiences")
def experiences() -> list[Experience]:
    return [Experience(**e) for e in db.list_experiences()]


@app.post("/api/experiences")
def add_experience(body: ExperienceCreate) -> Experience:
    text = body.text.strip()
    if len(text) < 15:
        raise HTTPException(400, "Describe the experience in at least a sentence: "
                                 "what you did, with what, and what came of it.")
    eid = db.add_experience(body.kind, body.title.strip()[:80], text)
    obs.event("experience.added", id=eid, kind=body.kind,
              has_title=bool(body.title.strip()), **obs.tmeta(text, "exp"))
    return Experience(**next(e for e in db.list_experiences() if e["id"] == eid))


@app.delete("/api/experiences/{experience_id}")
def delete_experience(experience_id: int) -> ExperienceDeleted:
    if not any(e["id"] == experience_id for e in db.list_experiences()):
        raise HTTPException(404, "Experience not found")
    gone = db.delete_experience(experience_id)
    obs.event("experience.deleted", id=experience_id, **gone)
    return ExperienceDeleted(**gone)


# --------------------------------------------------------------------------
# jobs (JD)
# --------------------------------------------------------------------------

@app.get("/api/jobs")
def jobs(resume_id: int | None = None) -> list[JobSummary]:
    return [JobSummary(**j) for j in db.list_jobs(resume_id)]


@app.post("/api/jobs")
async def create_job(body: JobCreate) -> JobCreated:
    if not db.get_resume(body.resume_id):
        raise HTTPException(404, "Resume not found")
    if len(body.text.strip()) < 40:
        raise HTTPException(400, "That job description looks too short to parse.")

    try:
        parsed = await claude.parse_jd(body.text)
    except claude.BackendUnavailable as exc:
        raise HTTPException(503, str(exc))

    if not parsed["points"]:
        raise HTTPException(400, "Could not find any requirements in that job description.")

    jid = db.add_job(
        resume_id=body.resume_id,
        company=parsed["company"],
        role=parsed["role"],
        raw=body.text,
        points=parsed["points"],
        mode=body.mode,
    )
    return JobCreated(
        id=jid,
        company=parsed["company"],
        role=parsed["role"],
        points=parsed["points"],
        mode=body.mode,
        source=parsed.get("source", "claude"),
        warning=parsed.get("warning"),
    )


@app.get("/api/jobs/{job_id}")
def job_detail(job_id: int) -> JobDetail:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    return JobDetail(
        id=j["id"],
        resume_id=j["resume_id"],
        company=j["company"],
        role=j["role"],
        mode=j["mode"],
        raw=j["raw_text"],
        points=j["points"],
        mappings=db.get_mappings(job_id),
        box_modes=db.get_box_modes(job_id),
        rewrites=db.get_rewrites(job_id),
        outreach=db.get_outreach(job_id),
        resume=_resume_out(r["id"], r["label"], r["filename"], r["doc"]) if r else None,
    )


@app.patch("/api/jobs/{job_id}")
def patch_job(job_id: int, body: JobPatch) -> JobSummary:
    """Change a job's company, role or mode; returns the job as it now stands."""
    if not db.get_job(job_id):
        raise HTTPException(404, "Job not found")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    db.update_job(job_id, **fields)
    j = db.get_job(job_id)
    return JobSummary(**{k: j[k] for k in JobSummary.model_fields})


@app.delete("/api/jobs/{job_id}")
def job_delete(job_id: int) -> Ok:
    db.delete_job(job_id)
    return Ok()


@app.put("/api/jobs/{job_id}/points")
def set_points(job_id: int, body: PointsKeep) -> PointsKept:
    """Choose which parsed points to carry into mapping.

    Dropping is reversible: the point keeps its text and its id and is only
    flagged, so the UI can put it back without re-parsing the JD.
    """
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    points = j["points"]
    keep = set(body.keep)
    unknown = sorted(keep - {p["id"] for p in points})
    if unknown:
        raise HTTPException(400, f"Unknown JD point(s): {', '.join(unknown)}")
    if not keep:
        raise HTTPException(400, "Keep at least one JD point to map against.")

    for p in points:
        p["dropped"] = p["id"] not in keep
    db.set_points(job_id, points)

    # A dropped point cannot stay wired to a resume box, or transform would
    # rewrite bullets against requirements the user has already discarded.
    gone = {p["id"] for p in points if p["dropped"]}
    unwired = 0
    for block_id, ids in db.get_mappings(job_id).items():
        left = [i for i in ids if i not in gone]
        if len(left) != len(ids):
            db.set_mappings(job_id, block_id, left)
            unwired += len(ids) - len(left)
    obs.event("jd.points_kept", job_id=job_id, kept=len(keep),
              dropped=len(gone), unwired=unwired)
    return PointsKept(points=points, dropped=sorted(gone), unwired=unwired)


@app.post("/api/jobs/{job_id}/mappings")
def save_mapping(job_id: int, body: MappingBody) -> MappingSaved:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    valid = {p["id"] for p in j["points"] if not p.get("dropped")}
    unknown = [p for p in body.point_ids if p not in valid]
    if unknown:
        obs.warn("map.unknown_points", job_id=job_id, ids=unknown)
        raise HTTPException(400, f"Unknown JD point(s): {', '.join(unknown)}")
    before = db.get_mappings(job_id).get(body.block_id, [])
    db.set_mappings(job_id, body.block_id, body.point_ids)
    obs.event("map.set", job_id=job_id, block=body.block_id,
              points=body.point_ids, added=len(set(body.point_ids) - set(before)),
              removed=len(set(before) - set(body.point_ids)))
    return MappingSaved(block_id=body.block_id, point_ids=body.point_ids)


@app.post("/api/jobs/{job_id}/box-modes")
def save_box_mode(job_id: int, body: BoxModeBody) -> BoxModeSaved:
    """Set how one resume box is transformed. Experiences are always new bullets,
    so only a parsed block can be replaced."""
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r or not any(b["id"] == body.block_id for b in r["doc"]["blocks"]):
        raise HTTPException(400, f"'{body.block_id}' is not a box on this resume.")
    db.set_box_mode(job_id, body.block_id, body.mode)
    obs.event("map.box_mode", job_id=job_id, block=body.block_id, mode=body.mode or "job")
    return BoxModeSaved(block_id=body.block_id, mode=body.mode)


# --------------------------------------------------------------------------
# transform
# --------------------------------------------------------------------------

@app.post("/api/jobs/{job_id}/transform")
async def transform(job_id: int, body: TransformBody) -> TransformOut:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r:
        raise HTTPException(404, "Resume not found")
    why = claude.backend_status()
    if why:
        raise HTTPException(503, why)

    blocks = {b["id"]: b for b in r["doc"]["blocks"]}
    exps = {e["key"]: e for e in db.list_experiences()}
    old = db.get_rewrites(job_id)
    jobs_to_run = _transform_jobs(j, r["doc"], body.block_ids, body.extra)
    replaced = {x["block_id"] for x in jobs_to_run if x.get("kind") == "replace"}

    if not jobs_to_run:
        raise HTTPException(400, "Nothing to transform. Map at least one resume box to a JD point first.")

    obs.event("transform.start", job_id=job_id, mode=j["mode"],
              bullets=len(jobs_to_run), replaced=len(replaced), reroll=bool(body.block_ids),
              has_note=bool(body.extra.strip()))
    results = await claude.rewrite_many(jobs_to_run)
    for res in results:
        bid = res["block_id"]
        if not res.get("ok"):
            continue
        meta = {
            "keywords_used": res["keywords_used"],
            "substitutions": res["substitutions"],
            "defensible": res["defensible"],
        }
        if bid in exps:
            # A re-roll re-suggests a spot, but one the user picked stays put.
            prev = (old.get(bid) or {}).get("meta") or {}
            meta["placement"] = (prev["placement"] if prev.get("placement_user")
                                 else res["placement"])
            meta["placement_user"] = bool(prev.get("placement_user"))
            original = exps[bid]["text"]
        else:
            original = blocks[bid]["text"]
            if bid in replaced:
                meta["replaced"] = True
        db.save_rewrite(job_id, bid, original, res["rewritten"], "pending", meta)
    return TransformOut(results=results, rewrites=db.get_rewrites(job_id))


def _transform_jobs(j: dict, doc: dict, block_ids: list[str] | None,
                    extra: str) -> list[dict]:
    """One job per mapped box: the arguments its rewrite call will get.

    Shared by transform and the prompt preview, so the preview is exactly what
    goes out rather than a second guess at it.
    """
    mappings = db.get_mappings(j["id"])
    blocks = {b["id"]: b for b in doc["blocks"]}
    by_point = {p["id"]: p for p in j["points"]}
    layout = db.get_layout(j["id"])
    budget = line_budget(doc, layout)
    exps = {e["key"]: e for e in db.list_experiences()}
    box_modes = db.get_box_modes(j["id"])
    out = []
    for bid in block_ids or list(mappings.keys()):
        pts = [by_point[p] for p in mappings.get(bid, []) if p in by_point]
        if not pts:
            continue
        if bid in exps:
            out.append({
                "block_id": bid,
                "kind": "experience",
                "args": {
                    "experience": exps[bid],
                    "jd_points": pts,
                    "role": j["role"],
                    "company": j["company"],
                    "placements": _placements(doc),
                    "examples": [b["text"] for b in doc["blocks"] if b["mappable"]][:3],
                    "mode": j["mode"],
                    "extra": extra,
                    "max_chars": new_bullet_budget(doc, layout),
                },
            })
        elif bid in blocks and box_modes.get(bid) == "replace":
            blk = blocks[bid]
            entry, siblings = _entry_context(doc, bid)
            out.append({
                "block_id": bid,
                "kind": "replace",
                "args": {
                    "bullet": blk["text"],
                    "section": blk.get("section", ""),
                    "entry": entry,
                    "siblings": siblings,
                    "jd_points": pts,
                    "role": j["role"],
                    "company": j["company"],
                    "extra": extra,
                    "max_chars": budget.get(bid, 0),
                },
            })
        elif bid in blocks:
            blk = blocks[bid]
            out.append({
                "block_id": bid,
                "args": {
                    "bullet": blk["text"],
                    "section": blk.get("section", ""),
                    "jd_points": pts,
                    "role": j["role"],
                    "company": j["company"],
                    "mode": j["mode"],
                    "extra": extra,
                    "max_chars": budget.get(bid, 0),
                },
            })
    return out


@app.get("/api/jobs/{job_id}/prompt")
def prompt_preview(job_id: int, block_id: str) -> PromptPreview:
    """The exact system prompt and message Transform would send for one box."""
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r:
        raise HTTPException(404, "Resume not found")
    jobs = _transform_jobs(j, r["doc"], [block_id], "")
    if not jobs:
        raise HTTPException(400, "That box is not mapped to any JD point yet.")
    job = jobs[0]
    kind = job.get("kind") or "rewrite"
    build = {"experience": claude.experience_request,
             "replace": claude.replace_request}.get(kind, claude.rewrite_request)
    system, user = build(**job["args"])
    return PromptPreview(block_id=block_id, kind=kind, mode=j["mode"], system=system, user=user)


def _placements(doc: dict) -> list[dict]:
    """The entries and sections a new bullet can be placed under."""
    out = []
    for b in doc.get("blocks", []):
        if b["kind"] not in ("entry", "section"):
            continue
        text = _left_text(b) + (f" ({b['right']})" if b.get("right") else "")
        out.append({"id": b["id"], "kind": b["kind"], "text": text,
                    "section": b.get("section", "")})
    return out


def _entry_context(doc: dict, block_id: str) -> tuple[str, list[str]]:
    """The entry a bullet sits under, and that entry's other points.

    A replaced bullet has nothing of its own to stand on, so these are what
    keep it plausible for the role and stop it repeating its neighbours.
    """
    entry, group, found = "", [], False
    for b in doc.get("blocks", []):
        if b["kind"] in ("entry", "section"):
            if found:
                break
            entry = (_left_text(b) + (f" ({b['right']})" if b.get("right") else "")
                     if b["kind"] == "entry" else "")
            group = []
        elif b["id"] == block_id:
            found = True
        elif b.get("mappable"):
            group.append(b["text"])
    return (entry, group) if found else ("", [])


@app.patch("/api/jobs/{job_id}/rewrites/{block_id}")
def patch_rewrite(job_id: int, block_id: str, body: RewritePatch) -> Rewrite:
    existing = db.get_rewrites(job_id).get(block_id)
    if not existing:
        raise HTTPException(404, "No rewrite for that block yet.")
    if body.placement is not None:
        j = db.get_job(job_id)
        r = db.get_resume(j["resume_id"]) if j else None
        place = body.placement.strip()
        anchors = {p["id"] for p in _placements(r["doc"])} if r else set()
        if not (place in anchors or (place.startswith("new:") and 0 < len(place[4:].strip()) <= 40)):
            raise HTTPException(400, f"Can't place a bullet at '{place}'.")
        obs.event("review.placement", job_id=job_id, block=block_id,
                  placement="new" if place.startswith("new:") else place)
        meta = {**existing["meta"], "placement": place, "placement_user": True}
        db.save_rewrite(job_id, block_id, existing["original"], existing["rewritten"],
                        existing["status"], meta)
        existing = db.get_rewrites(job_id)[block_id]
    if body.text is not None:
        obs.event("review.edit", job_id=job_id, block=block_id,
                  from_status=existing["status"], to_status=body.status or "edited",
                  **obs.tmeta(body.text, "edited"))
        db.save_rewrite(job_id, block_id, existing["original"], body.text.strip(),
                        body.status or "edited", existing["meta"])
    elif body.status is not None:
        obs.event("review.status", job_id=job_id, block=block_id,
                  from_status=existing["status"], to_status=body.status)
        db.set_rewrite_status(job_id, block_id, body.status)
    return Rewrite(**db.get_rewrites(job_id)[block_id])


@app.post("/api/jobs/{job_id}/accept-all")
def accept_all(job_id: int) -> dict[str, Rewrite]:
    rws = db.get_rewrites(job_id)
    pending = [bid for bid, rw in rws.items() if rw["status"] == "pending"]
    for bid in pending:
        db.set_rewrite_status(job_id, bid, "accepted")
    obs.event("review.accept_all", job_id=job_id, accepted=len(pending))
    return {bid: Rewrite(**rw) for bid, rw in db.get_rewrites(job_id).items()}


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def _build_pdf(job_id: int, preview: bool) -> tuple[Path, str, dict]:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r:
        raise HTTPException(404, "Resume not found")

    rewrites = db.get_rewrites(job_id)
    first = db.get_setting("first_name", DEFAULT_FIRST_NAME)
    name = export_filename(first, j["company"] or "company")
    ensure_dirs()
    out = EXPORT_DIR / (f"_preview_{job_id}.pdf" if preview else name)
    fit = render_resume(_render_doc(r["doc"], rewrites), str(out), _overrides(rewrites),
                        layout=db.get_layout(job_id))
    return out, name, fit


def _render_doc(doc: dict, rewrites: dict) -> dict:
    """The parsed resume plus the bullets written from experiences that are going in.

    Export still does not rewrite the stored document: this is a copy, rebuilt
    on every render, so dropping or re-placing a bullet needs no clean-up.
    """
    inserts = sorted(
        ({"id": bid, "text": rw["rewritten"].strip(),
          "placement": (rw["meta"] or {}).get("placement", "")}
         for bid, rw in rewrites.items()
         if db.experience_id(bid) is not None and rw["status"] in INCLUDED_STATUSES
         and rw["rewritten"].strip()),
        key=lambda x: db.experience_id(x["id"]))
    return insert_blocks(doc, inserts)


def _overrides(rewrites: dict) -> dict[str, str]:
    """{block_id: text} for every rewrite that goes into the PDF."""
    return {
        bid: rw["rewritten"]
        for bid, rw in rewrites.items()
        if rw["status"] in INCLUDED_STATUSES and rw["rewritten"].strip()
    }


@app.get("/api/jobs/{job_id}/preview.pdf")
def preview(job_id: int) -> FileResponse:
    out, _name, _fit = _build_pdf(job_id, preview=True)
    return FileResponse(out, media_type="application/pdf",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/jobs/{job_id}/export")
def export(job_id: int) -> ExportOut:
    out, name, fit = _build_pdf(job_id, preview=False)
    rewrites = db.get_rewrites(job_id)
    j = db.get_job(job_id)
    r = db.get_resume(j["resume_id"]) if j else None
    check = report.verify_pdf(out, r["doc"] if r else {},
                              r["stored_path"] if r else None)
    obs.event("export.done", job_id=job_id, filename=name,
              bytes=out.stat().st_size, pages=fit["pages"],
              font_scale=fit["font_scale"], cramped=fit["cramped"],
              applied=sum(1 for rw in rewrites.values()
                          if rw["status"] in INCLUDED_STATUSES),
              selfcheck_ok=check.get("ok"),
              problems=check.get("problems") or None)
    return ExportOut(
        check={k: v for k, v in check.items() if k != "fonts"},
        filename=name,
        url=f"/api/download/{name}",
        applied=sum(1 for rw in rewrites.values() if rw["status"] in INCLUDED_STATUSES),
        total=len(rewrites),
        pages=fit["pages"],
        font_scale=fit["font_scale"],
        cramped=fit["cramped"],
    )


@app.get("/api/jobs/{job_id}/fit")
def fit_info(job_id: int) -> FitOut:
    """How hard the resume had to be squeezed to reach a single page."""
    _out, _name, fit = _build_pdf(job_id, preview=True)
    return FitOut(**{k: v for k, v in fit.items() if k not in ("path", "blocks")})


@app.get("/api/download/{filename}")
def download(filename: str) -> FileResponse:
    safe = Path(filename).name
    path = EXPORT_DIR / safe
    if not path.exists():
        raise HTTPException(404, "Export not found. Generate it again.")
    return FileResponse(path, media_type="application/pdf", filename=safe)


# --------------------------------------------------------------------------
# corrections chat
# --------------------------------------------------------------------------

CHAT_HISTORY = 12


def _left_text(blk: dict) -> str:
    """A block's editable text: an entry's right-aligned column is not part of it."""
    return (blk.get("left") or blk["text"]) if blk.get("right") else blk["text"]


def _chat_blocks(doc: dict, rewrites: dict, stats: dict) -> list[dict]:
    """Every block as the model sees it: rendered wording, original, line counts.

    `doc` is the render doc, so bullets written from experiences are listed
    with the user's own notes as their original. Headings spliced in for them
    are left out: they are named by the bullet's placement, not edited.
    """
    out = []
    for b in doc.get("blocks", []):
        if b.get("inserted") and b["kind"] == "section":
            continue
        rw = rewrites.get(b["id"])
        applied = bool(rw and rw["status"] in INCLUDED_STATUSES and rw["rewritten"].strip())
        st = stats.get(b["id"]) or {}
        new = bool(b.get("inserted"))
        out.append({
            "id": b["id"], "kind": b["kind"], "section": b.get("section", ""),
            "text": rw["rewritten"] if applied else _left_text(b),
            "original": rw["original"] if new and rw else _left_text(b),
            "new": new, "right": b.get("right", ""),
            "pending": bool(rw) and rw["status"] == "pending",
            "lines": st.get("lines", 1), "source_lines": st.get("source_lines", 1),
            "chars_per_line": st.get("chars_per_line", 0),
        })
    return out


def _applied(m: dict) -> bool:
    meta = m.get("meta") or {}
    return m["role"] == "assistant" and not meta.get("undone") and bool(
        meta.get("edits") or meta.get("layout"))


def _chat_public(job_id: int) -> ChatState:
    """Messages without their undo snapshots, the latest applied one undoable."""
    msgs = db.get_chat(job_id)
    last = next((m["id"] for m in reversed(msgs) if _applied(m)), None)
    return ChatState(
        messages=[
            ChatMessageOut(id=m["id"], role=m["role"], text=m["text"],
                           meta={k: v for k, v in (m["meta"] or {}).items() if k != "before"},
                           can_undo=m["id"] == last)
            for m in msgs
        ],
        rewrites=db.get_rewrites(job_id),
        layout=clean_layout(db.get_layout(job_id)),
    )


@app.get("/api/jobs/{job_id}/chat")
def get_chat(job_id: int) -> ChatState:
    if not db.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return _chat_public(job_id)


@app.post("/api/jobs/{job_id}/chat")
async def chat(job_id: int, body: ChatBody) -> ChatState:
    """Apply one correction request to the PDF: block edits and layout changes.

    Edits are saved as "edited" rewrites, so they are in the PDF at once and in
    the review step with their substitutions, like a hand edit. The state each
    one replaced is kept on the reply, which is what Undo restores.
    """
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r:
        raise HTTPException(404, "Resume not found")
    why = claude.backend_status()
    if why:
        raise HTTPException(503, why)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "Type what you want changed first.")

    _out, _name, fit = _build_pdf(job_id, preview=True)
    rewrites = db.get_rewrites(job_id)
    doc = _render_doc(r["doc"], rewrites)
    layout = fit["layout"]
    history = db.get_chat(job_id)
    try:
        rev = await claude.revise_pdf(
            blocks=_chat_blocks(doc, rewrites, fit["blocks"]),
            layout=layout, fit=fit, geometry=doc.get("geometry") or {},
            role=j["role"], company=j["company"], jd_points=j["points"],
            history=history[-CHAT_HISTORY:], message=message,
        )
    except claude.BackendUnavailable as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:  # noqa: BLE001 - say why, keep the page as it was
        obs.record_failure("pdf_chat", exc, 0.0, job_id=job_id)
        raise HTTPException(502, f"Could not apply that: {type(exc).__name__}: {exc}")

    blocks = {b["id"]: b for b in doc.get("blocks", [])}
    before: dict[str, dict | None] = {}
    for e in rev["edits"]:
        bid = e["block_id"]
        old = rewrites.get(bid)
        before[bid] = old
        meta = {**((old or {}).get("meta") or {}),
                "substitutions": e["substitutions"],
                "defensible": e["defensible"], "source": "chat"}
        db.save_rewrite(job_id, bid, old["original"] if old else _left_text(blocks[bid]),
                        e["text"], "edited", meta)

    new_layout = clean_layout({**layout, **rev["layout"]})
    changed = {k: v for k, v in new_layout.items() if v != layout.get(k)}
    if changed:
        db.set_layout(job_id, new_layout)

    db.add_chat(job_id, "user", message, {})
    db.add_chat(job_id, "assistant", rev["reply"] or "Done.", {
        "edits": [e["block_id"] for e in rev["edits"]],
        "layout": changed,
        "before": {"rewrites": before, "layout": layout},
    })
    obs.event("pdfchat.applied", job_id=job_id, edits=len(rev["edits"]),
              layout=sorted(changed) or None)
    return _chat_public(job_id)


@app.post("/api/jobs/{job_id}/chat/{message_id}/undo")
def undo_chat(job_id: int, message_id: int) -> ChatState:
    """Put back what one correction replaced. Only the latest applied one, so an
    undo can never silently revert a later change to the same block."""
    msgs = db.get_chat(job_id)
    target = next((m for m in msgs if m["id"] == message_id), None)
    if not target or target["role"] != "assistant":
        raise HTTPException(404, "No such correction.")
    if not _applied(target):
        raise HTTPException(400, "That correction changed nothing, or is already undone.")
    if any(_applied(m) for m in msgs if m["id"] > message_id):
        raise HTTPException(409, "Undo the later corrections first.")

    before = target["meta"].get("before") or {}
    for bid, row in (before.get("rewrites") or {}).items():
        if row is None:
            db.delete_rewrite(job_id, bid)
        else:
            db.save_rewrite(job_id, bid, row["original"], row["rewritten"],
                            row["status"], row["meta"])
    db.set_layout(job_id, before.get("layout") or {})
    db.set_chat_meta(message_id, {**target["meta"], "undone": True})
    obs.event("pdfchat.undone", job_id=job_id, message=message_id,
              edits=len(before.get("rewrites") or {}))
    return _chat_public(job_id)


# --------------------------------------------------------------------------
# recruiter outreach
# --------------------------------------------------------------------------

HIGHLIGHT_LIMIT = 8


def _highlights(doc: dict, rewrites: dict, mappings: dict) -> list[str]:
    """The lines the outreach may quote: what the exported resume will say.

    Preference order per point: the tailored wording that is actually going into
    the PDF, then anything mapped to the JD but not accepted, then -- only if
    neither exists -- the resume's own strongest points, so a draft is still
    possible before the review step is finished.
    """
    picked: list[str] = []
    spare: list[str] = []
    for b in doc.get("blocks", []):
        if not b.get("mappable"):
            continue
        rw = rewrites.get(b["id"])
        if rw and rw["status"] in INCLUDED_STATUSES and rw["rewritten"].strip():
            picked.append(rw["rewritten"].strip())
        elif mappings.get(b["id"]):
            picked.append(b["text"])
        else:
            spare.append(b["text"])
    return (picked or spare)[:HIGHLIGHT_LIMIT]


@app.get("/api/jobs/{job_id}/outreach")
def get_outreach(job_id: int) -> OutreachOut:
    if not db.get_job(job_id):
        raise HTTPException(404, "Job not found")
    return OutreachOut(outreach=db.get_outreach(job_id))


@app.post("/api/jobs/{job_id}/outreach")
async def make_outreach(job_id: int, body: OutreachBody) -> OutreachOut:
    j = db.get_job(job_id)
    if not j:
        raise HTTPException(404, "Job not found")
    r = db.get_resume(j["resume_id"])
    if not r:
        raise HTTPException(404, "Resume not found")
    why = claude.backend_status()
    if why:
        raise HTTPException(503, why)

    rewrites = db.get_rewrites(job_id)
    highlights = _highlights(_render_doc(r["doc"], rewrites), rewrites,
                             db.get_mappings(job_id))
    if not highlights:
        raise HTTPException(400, "No resume points to write from. Map and transform some boxes first.")

    try:
        draft = await claude.draft_outreach(
            candidate=r["doc"].get("name", "") or r["person_name"],
            recruiter=body.recruiter.strip(),
            role=j["role"],
            company=j["company"],
            highlights=highlights,
            jd_points=j["points"],
            extra=body.extra,
            examples=db.list_outreach_examples(),
        )
    except claude.BackendUnavailable as exc:
        raise HTTPException(503, str(exc))
    except Exception as exc:  # noqa: BLE001 - the draft is optional, say why it failed
        obs.record_failure("outreach", exc, 0.0, job_id=job_id)
        raise HTTPException(502, f"Could not draft the outreach: {type(exc).__name__}: {exc}")

    meta = {
        "highlights_used": draft["highlights_used"],
        "linkedin_limit": claude.LINKEDIN_LIMIT,
    }
    db.save_outreach(job_id, body.recruiter.strip(), draft["linkedin"],
                     draft["email_subject"], draft["email_body"], meta,
                     linkedin_subject=draft["linkedin_subject"])
    obs.event("outreach.done", job_id=job_id, highlights=len(highlights),
              linkedin_chars=len(draft["linkedin"]),
              over_limit=len(draft["linkedin"]) > claude.LINKEDIN_LIMIT)
    return OutreachOut(outreach=db.get_outreach(job_id))


@app.patch("/api/jobs/{job_id}/outreach")
def patch_outreach(job_id: int, body: OutreachPatch) -> OutreachOut:
    existing = db.get_outreach(job_id)
    if not existing:
        raise HTTPException(404, "Nothing drafted for this job yet.")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    merged = {**existing, **fields}
    db.save_outreach(job_id, merged["recruiter"], merged["linkedin"].strip(),
                     merged["email_subject"].strip(), merged["email_body"].strip(),
                     existing["meta"],
                     linkedin_subject=merged["linkedin_subject"].strip())
    obs.event("outreach.edited", job_id=job_id, fields=sorted(fields))
    return OutreachOut(outreach=db.get_outreach(job_id))


# Liked messages, shared by every job: the newest few of each kind go into each
# draft as style references (claude.OUTREACH_EXAMPLE_LIMIT).
EXAMPLE_MAX_CHARS = 4000


@app.get("/api/outreach-examples")
def outreach_examples() -> list[OutreachExample]:
    return [OutreachExample(**e) for e in db.list_outreach_examples()]


@app.post("/api/outreach-examples")
def add_outreach_example(body: OutreachExampleCreate) -> OutreachExample:
    text = body.body.strip()
    if len(text) < 20:
        raise HTTPException(400, "Paste the whole message you liked, not just a line of it.")
    if len(text) > EXAMPLE_MAX_CHARS:
        raise HTTPException(400, f"That example is {len(text)} characters. Keep it under "
                                 f"{EXAMPLE_MAX_CHARS}: one message, not a thread.")
    eid = db.add_outreach_example(body.kind, body.subject.strip()[:200], text)
    obs.event("outreach_example.added", id=eid, kind=body.kind,
              has_subject=bool(body.subject.strip()), **obs.tmeta(text, "ex"))
    return OutreachExample(**next(e for e in db.list_outreach_examples() if e["id"] == eid))


@app.delete("/api/outreach-examples/{example_id}")
def delete_outreach_example(example_id: int) -> Ok:
    if not db.delete_outreach_example(example_id):
        raise HTTPException(404, "Example not found")
    obs.event("outreach_example.deleted", id=example_id)
    return Ok()


# --------------------------------------------------------------------------
# diagnostics
# --------------------------------------------------------------------------

@app.get("/api/debug/parse/{resume_id}", response_class=PlainTextResponse)
def debug_parse(resume_id: int) -> str:
    """The block-classification table for a stored resume."""
    r = db.get_resume(resume_id)
    if not r:
        raise HTTPException(404, "Resume not found")
    return report.parse_report(resume_id, r["label"], r["doc"])


@app.get("/api/debug/usage")
def debug_usage() -> UsageOut:
    """Token and cost totals for this process, broken down by operation."""
    return UsageOut(model=backends.model_label(), backend=backends.current_name(),
                    **obs.totals())


# --------------------------------------------------------------------------
# static UI (mounted last so /api routes win)
# --------------------------------------------------------------------------

@app.exception_handler(claude.BackendUnavailable)
async def _backend_unavailable(_req, exc: claude.BackendUnavailable) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(RequestValidationError)
async def _invalid_request(_req, exc: RequestValidationError) -> JSONResponse:
    """422, with the per-field errors joined into one readable `detail`."""
    errors = jsonable_encoder(exc.errors())
    detail = "; ".join(
        f"{'.'.join(str(p) for p in e['loc'][1:]) or e['loc'][0]}: {e['msg']}" for e in errors)
    return JSONResponse(ErrorOut(detail=detail, errors=errors).model_dump(), status_code=422)


class _NoCacheStatic(StaticFiles):
    """Serve the UI with revalidation forced.

    The frontend is three files on 127.0.0.1, so caching buys nothing, while a
    browser holding yesterday's `app.js` silently hides whatever changed.
    `no-cache` still allows a 304 against the ETag; it only bans using the copy
    without asking.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[override]
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/", _NoCacheStatic(directory=str(FRONTEND_DIR), html=True), name="frontend")
