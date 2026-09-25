"""Programmatic Alembic entry points used by the CLI, the entrypoint and tests."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

import app

MIGRATIONS_DIR = Path(app.__file__).resolve().parent / "db" / "migrations"
INI_PATH = MIGRATIONS_DIR.parent.parent.parent / "alembic.ini"


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(INI_PATH)) if INI_PATH.exists() else Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url:
        # ConfigParser treats % as interpolation syntax.
        cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def upgrade(database_url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade(database_url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(database_url), revision)
