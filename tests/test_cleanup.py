"""What a delete leaves on disk: nothing that was the deleted thing's."""

from __future__ import annotations

from tests.conftest import upload


def test_deleting_a_job_deletes_its_preview(client, job, paths):
    assert client.get(f"/api/jobs/{job['id']}/preview.pdf").status_code == 200
    assert paths.preview(job["id"]).exists()

    assert client.delete(f"/api/jobs/{job['id']}").status_code == 200
    assert not paths.preview(job["id"]).exists()


def test_deleting_a_resume_deletes_its_upload_report_and_previews(client, resume, job, paths):
    client.get(f"/api/jobs/{job['id']}/preview.pdf")
    left = [paths.preview(job["id"]), paths.parse_report(resume["id"]), *paths.uploads.iterdir()]
    assert left and all(f.exists() for f in left)

    assert client.delete(f"/api/resumes/{resume['id']}").status_code == 200
    assert not any(f.exists() for f in left)


def test_two_uploads_of_one_name_keep_files_of_their_own(client, resume_files, paths):
    first = upload(client, resume_files["txt"])
    upload(client, resume_files["txt"])
    assert len(list(paths.uploads.iterdir())) == 2

    client.delete(f"/api/resumes/{first['id']}")
    assert len(list(paths.uploads.iterdir())) == 1
