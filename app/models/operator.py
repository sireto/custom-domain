"""The operator API token, when it was set through the API."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, utcnow


class OperatorToken(Base):
    """At most one row, ``active``: the SHA-256 of the token set through
    ``PUT /operator/v1/token``. While it exists it replaces
    ``OPERATOR_API_TOKEN``; ``custom-domain operator reset-token`` removes it.
    """

    __tablename__ = "operator_tokens"

    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    set_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
