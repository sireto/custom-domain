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


def acquire_reconcile_lock(session: Session, holder: str) -> None:
    """Take the reconcile lock for the rest of ``session``'s transaction.

    Blocks while another transaction holds it. Must be the first statement
    of the transaction so SQLite does not carry a stale read snapshot into
    the write.
    """
    stmt = (
        update(EdgeLock)
        .where(EdgeLock.name == RECONCILE_LOCK)
        .values(holder=holder[:128], locked_at=utcnow())
    )
    if session.execute(stmt).rowcount:
        return
    # The seed row is created by the migration; recreate it if it was removed.
    try:
        with session.begin_nested():
            session.add(EdgeLock(name=RECONCILE_LOCK))
            session.flush()
    except IntegrityError:
        pass
    session.execute(stmt)
