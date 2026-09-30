"""One repository per table group, each handing out domain models.

A repository owns its SQL and nothing else: no HTTP, no rules about what a
change means. Rows come back as validated domain models, and JSON columns are
decoded on the way out and encoded on the way in. Writes accept either a
model or a plain dict, and a model is written as it was read: fields that
were never set stay absent from the stored JSON. ``add`` returns the new
entity as stored, read back through the same connection.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, TypeVar

from pydantic import BaseModel

from ..domain import (
    BoxMode,
    ChatMessage,
    ChatRole,
    Experience,
    ExperienceKind,
    Job,
    JobSummary,
    OutreachDraft,
    OutreachExample,
    OutreachKind,
    Resume,
    ResumeSummary,
    Rewrite,
    RewriteMode,
    RewriteStatus,
    experience_key,
)
from .database import Database

T = TypeVar("T")


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", by_alias=True, exclude_unset=True)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _json(value: Any) -> str:
    return json.dumps(_plain(value))


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def _created(entity: T | None) -> T:
    """The row an INSERT just wrote, read back in the same transaction."""
    if entity is None:
        raise RuntimeError("an inserted row could not be read back")
    return entity


class Repository:
    def __init__(self, database: Database) -> None:
        self._db = database


class ResumeRepository(Repository):
    @staticmethod
    def _fetch(conn: sqlite3.Connection, resume_id: int) -> Resume | None:
        r = _row(conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone())
        if r is None:
            return None
        r["doc"] = json.loads(r.pop("doc_json"))
        return Resume.model_validate(r)

    def add(self, *, label: str, filename: str, stored_path: str, person: str,
            doc: dict[str, Any] | BaseModel) -> Resume:
        with self._db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO resumes (label, filename, stored_path, person_name, doc_json,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (label, filename, stored_path, person, _json(doc), time.time()))
            return _created(self._fetch(conn, cur.lastrowid or 0))

    def all(self) -> list[ResumeSummary]:
        with self._db.connect() as conn:
            rows = conn.execute(
                "SELECT id, label, filename, person_name, created_at,"
                " (SELECT COUNT(*) FROM jobs WHERE jobs.resume_id = resumes.id) AS job_count"
                " FROM resumes ORDER BY created_at DESC").fetchall()
        return [ResumeSummary.model_validate(dict(r)) for r in rows]

    def get(self, resume_id: int) -> Resume | None:
        with self._db.connect() as conn:
            return self._fetch(conn, resume_id)

    def delete(self, resume_id: int) -> None:
        """The resume and, by cascade, every job made against it."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))


class JobRepository(Repository):
    @staticmethod
    def _fetch(conn: sqlite3.Connection, job_id: int) -> Job | None:
        r = _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        if r is None:
            return None
        r["points"] = json.loads(r.pop("points_json"))
        return Job.model_validate(r)

    def add(self, *, resume_id: int, company: str, role: str, raw: str,
            points: list[Any], mode: RewriteMode) -> Job:
        with self._db.connect() as conn:
            cur = conn.execute(
                "INSERT INTO jobs (resume_id, company, role, raw_text, points_json, mode,"
                " created_at) VALUES (?,?,?,?,?,?,?)",
                (resume_id, company, role, raw, _json(points), mode, time.time()))
            return _created(self._fetch(conn, cur.lastrowid or 0))

    def get(self, job_id: int) -> Job | None:
        with self._db.connect() as conn:
            return self._fetch(conn, job_id)

    def all(self, resume_id: int | None = None) -> list[JobSummary]:
        q = "SELECT id, resume_id, company, role, mode, created_at FROM jobs"
        args: tuple[Any, ...] = ()
        if resume_id is not None:
            q += " WHERE resume_id=?"
            args = (resume_id,)
        with self._db.connect() as conn:
            rows = conn.execute(q + " ORDER BY created_at DESC", args).fetchall()
        return [JobSummary.model_validate(dict(r)) for r in rows]

    def update(self, job_id: int, *, company: str | None = None, role: str | None = None,
               mode: RewriteMode | None = None) -> None:
        """Change the fields given; a None leaves that column as it is."""
        sets = {k: v for k, v in (("company", company), ("role", role), ("mode", mode))
                if v is not None}
        if not sets:
            return
        clause = ", ".join(f"{k}=?" for k in sets)
        with self._db.connect() as conn:
            conn.execute(f"UPDATE jobs SET {clause} WHERE id=?", (*sets.values(), job_id))

    def set_points(self, job_id: int, points: list[Any]) -> None:
        with self._db.connect() as conn:
            conn.execute("UPDATE jobs SET points_json=? WHERE id=?", (_json(points), job_id))

    def delete(self, job_id: int) -> None:
        """The job and, by cascade, everything filed under it."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


class MappingRepository(Repository):
    """Which JD points each box answers, and boxes set to be replaced."""

    def for_job(self, job_id: int) -> dict[str, list[str]]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT block_id, point_id FROM mappings WHERE job_id=?",
                                (job_id,)).fetchall()
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["block_id"], []).append(r["point_id"])
        return out

    def map_box(self, job_id: int, block_id: str, point_ids: list[str]) -> None:
        """Replace the points `block_id` answers; an empty list unmaps it."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM mappings WHERE job_id=? AND block_id=?",
                         (job_id, block_id))
            conn.executemany(
                "INSERT OR IGNORE INTO mappings (job_id, block_id, point_id) VALUES (?,?,?)",
                [(job_id, block_id, p) for p in point_ids])

    def delete_key(self, key: str) -> int:
        """Unmap `key` in every job. Returns how many links went."""
        with self._db.connect() as conn:
            return conn.execute("DELETE FROM mappings WHERE block_id=?", (key,)).rowcount

    def box_modes(self, job_id: int) -> dict[str, BoxMode]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT block_id, mode FROM box_modes WHERE job_id=?",
                                (job_id,)).fetchall()
        return {r["block_id"]: BoxMode(r["mode"]) for r in rows}

    def set_box_mode(self, job_id: int, block_id: str, mode: BoxMode) -> None:
        """Mark one box's mode; ``BoxMode.INHERIT`` puts it back on the job's."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM box_modes WHERE job_id=? AND block_id=?",
                         (job_id, block_id))
            if mode:
                conn.execute("INSERT INTO box_modes (job_id, block_id, mode) VALUES (?,?,?)",
                             (job_id, block_id, mode))


class RewriteRepository(Repository):
    def for_job(self, job_id: int) -> dict[str, Rewrite]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM rewrites WHERE job_id=?", (job_id,)).fetchall()
        return {r["block_id"]: self._model(r) for r in rows}

    def get(self, job_id: int, block_id: str) -> Rewrite | None:
        with self._db.connect() as conn:
            r = conn.execute("SELECT * FROM rewrites WHERE job_id=? AND block_id=?",
                             (job_id, block_id)).fetchone()
        return self._model(r) if r is not None else None

    @staticmethod
    def _model(r: sqlite3.Row) -> Rewrite:
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        return Rewrite.model_validate(d)

    def save(self, job_id: int, block_id: str, *, original: str, rewritten: str,
             status: RewriteStatus, meta: dict[str, Any] | BaseModel) -> None:
        """Insert, or update everything but `original`: a re-roll keeps the source text."""
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO rewrites (job_id, block_id, original, rewritten, status, meta_json,"
                " updated_at) VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id, block_id) DO UPDATE SET"
                " rewritten=excluded.rewritten, status=excluded.status,"
                " meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                (job_id, block_id, original, rewritten, status, _json(meta), time.time()))

    def set_status(self, job_id: int, block_id: str, status: RewriteStatus) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "UPDATE rewrites SET status=?, updated_at=? WHERE job_id=? AND block_id=?",
                (status, time.time(), job_id, block_id))

    def delete(self, job_id: int, block_id: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM rewrites WHERE job_id=? AND block_id=?",
                         (job_id, block_id))

    def delete_key(self, key: str) -> int:
        """Delete every job's rewrite filed under `key`. Returns how many went."""
        with self._db.connect() as conn:
            return conn.execute("DELETE FROM rewrites WHERE block_id=?", (key,)).rowcount


class OutreachRepository(Repository):
    def get(self, job_id: int) -> OutreachDraft | None:
        with self._db.connect() as conn:
            r = _row(conn.execute("SELECT * FROM outreach WHERE job_id=?",
                                  (job_id,)).fetchone())
        if r is None:
            return None
        r["meta"] = json.loads(r.pop("meta_json") or "{}")
        return OutreachDraft.model_validate(r)

    def save(self, job_id: int, *, recruiter: str, linkedin: str, linkedin_subject: str,
             email_subject: str, email_body: str, meta: dict[str, Any] | BaseModel) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO outreach (job_id, recruiter, linkedin, linkedin_subject,"
                " email_subject, email_body, meta_json, updated_at) VALUES (?,?,?,?,?,?,?,?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " recruiter=excluded.recruiter, linkedin=excluded.linkedin,"
                " linkedin_subject=excluded.linkedin_subject,"
                " email_subject=excluded.email_subject, email_body=excluded.email_body,"
                " meta_json=excluded.meta_json, updated_at=excluded.updated_at",
                (job_id, recruiter, linkedin, linkedin_subject, email_subject, email_body,
                 _json(meta), time.time()))


class OutreachExampleRepository(Repository):
    """Liked messages, shared by every job."""

    def all(self) -> list[OutreachExample]:
        """Newest first, so a prompt that keeps only the first few keeps the latest."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM outreach_examples"
                                " ORDER BY created_at DESC, id DESC").fetchall()
        return [OutreachExample.model_validate(dict(r)) for r in rows]

    @staticmethod
    def _fetch(conn: sqlite3.Connection, example_id: int) -> OutreachExample | None:
        r = conn.execute("SELECT * FROM outreach_examples WHERE id=?", (example_id,)).fetchone()
        return OutreachExample.model_validate(dict(r)) if r is not None else None

    def get(self, example_id: int) -> OutreachExample | None:
        with self._db.connect() as conn:
            return self._fetch(conn, example_id)

    def add(self, *, kind: OutreachKind, subject: str, body: str) -> OutreachExample:
        with self._db.connect() as conn:
            cur = conn.execute("INSERT INTO outreach_examples (kind, subject, body, created_at)"
                               " VALUES (?,?,?,?)", (kind, subject, body, time.time()))
            return _created(self._fetch(conn, cur.lastrowid or 0))

    def delete(self, example_id: int) -> bool:
        with self._db.connect() as conn:
            return conn.execute("DELETE FROM outreach_examples WHERE id=?",
                                (example_id,)).rowcount > 0


class ExperienceRepository(Repository):
    """Things that are not on the resume, shared by every job."""

    @staticmethod
    def _model(r: sqlite3.Row) -> Experience:
        return Experience.model_validate({**dict(r), "key": experience_key(r["id"])})

    def _fetch(self, conn: sqlite3.Connection, experience_id: int) -> Experience | None:
        r = conn.execute("SELECT * FROM experiences WHERE id=?", (experience_id,)).fetchone()
        return self._model(r) if r is not None else None

    def all(self) -> list[Experience]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM experiences ORDER BY id").fetchall()
        return [self._model(r) for r in rows]

    def get(self, experience_id: int) -> Experience | None:
        with self._db.connect() as conn:
            return self._fetch(conn, experience_id)

    def add(self, *, kind: ExperienceKind, title: str, text: str) -> Experience:
        with self._db.connect() as conn:
            cur = conn.execute("INSERT INTO experiences (kind, title, text, created_at)"
                               " VALUES (?,?,?,?)", (kind, title, text, time.time()))
            return _created(self._fetch(conn, cur.lastrowid or 0))

    def delete(self, experience_id: int) -> None:
        """The experience only; what is filed under its key is the caller's to remove."""
        with self._db.connect() as conn:
            conn.execute("DELETE FROM experiences WHERE id=?", (experience_id,))


class LayoutRepository(Repository):
    """Page-wide adjustments from the corrections chat, as stored (see pdfgen.clean_layout)."""

    def get(self, job_id: int) -> dict[str, Any]:
        with self._db.connect() as conn:
            r = conn.execute("SELECT layout_json FROM layouts WHERE job_id=?",
                             (job_id,)).fetchone()
        return json.loads(r["layout_json"]) if r else {}

    def set(self, job_id: int, layout: dict[str, Any]) -> None:
        with self._db.connect() as conn:
            conn.execute(
                "INSERT INTO layouts (job_id, layout_json, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(job_id) DO UPDATE SET"
                " layout_json=excluded.layout_json, updated_at=excluded.updated_at",
                (job_id, _json(layout), time.time()))


class ChatRepository(Repository):
    """The corrections chat, oldest turn first."""

    def for_job(self, job_id: int) -> list[ChatMessage]:
        with self._db.connect() as conn:
            rows = conn.execute("SELECT * FROM chat WHERE job_id=? ORDER BY id",
                                (job_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d.pop("meta_json") or "{}")
            out.append(ChatMessage.model_validate(d))
        return out

    def add(self, job_id: int, role: ChatRole, text: str, meta: dict[str, Any]) -> int:
        with self._db.connect() as conn:
            cur = conn.execute("INSERT INTO chat (job_id, role, text, meta_json, created_at)"
                               " VALUES (?,?,?,?,?)", (job_id, role, text, _json(meta),
                                                        time.time()))
            return int(cur.lastrowid or 0)

    def set_meta(self, message_id: int, meta: dict[str, Any]) -> None:
        with self._db.connect() as conn:
            conn.execute("UPDATE chat SET meta_json=? WHERE id=?", (_json(meta), message_id))


class SettingsRepository(Repository):
    """Key-value settings: the chosen backend and models, prompt edits, the filename prefix."""

    def get(self, key: str, default: str = "") -> str:
        with self._db.connect() as conn:
            r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r["value"] if r else default

    def set(self, key: str, value: str) -> None:
        with self._db.connect() as conn:
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def delete(self, key: str) -> None:
        with self._db.connect() as conn:
            conn.execute("DELETE FROM settings WHERE key=?", (key,))

    def with_prefix(self, prefix: str) -> dict[str, str]:
        """{rest_of_key: value} for every setting whose key starts with `prefix`."""
        with self._db.connect() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"][len(prefix):]: r["value"] for r in rows
                if r["key"].startswith(prefix)}
