"""SQLite persistence. Documents are stored as JSON blobs -- the schema here is
just enough relational structure to hang many-to-many mappings off."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS resumes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    filename    TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    person_name TEXT DEFAULT '',
    doc_json    TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    resume_id   INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
    company     TEXT DEFAULT '',
    role        TEXT DEFAULT '',
    raw_text    TEXT NOT NULL,
    points_json TEXT NOT NULL,
    mode        TEXT DEFAULT 'aggressive',
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mappings (
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    block_id    TEXT NOT NULL,
    point_id    TEXT NOT NULL,
    PRIMARY KEY (job_id, block_id, point_id)
);

-- Boxes the user wants written from scratch for their JD points instead of
-- realigned. Keyed by block id like mappings; no row means the job's mode.
CREATE TABLE IF NOT EXISTS box_modes (
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    block_id    TEXT NOT NULL,
    mode        TEXT NOT NULL,
    PRIMARY KEY (job_id, block_id)
);

CREATE TABLE IF NOT EXISTS rewrites (
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    block_id    TEXT NOT NULL,
    original    TEXT NOT NULL,
    rewritten   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    meta_json   TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL,
    PRIMARY KEY (job_id, block_id)
);

CREATE TABLE IF NOT EXISTS outreach (
    job_id        INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    recruiter     TEXT NOT NULL DEFAULT '',
    linkedin      TEXT NOT NULL DEFAULT '',
    linkedin_subject TEXT NOT NULL DEFAULT '',
    email_subject TEXT NOT NULL DEFAULT '',
    email_body    TEXT NOT NULL DEFAULT '',
    meta_json     TEXT NOT NULL DEFAULT '{}',
    updated_at    REAL NOT NULL
);

-- Page-wide adjustments asked for in the corrections chat; see
-- pdfgen.LAYOUT_DEFAULTS for the keys.
CREATE TABLE IF NOT EXISTS layouts (
    job_id      INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    layout_json TEXT NOT NULL DEFAULT '{}',
    updated_at  REAL NOT NULL
);

-- The corrections chat. An assistant turn's meta records what it changed and
-- the state it replaced, which is what makes it undoable.
CREATE TABLE IF NOT EXISTS chat (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    meta_json   TEXT NOT NULL DEFAULT '{}',
    created_at  REAL NOT NULL
);

-- Experiences that are not on the resume: stored once, offered for mapping on
-- every job. Mapped and rewritten under experience_key(id), which never
-- collides with a parsed block id.
CREATE TABLE IF NOT EXISTS experiences (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'project',
    title       TEXT NOT NULL DEFAULT '',
    text        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- Outreach messages the user liked, stored once and sent with every draft as
-- style references. kind is 'linkedin' or 'email'.
CREATE TABLE IF NOT EXISTS outreach_examples (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    subject     TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    created_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves
# an existing table alone, so a DB from before the column needs it added here.
ADDED_COLUMNS = {
    "outreach": {"linkedin_subject": "TEXT NOT NULL DEFAULT ''"},
}


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _row(r: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


# -- resumes ---------------------------------------------------------------

def add_resume(label: str, filename: str, stored_path: str, person: str, doc: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO resumes (label, filename, stored_path, person_name, doc_json, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (label, filename, stored_path, person, json.dumps(doc), time.time()),
        )
        return int(cur.lastrowid)


def list_resumes() -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, label, filename, person_name, created_at,"
            " (SELECT COUNT(*) FROM jobs WHERE jobs.resume_id = resumes.id) AS job_count"
            " FROM resumes ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_resume(resume_id: int) -> dict | None:
    with connect() as conn:
        r = _row(conn.execute("SELECT * FROM resumes WHERE id=?", (resume_id,)).fetchone())
    if r:
        r["doc"] = json.loads(r.pop("doc_json"))
    return r


def delete_resume(resume_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM resumes WHERE id=?", (resume_id,))


# -- jobs ------------------------------------------------------------------

def add_job(resume_id: int, company: str, role: str, raw: str, points: list[dict], mode: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (resume_id, company, role, raw_text, points_json, mode, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (resume_id, company, role, raw, json.dumps(points), mode, time.time()),
        )
        return int(cur.lastrowid)


def update_job(job_id: int, **fields: Any) -> None:
    allowed = {"company", "role", "mode", "points_json"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    clause = ", ".join(f"{k}=?" for k in sets)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {clause} WHERE id=?", (*sets.values(), job_id))


def set_points(job_id: int, points: list[dict]) -> None:
    """Replace a job's point list, keeping the JSON blob this module owns."""
    with connect() as conn:
        conn.execute("UPDATE jobs SET points_json=? WHERE id=?",
                     (json.dumps(points), job_id))


def get_job(job_id: int) -> dict | None:
    with connect() as conn:
        r = _row(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
    if r:
        r["points"] = json.loads(r.pop("points_json"))
    return r


def list_jobs(resume_id: int | None = None) -> list[dict]:
    q = ("SELECT id, resume_id, company, role, mode, created_at FROM jobs")
    args: tuple = ()
    if resume_id is not None:
        q += " WHERE resume_id=?"
        args = (resume_id,)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY created_at DESC", args).fetchall()]


def delete_job(job_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


# -- mappings --------------------------------------------------------------

def set_mappings(job_id: int, block_id: str, point_ids: list[str]) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM mappings WHERE job_id=? AND block_id=?", (job_id, block_id))
        conn.executemany(
            "INSERT OR IGNORE INTO mappings (job_id, block_id, point_id) VALUES (?,?,?)",
            [(job_id, block_id, p) for p in point_ids],
        )


def get_mappings(job_id: int) -> dict[str, list[str]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT block_id, point_id FROM mappings WHERE job_id=?", (job_id,)
        ).fetchall()
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(r["block_id"], []).append(r["point_id"])
    return out


def set_box_mode(job_id: int, block_id: str, mode: str) -> None:
    """Mark one box's rewrite mode; an empty `mode` puts it back on the job's."""
    with connect() as conn:
        conn.execute("DELETE FROM box_modes WHERE job_id=? AND block_id=?", (job_id, block_id))
        if mode:
            conn.execute("INSERT INTO box_modes (job_id, block_id, mode) VALUES (?,?,?)",
                         (job_id, block_id, mode))


def get_box_modes(job_id: int) -> dict[str, str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT block_id, mode FROM box_modes WHERE job_id=?", (job_id,)
        ).fetchall()
    return {r["block_id"]: r["mode"] for r in rows}


# -- rewrites --------------------------------------------------------------

def save_rewrite(job_id: int, block_id: str, original: str, rewritten: str,
                 status: str, meta: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO rewrites (job_id, block_id, original, rewritten, status, meta_json, updated_at)"
            " VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT(job_id, block_id) DO UPDATE SET"
            " rewritten=excluded.rewritten, status=excluded.status,"
            " meta_json=excluded.meta_json, updated_at=excluded.updated_at",
            (job_id, block_id, original, rewritten, status, json.dumps(meta), time.time()),
        )


def set_rewrite_status(job_id: int, block_id: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE rewrites SET status=?, updated_at=? WHERE job_id=? AND block_id=?",
            (status, time.time(), job_id, block_id),
        )


def delete_rewrite(job_id: int, block_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM rewrites WHERE job_id=? AND block_id=?",
                     (job_id, block_id))


def get_rewrites(job_id: int) -> dict[str, dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM rewrites WHERE job_id=?", (job_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out[d["block_id"]] = d
    return out


# -- outreach --------------------------------------------------------------

def save_outreach(job_id: int, recruiter: str, linkedin: str, subject: str,
                  body: str, meta: dict, linkedin_subject: str = "") -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO outreach (job_id, recruiter, linkedin, linkedin_subject,"
            " email_subject, email_body, meta_json, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(job_id) DO UPDATE SET"
            " recruiter=excluded.recruiter, linkedin=excluded.linkedin,"
            " linkedin_subject=excluded.linkedin_subject,"
            " email_subject=excluded.email_subject, email_body=excluded.email_body,"
            " meta_json=excluded.meta_json, updated_at=excluded.updated_at",
            (job_id, recruiter, linkedin, linkedin_subject, subject, body,
             json.dumps(meta), time.time()),
        )


def get_outreach(job_id: int) -> dict | None:
    with connect() as conn:
        r = _row(conn.execute("SELECT * FROM outreach WHERE job_id=?", (job_id,)).fetchone())
    if r:
        r["meta"] = json.loads(r.pop("meta_json") or "{}")
    return r


def add_outreach_example(kind: str, subject: str, body: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO outreach_examples (kind, subject, body, created_at) VALUES (?,?,?,?)",
            (kind, subject, body, time.time()),
        )
        return int(cur.lastrowid)


def list_outreach_examples() -> list[dict]:
    """Newest first, so a prompt that keeps only the first few keeps the latest."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM outreach_examples ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def delete_outreach_example(example_id: int) -> bool:
    with connect() as conn:
        return conn.execute("DELETE FROM outreach_examples WHERE id=?",
                            (example_id,)).rowcount > 0


# -- layout & corrections chat ---------------------------------------------

def get_layout(job_id: int) -> dict:
    with connect() as conn:
        r = conn.execute("SELECT layout_json FROM layouts WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(r["layout_json"]) if r else {}


def set_layout(job_id: int, layout: dict) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO layouts (job_id, layout_json, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(job_id) DO UPDATE SET"
            " layout_json=excluded.layout_json, updated_at=excluded.updated_at",
            (job_id, json.dumps(layout), time.time()),
        )


def add_chat(job_id: int, role: str, text: str, meta: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO chat (job_id, role, text, meta_json, created_at) VALUES (?,?,?,?,?)",
            (job_id, role, text, json.dumps(meta), time.time()),
        )
        return int(cur.lastrowid)


def get_chat(job_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM chat WHERE job_id=? ORDER BY id", (job_id,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        out.append(d)
    return out


def set_chat_meta(message_id: int, meta: dict) -> None:
    with connect() as conn:
        conn.execute("UPDATE chat SET meta_json=? WHERE id=?", (json.dumps(meta), message_id))


# -- experiences -----------------------------------------------------------

# Parsed block ids are a kind's first three letters plus a counter ("bul012"),
# and no block kind starts with "exp", so these keys share the mappings and
# rewrites tables with resume blocks without colliding.
EXP_PREFIX = "exp"


def experience_key(experience_id: int) -> str:
    return f"{EXP_PREFIX}{experience_id:03d}"


def experience_id(key: str) -> int | None:
    """The experience a mapping/rewrite key names, or None for a resume block."""
    if key.startswith(EXP_PREFIX) and key[len(EXP_PREFIX):].isdigit():
        return int(key[len(EXP_PREFIX):])
    return None


def add_experience(kind: str, title: str, text: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO experiences (kind, title, text, created_at) VALUES (?,?,?,?)",
            (kind, title, text, time.time()),
        )
        return int(cur.lastrowid)


def list_experiences() -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM experiences ORDER BY id").fetchall()
    return [{**dict(r), "key": experience_key(r["id"])} for r in rows]


def delete_experience(experience_id: int) -> dict[str, int]:
    """Delete an experience and unpick it everywhere it was mapped or written up.

    Its tailored bullets go too: otherwise the next export would still splice
    in a bullet written from something the user has taken back.
    """
    key = experience_key(experience_id)
    with connect() as conn:
        maps = conn.execute("DELETE FROM mappings WHERE block_id=?", (key,)).rowcount
        rws = conn.execute("DELETE FROM rewrites WHERE block_id=?", (key,)).rowcount
        conn.execute("DELETE FROM experiences WHERE id=?", (experience_id,))
    return {"mappings": maps, "rewrites": rws}


# -- settings --------------------------------------------------------------

def get_setting(key: str, default: str = "") -> str:
    with connect() as conn:
        r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def delete_setting(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM settings WHERE key=?", (key,))


def settings_with_prefix(prefix: str) -> dict[str, str]:
    """{rest_of_key: value} for every setting whose key starts with `prefix`."""
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"][len(prefix):]: r["value"] for r in rows if r["key"].startswith(prefix)}


def set_setting(key: str, value: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
