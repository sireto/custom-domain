"""GET /operator/v1/backup: pg_dump over the operator API."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.services import backup
from tests.test_operator_api import AUTH, make_app

PG_URL = "postgresql+psycopg://custom_domain:s3cret-pass@db:5432/custom_domain?sslmode=disable"


def test_the_command_keeps_the_password_out_of_the_arguments():
    argv, env = backup.pg_dump_command(PG_URL)
    assert argv == [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--host",
        "db",
        "--port",
        "5432",
        "--username",
        "custom_domain",
        "--dbname",
        "custom_domain",
    ]
    assert "s3cret-pass" not in " ".join(argv)
    assert env["PGPASSWORD"] == "s3cret-pass" and env["PGSSLMODE"] == "disable"
    # Nothing else from the API's environment (tokens, keys) reaches pg_dump.
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TZ", "PGPASSWORD", "PGSSLMODE"}


def test_sqlite_deployments_are_told_to_copy_the_file():
    with pytest.raises(backup.BackupUnavailable, match="SQLite"):
        backup.pg_dump_command("sqlite:///data/custom_domain.db")


def _fake_pg_dump(tmp_path, script: str):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    tool = bin_dir / "pg_dump"
    tool.write_text("#!/bin/sh\n" + script)
    tool.chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}"


def test_the_endpoint_streams_the_dump(session_factory, session, monkeypatch, tmp_path):
    record = tmp_path / "seen"
    path = _fake_pg_dump(
        tmp_path,
        f'echo "$*" > {record}; echo "PGPASSWORD=$PGPASSWORD" >> {record}\n'
        "printf 'PGDMP'; head -c 200000 /dev/zero\n",
    )
    monkeypatch.setenv("PATH", path)
    app = make_app(
        session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34", OPERATOR_BACKUP="true"
    )
    monkeypatch.setattr("app.db.session.get_database_url", lambda: PG_URL)
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        assert client.get("/operator/v1/backup").status_code == 401
        response = client.get("/operator/v1/backup", headers=AUTH)
    assert response.status_code == 200
    assert response.content.startswith(b"PGDMP") and len(response.content) == 200005
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="custom-domain-'
    )
    assert response.headers["cache-control"] == "no-store"
    seen = record.read_text()
    assert "--format=custom" in seen and "s3cret-pass" not in seen.splitlines()[0]
    assert "PGPASSWORD=s3cret-pass" in seen


def test_a_failing_pg_dump_is_an_error_not_an_empty_file(
    session_factory, session, monkeypatch, tmp_path
):
    path = _fake_pg_dump(tmp_path, 'echo "FATAL: password authentication failed" >&2; exit 1\n')
    monkeypatch.setenv("PATH", path)
    app = make_app(
        session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34", OPERATOR_BACKUP="true"
    )
    monkeypatch.setattr("app.db.session.get_database_url", lambda: PG_URL)
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        response = client.get("/operator/v1/backup", headers=AUTH)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "backup_unavailable" and "password authentication" in error["message"]


def test_sqlite_answers_409(session_factory, session, monkeypatch):
    app = make_app(
        session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34", OPERATOR_BACKUP="true"
    )
    monkeypatch.setattr("app.db.session.get_database_url", lambda: "sqlite:///x.db")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        response = client.get("/operator/v1/backup", headers=AUTH)
    assert response.status_code == 409 and response.json()["error"]["code"] == "backup_unavailable"


def test_it_is_off_unless_asked_for(session_factory, session, monkeypatch, tmp_path):
    """Off by default: the dump would hand the operator token every signing secret."""
    monkeypatch.setenv("PATH", _fake_pg_dump(tmp_path, "printf PGDMP\n"))
    monkeypatch.delenv("OPERATOR_BACKUP", raising=False)
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    monkeypatch.setattr("app.db.session.get_database_url", lambda: PG_URL)
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        assert client.get("/operator/v1/backup", headers=AUTH).status_code == 404
    assert backup.enabled({"OPERATOR_BACKUP": "true"}) and not backup.enabled({})


def test_a_burst_on_stderr_does_not_stall_the_dump(monkeypatch, tmp_path):
    # 300 KB of warnings first: a pipe nobody reads fills at about 64 KB.
    path = _fake_pg_dump(
        tmp_path,
        "head -c 300000 /dev/zero | tr '\\0' w >&2; printf PGDMP; head -c 1000 /dev/zero\n",
    )
    monkeypatch.setenv("PATH", path)
    data = b"".join(backup.stream(PG_URL))
    assert data.startswith(b"PGDMP") and len(data) == 1005


def test_one_backup_at_a_time_and_the_outcome_is_audited(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setenv("PATH", _fake_pg_dump(tmp_path, "printf PGDMP; head -c 200000 /dev/zero\n"))
    first = backup.stream(PG_URL)
    head = next(first)
    with pytest.raises(backup.RateLimited):
        backup.stream(PG_URL)
    with caplog.at_level(logging.INFO, logger="app.operator.audit"):
        rest = b"".join(first)
    assert len(head) + len(rest) == 200005
    assert "backup complete bytes=200005" in caplog.text
    # The lock is free again.
    b"".join(backup.stream(PG_URL))


def test_a_failure_mid_stream_is_audited(monkeypatch, tmp_path, caplog):
    import logging

    monkeypatch.setenv("PATH", _fake_pg_dump(tmp_path, "printf PGDMP; echo boom >&2; exit 3\n"))
    with (
        caplog.at_level(logging.INFO, logger="app.operator.audit"),
        pytest.raises(RuntimeError, match="boom"),
    ):
        b"".join(backup.stream(PG_URL))
    assert "backup failed bytes=5" in caplog.text
    b"".join(_ok(monkeypatch, tmp_path))  # the lock was released


def _ok(monkeypatch, tmp_path):
    other = tmp_path / "ok"
    other.mkdir()
    monkeypatch.setenv("PATH", _fake_pg_dump(other, "printf PGDMP\n"))
    return backup.stream(PG_URL)
