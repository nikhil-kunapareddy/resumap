"""The SQLite schema, and the migrations an existing database needs.

Documents (a parsed resume, a job's points, a rewrite's meta) are stored as
JSON blobs; the tables are just enough relational structure to hang the
many-to-many mappings off.
"""
from __future__ import annotations

import sqlite3

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


# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS leaves
# an existing table alone, so a DB from before the column needs it added here.
ADDED_COLUMNS = {
    "outreach": {"linkedin_subject": "TEXT NOT NULL DEFAULT ''"},
}


def migrate(conn: sqlite3.Connection) -> None:
    """Create what is missing and add columns to tables that predate them."""
    conn.executescript(SCHEMA)
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in cols.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
