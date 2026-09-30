"""db.py: the SQLite layer's own guarantees."""
from __future__ import annotations

import sqlite3

from app import db


def test_init_is_idempotent(storage):
    db.init()
    db.init()
    tables = {r[0] for r in sqlite3.connect(db.DB_PATH).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"resumes", "jobs", "mappings", "box_modes", "rewrites", "outreach", "layouts",
            "chat", "experiences", "outreach_examples", "settings"} <= tables


def test_added_columns_reach_a_table_that_already_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "old.db")
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("CREATE TABLE outreach (job_id INTEGER PRIMARY KEY, recruiter TEXT, "
                     "linkedin TEXT, email_subject TEXT, email_body TEXT, meta_json TEXT, "
                     "updated_at REAL)")
    db.init()
    cols = {r[1] for r in sqlite3.connect(db.DB_PATH).execute("PRAGMA table_info(outreach)")}
    assert "linkedin_subject" in cols


def _job(storage) -> int:
    rid = db.add_resume("r", "r.pdf", "/tmp/r.pdf", "Jane", {"blocks": []})
    return db.add_job(rid, "Contoso", "Data Engineer", "raw", [{"id": "jd01"}], "aggressive")


def test_deleting_a_job_cascades_to_everything_keyed_on_it(storage):
    jid = _job(storage)
    db.set_mappings(jid, "bul005", ["jd01"])
    db.save_rewrite(jid, "bul005", "old", "new", "pending", {})
    db.set_layout(jid, {"font": 0.9})
    db.add_chat(jid, "user", "hi", {})
    db.delete_job(jid)
    assert (db.get_mappings(jid), db.get_rewrites(jid), db.get_layout(jid),
            db.get_chat(jid)) == ({}, {}, {}, [])


def test_update_job_writes_only_known_columns(storage):
    jid = _job(storage)
    db.update_job(jid, role="Staff Engineer", resume_id=999, raw_text="changed")
    j = db.get_job(jid)
    assert (j["role"], j["raw_text"]) == ("Staff Engineer", "raw")
    assert j["resume_id"] != 999


def test_set_mappings_replaces_a_boxs_points(storage):
    jid = _job(storage)
    db.set_mappings(jid, "bul005", ["jd01", "jd02", "jd01"])
    db.set_mappings(jid, "bul006", ["jd01"])
    db.set_mappings(jid, "bul005", ["jd03"])
    assert db.get_mappings(jid) == {"bul005": ["jd03"], "bul006": ["jd01"]}


def test_saving_a_rewrite_twice_keeps_its_original(storage):
    jid = _job(storage)
    db.save_rewrite(jid, "bul005", "first original", "v1", "pending", {"a": 1})
    db.save_rewrite(jid, "bul005", "second original", "v2", "accepted", {"b": 2})
    rw = db.get_rewrites(jid)["bul005"]
    assert (rw["original"], rw["rewritten"], rw["status"], rw["meta"]) == (
        "first original", "v2", "accepted", {"b": 2})


def test_experience_keys_never_collide_with_block_ids():
    assert db.experience_key(12) == "exp012"
    assert db.experience_id("exp012") == 12
    for key in ("bul012", "sec003", "exp", "expert", "exp01a"):
        assert db.experience_id(key) is None


def test_settings_by_prefix(storage):
    db.set_setting("prompt.rewrite", "A")
    db.set_setting("prompt.mode.bridge", "B")
    db.set_setting("backend", "api")
    db.set_setting("prompt.rewrite", "C")
    assert db.settings_with_prefix("prompt.") == {"rewrite": "C", "mode.bridge": "B"}
    db.delete_setting("prompt.rewrite")
    assert db.get_setting("prompt.rewrite", "default") == "default"
