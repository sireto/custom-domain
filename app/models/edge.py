from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime


class EdgeLock(Base):
    """Single-row mutex for edge reconciliation.

    Updating the row takes a row lock on PostgreSQL and the write lock on
    SQLite until the transaction ends, which serializes reconciliation across
    API instances that share a database. ``holder`` and ``locked_at`` are
    diagnostics only.
    """

    __tablename__ = "edge_locks"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    holder: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
