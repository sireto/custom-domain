"""A database backup over the operator API (``GET /operator/v1/backup``).

The response is ``pg_dump --format=custom`` of the deployment's database:
restore it with ``pg_restore`` as described in docs/operations.md. It holds
everything the database does, claim tokens, credential hashes and webhook
signing secrets included, so treat it like a secrets file. The certificate
store is not included; certificates are re-issued after a restore.

Only PostgreSQL deployments can be backed up this way; for SQLite, copy the
database file. The password reaches pg_dump through its environment, never
its command line.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator

from sqlalchemy.engine import make_url

from app.services.errors import ServiceError

CHUNK = 64 * 1024


class BackupUnavailable(ServiceError):
    code = "backup_unavailable"


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
    """
    argv, env = pg_dump_command(database_url)
    if shutil.which("pg_dump", path=env.get("PATH")) is None:
        raise BackupUnavailable("pg_dump is not installed in this image")
    process = subprocess.Popen(  # noqa: S603 - fixed program, arguments from our own URL
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    first = process.stdout.read(CHUNK)
    if not first:
        _, err = process.communicate()
        raise BackupUnavailable(
            f"pg_dump failed: {err.decode(errors='replace').strip()[:300] or process.returncode}"
        )

    def chunks() -> Iterator[bytes]:
        try:
            yield first
            while chunk := process.stdout.read(CHUNK):
                yield chunk
            if process.wait() != 0:
                raise RuntimeError(f"pg_dump exited with {process.returncode}")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

    return chunks()
