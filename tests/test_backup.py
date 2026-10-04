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
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
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
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    monkeypatch.setattr("app.db.session.get_database_url", lambda: PG_URL)
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        response = client.get("/operator/v1/backup", headers=AUTH)
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "backup_unavailable" and "password authentication" in error["message"]


def test_sqlite_answers_409(session_factory, session, monkeypatch):
    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    monkeypatch.setattr("app.db.session.get_database_url", lambda: "sqlite:///x.db")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        response = client.get("/operator/v1/backup", headers=AUTH)
    assert response.status_code == 409 and response.json()["error"]["code"] == "backup_unavailable"
