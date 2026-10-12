"""The Domain Connect signer set through the operator API."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.types import UTCDateTime, utcnow


class DomainConnectSigner(Base):
    """At most one row, ``active``: the signer set with
    ``PUT /operator/v1/domain-connect/signer``. While it exists it replaces the
    ``DOMAIN_CONNECT_SIGNER*`` settings and the template ids; deleting it
    (``custom-domain domain-connect reset``) returns to the settings.

    The credential is stored encrypted with a key derived from
    ``SERVICE_SECRET_KEY`` (app/services/secret_box.py), so a database dump
    alone does not reveal it. ``credential_prefix`` (its first 12
    characters) is not secret: the signing service uses it to tell which
    credential the deployment holds.
    """

    __tablename__ = "domain_connect_signers"

    name: Mapped[str] = mapped_column(String(16), primary_key=True)
    signer: Mapped[str] = mapped_column(String(16), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    credential_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    credential_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    provider_id: Mapped[str] = mapped_column(String(64), nullable=False)
    service_id: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
