"""A database backup over the operator API (``GET /operator/v1/backup``).

The response is ``pg_dump --format=custom`` of the deployment's database:
restore it with ``pg_restore`` as described in docs/operations.md. It holds
everything the database does, claim tokens, credential hashes and webhook
signing secrets included, so treat it like a secrets file. The certificate
store is not included; certificates are re-issued after a restore.

The endpoint is off unless ``OPERATOR_BACKUP=true``: without it, the
operator token can manage a deployment but not read its secrets (webhook
signing secrets above all, which would let the holder forge webhooks to
every application). Only PostgreSQL deployments can be backed up this way;
for SQLite, copy the database file. The password reaches pg_dump through its
environment, never its command line. One backup runs at a time per API
instance; a second gets 429.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator

from sqlalchemy.engine import make_url

from app.services.errors import RateLimited, ServiceError

CHUNK = 64 * 1024
audit = logging.getLogger("app.operator.audit")
_running = threading.Lock()


class BackupUnavailable(ServiceError):
    code = "backup_unavailable"


def enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return env.get("OPERATOR_BACKUP", "").strip().lower() in ("1", "true", "yes", "on")


def pg_dump_command(database_url: str) -> tuple[list[str], dict[str, str]]:
    """The pg_dump arguments and environment for ``database_url``."""
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise BackupUnavailable(
            "Backups over the API need PostgreSQL; with SQLite, copy the database file"
        )
    argv = ["pg_dump", "--format=custom", "--no-owner", "--no-privileges"]
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TZ")}
    if url.host:
        argv += ["--host", url.host]
    if url.port:
        argv += ["--port", str(url.port)]
    if url.username:
        argv += ["--username", url.username]
    if url.password:
        env["PGPASSWORD"] = url.password
    sslmode = url.query.get("sslmode")
    if isinstance(sslmode, str):
        env["PGSSLMODE"] = sslmode
    argv += ["--dbname", url.database or ""]
    return argv, env


def stream(database_url: str) -> Iterator[bytes]:
    """Start pg_dump and return its output as chunks.

    Failures before the first byte (no pg_dump, refused login) raise
    ``BackupUnavailable``, so the caller can still answer with an error. A
    failure later truncates the stream; pg_restore refuses a truncated dump.
    The outcome and size are written to the audit log when the stream ends.
    """
    argv, env = pg_dump_command(database_url)
    if shutil.which("pg_dump", path=env.get("PATH")) is None:
        raise BackupUnavailable("pg_dump is not installed in this image")
    if not _running.acquire(blocking=False):
        raise RateLimited(
            "A backup is already running; try again when it has finished", retry_after=60
        )
    # stderr goes to a file, never a pipe nobody reads while stdout streams:
    # a burst of warnings would fill the pipe and stall pg_dump.
    errors = tempfile.TemporaryFile()  # noqa: SIM115 - closed when the stream ends
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed program, arguments from our own URL
            argv, stdout=subprocess.PIPE, stderr=errors, env=env
        )
        first = process.stdout.read(CHUNK)
        if not first:
            process.wait()
            raise BackupUnavailable(f"pg_dump failed: {_tail(errors) or process.returncode}")
    except BaseException:
        errors.close()
        _running.release()
        raise

    def chunks() -> Iterator[bytes]:
        sent, outcome = 0, "failed"
        try:
            yield first
            sent = len(first)
            while chunk := process.stdout.read(CHUNK):
                yield chunk
                sent += len(chunk)
            if process.wait() != 0:
                raise RuntimeError(f"pg_dump exited with {process.returncode}: {_tail(errors)}")
            outcome = "complete"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
                outcome = "interrupted" if outcome == "failed" else outcome
            audit.info("backup %s bytes=%s", outcome, sent)
            errors.close()
            _running.release()

    return chunks()


def _tail(errors) -> str:
    errors.seek(0)
    return errors.read()[-600:].decode(errors="replace").strip()
