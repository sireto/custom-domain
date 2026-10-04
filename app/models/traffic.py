from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime


class ApplicationTraffic(Base):
    """One application's proxied requests and response bytes on one UTC day.

    Counted at the edge across all of the application's hostnames, including
    requests the edge refused (429 over a request limit, 403 without an
    assertion). Kept for ``TRAFFIC_RETENTION_DAYS``; purging an application
    removes its rows.
    """

    __tablename__ = "application_traffic"

    application_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class EdgeTrafficCounter(Base):
    """The edge's running totals for one hostname when they were last read.

    Caddy's counters restart at zero on every configuration change and every
    restart; the next reading is counted from these baselines
    (``app/services/traffic.py``).
    """

    __tablename__ = "edge_traffic_counters"

    hostname: Mapped[str] = mapped_column(String(253), primary_key=True)
    requests: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    response_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    read_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
