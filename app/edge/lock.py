"""Cross-instance serialization of reconciliation.

Every reconciler run updates the ``reconcile`` row of ``edge_locks`` as the
first statement of its transaction and keeps the transaction open until the
Caddy write has finished. Another instance's run blocks on that row until
then, so it always builds its snapshot from state at least as new as the
previous run's, and a snapshot built before a deletion committed can never be
applied after a newer snapshot.
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import EdgeLock
from app.models.types import utcnow

RECONCILE_LOCK = "reconcile"
# Serializes registrations across applications while MAX_DOMAINS is set, so
# the deployment-wide count and the insert cannot interleave.
DOMAIN_LIMIT_LOCK = "domain-limit"


def acquire_lock(session: Session, name: str, holder: str) -> None:
    """Take the ``edge_locks`` row ``name`` for the rest of the transaction.

    Blocks while another transaction holds it. The row is created on first
    use if it does not exist.
    """
    stmt = (
        update(EdgeLock)
        .where(EdgeLock.name == name)
        .values(holder=holder[:128], locked_at=utcnow())
    )
    if session.execute(stmt).rowcount:
        return
    try:
        with session.begin_nested():
            session.add(EdgeLock(name=name))
            session.flush()
    except IntegrityError:
        pass
    session.execute(stmt)


def acquire_reconcile_lock(session: Session, holder: str) -> None:
    """Take the reconcile lock for the rest of ``session``'s transaction.

    Blocks while another transaction holds it. Must be the first statement
    of the transaction so SQLite does not carry a stale read snapshot into
    the write. The seed row is created by the migration and recreated if it
    was removed.
    """
    acquire_lock(session, RECONCILE_LOCK, holder)
