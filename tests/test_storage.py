"""storage/: connections, transactions, and each repository's guarantees."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app.domain import (BoxMode, ExperienceKind, Job, Resume, RewriteMode, RewriteStatus,
                        experience_id, experience_key)
from app.storage import Database, Store

DOC = {"name": "Jane Doe", "body_size": 10.0, "leading": 12.5, "diagnostics": {},
       "geometry": {"width": 612.0, "height": 792.0, "left": 54.0, "right": 54.0,
                    "top": 54.0, "bottom": 54.0},
       "blocks": [], "sections": []}


def _job(store: Store) -> Job:
    resume = store.resumes.add(label="r", filename="r.pdf", stored_path="/tmp/r.pdf",
                               person="Jane", doc=DOC)
    return store.jobs.add(resume_id=resume.id, company="Contoso", role="Data Engineer",
                          raw="raw", points=[{"id": "jd01", "text": "Operate Kafka."}],
                          mode=RewriteMode.AGGRESSIVE)


def _rewrite(store: Store, job_id: int, block_id: str = "bul005", text: str = "new") -> None:
    store.rewrites.save(job_id, block_id, original="old", rewritten=text,
                        status=RewriteStatus.PENDING, meta={})


# -- the database -----------------------------------------------------------

def test_init_is_idempotent(paths):
    db = Database(paths.db)
    db.init()
    db.init()
    tables = {r[0] for r in sqlite3.connect(db.path).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"resumes", "jobs", "mappings", "box_modes", "rewrites", "outreach", "layouts",
            "chat", "experiences", "outreach_examples", "settings"} <= tables


def test_added_columns_reach_a_table_that_already_exists(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE outreach (job_id INTEGER PRIMARY KEY, recruiter TEXT, "
                     "linkedin TEXT, email_subject TEXT, email_body TEXT, meta_json TEXT, "
                     "updated_at REAL)")
    Database(path).init()
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(outreach)")}
    assert "linkedin_subject" in cols


def test_every_call_closes_its_connection(store):
    with store.database.connect() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_foreign_keys_are_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.jobs.add(resume_id=999, company="", role="", raw="", points=[],
                       mode=RewriteMode.AGGRESSIVE)


# -- transactions -----------------------------------------------------------

def test_a_transaction_commits_every_write_together(store):
    job = _job(store)
    with store.transaction():
        _rewrite(store, job.id, "bul005")
        _rewrite(store, job.id, "bul006")
        # Reads inside the transaction see its own writes.
        assert set(store.rewrites.for_job(job.id)) == {"bul005", "bul006"}
    assert set(store.rewrites.for_job(job.id)) == {"bul005", "bul006"}


def test_a_failed_transaction_leaves_nothing_behind(store):
    job = _job(store)
    with pytest.raises(RuntimeError), store.transaction():
        _rewrite(store, job.id, "bul005")
        store.layouts.set(job.id, {"font": 0.9})
        raise RuntimeError("halfway")
    assert store.rewrites.for_job(job.id) == {}
    assert store.layouts.get(job.id) == {}


def test_a_nested_transaction_joins_the_outer_one(store):
    job = _job(store)
    with pytest.raises(RuntimeError), store.transaction():
        with store.transaction():
            _rewrite(store, job.id)
        raise RuntimeError("after the inner block finished")
    assert store.rewrites.for_job(job.id) == {}


def test_a_transaction_on_one_database_leaves_another_alone(store, tmp_path):
    other = Store(Database(tmp_path / "other.db"))
    other.init()
    with pytest.raises(RuntimeError), store.transaction():
        other.settings.set("k", "v")
        raise RuntimeError
    assert other.settings.get("k") == "v"


# -- repositories -----------------------------------------------------------

def test_add_returns_the_stored_entity(store):
    job = _job(store)
    assert isinstance(job, Job) and job.company == "Contoso" and job.points[0].id == "jd01"
    resume = store.resumes.get(job.resume_id)
    assert isinstance(resume, Resume) and resume.doc.name == "Jane Doe"
    exp = store.experiences.add(kind=ExperienceKind.WORK, title="", text="Ran on-call.")
    assert (exp.key, exp.kind) == (experience_key(exp.id), "work")


def test_deleting_a_job_cascades_to_everything_keyed_on_it(store):
    job = _job(store)
    store.mappings.map_box(job.id, "bul005", ["jd01"])
    _rewrite(store, job.id)
    store.layouts.set(job.id, {"font": 0.9})
    store.chat.add(job.id, "user", "hi", {})
    store.jobs.delete(job.id)
    assert (store.mappings.for_job(job.id), store.rewrites.for_job(job.id),
            store.layouts.get(job.id), store.chat.for_job(job.id)) == ({}, {}, {}, [])


def test_update_changes_only_the_fields_given(store):
    job = _job(store)
    store.jobs.update(job.id, role="Staff Engineer")
    got = store.jobs.get(job.id)
    assert (got.company, got.role, got.mode) == ("Contoso", "Staff Engineer", "aggressive")
    with pytest.raises(TypeError):
        store.jobs.update(job.id, raw_text="changed")   # not an updatable column


def test_map_box_replaces_a_boxs_points(store):
    job = _job(store)
    store.mappings.map_box(job.id, "bul005", ["jd01", "jd02", "jd01"])
    store.mappings.map_box(job.id, "bul006", ["jd01"])
    store.mappings.map_box(job.id, "bul005", ["jd03"])
    assert store.mappings.for_job(job.id) == {"bul005": ["jd03"], "bul006": ["jd01"]}


def test_box_modes_round_trip_and_inherit_clears(store):
    job = _job(store)
    store.mappings.set_box_mode(job.id, "bul005", BoxMode.REPLACE)
    assert store.mappings.box_modes(job.id) == {"bul005": BoxMode.REPLACE}
    store.mappings.set_box_mode(job.id, "bul005", BoxMode.INHERIT)
    assert store.mappings.box_modes(job.id) == {}


def test_saving_a_rewrite_twice_keeps_its_original(store):
    job = _job(store)
    store.rewrites.save(job.id, "bul005", original="first", rewritten="v1",
                        status=RewriteStatus.PENDING, meta={"keywords_used": ["a"]})
    store.rewrites.save(job.id, "bul005", original="second", rewritten="v2",
                        status=RewriteStatus.ACCEPTED, meta={"defensible": False})
    rw = store.rewrites.get(job.id, "bul005")
    assert (rw.original, rw.rewritten, rw.status) == ("first", "v2", "accepted")
    assert (rw.meta.keywords_used, rw.meta.defensible) == ([], False)


def test_a_model_is_written_back_exactly_as_it_was_read(store):
    """Defaults the model fills in on read must not leak into the stored JSON."""
    job = _job(store)
    store.rewrites.save(job.id, "bul005", original="o", rewritten="r",
                        status=RewriteStatus.PENDING,
                        meta={"keywords_used": ["Kafka"], "future_field": 1})
    rw = store.rewrites.get(job.id, "bul005")
    assert rw.meta.replaced is False            # a default, filled in on read
    store.rewrites.save(job.id, "bul005", original=rw.original, rewritten=rw.rewritten,
                        status=rw.status, meta=rw.meta)
    with sqlite3.connect(store.database.path) as conn:
        stored = conn.execute("SELECT meta_json FROM rewrites").fetchone()[0]
    assert json.loads(stored) == {"keywords_used": ["Kafka"], "future_field": 1}


def test_delete_key_removes_a_key_from_every_job(store):
    first, second = _job(store), _job(store)
    for job in (first, second):
        store.mappings.map_box(job.id, "exp001", ["jd01"])
    _rewrite(store, first.id, "exp001")
    assert store.mappings.delete_key("exp001") == 2
    assert store.rewrites.delete_key("exp001") == 1
    assert store.mappings.for_job(first.id) == store.mappings.for_job(second.id) == {}


def test_outreach_examples_are_newest_first(store):
    a = store.examples.add(kind="email", subject="", body="first")
    b = store.examples.add(kind="linkedin", subject="", body="second")
    assert [e.id for e in store.examples.all()] == [b.id, a.id]
    assert store.examples.delete(a.id) is True and store.examples.delete(a.id) is False


def test_experience_keys_never_collide_with_block_ids():
    assert experience_key(12) == "exp012"
    assert experience_id("exp012") == 12
    for key in ("bul012", "sec003", "exp", "expert", "exp01a"):
        assert experience_id(key) is None


def test_settings_by_prefix(store):
    store.settings.set("prompt.rewrite", "A")
    store.settings.set("prompt.mode.bridge", "B")
    store.settings.set("backend", "api")
    store.settings.set("prompt.rewrite", "C")
    assert store.settings.with_prefix("prompt.") == {"rewrite": "C", "mode.bridge": "B"}
    store.settings.delete("prompt.rewrite")
    assert store.settings.get("prompt.rewrite", "default") == "default"
