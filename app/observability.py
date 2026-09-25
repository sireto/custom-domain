"""Metrics and log hygiene.

Metrics are Prometheus counters, histograms and gauges exposed on
``/internal/metrics``. Logging never carries credentials or tokens: the
``RedactSecrets`` filter masks anything that looks like one in log messages
as a last line of defence; the code paths avoid logging them in the first
place.
"""

from __future__ import annotations

import logging
import re

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

registry = CollectorRegistry()

dns_check_seconds = Histogram(
    "custom_domain_dns_check_seconds",
    "Wall time of one domain's DNS checks (ownership and routing)",
    registry=registry,
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)
checks_total = Counter(
    "custom_domain_checks_total",
    "Check outcomes recorded by the worker",
    ["check", "status", "error_code"],
    registry=registry,
)
status_transitions_total = Counter(
    "custom_domain_status_transitions_total",
    "Domain status transitions",
    ["to", "reason"],
    registry=registry,
)
tls_ask_total = Counter(
    "custom_domain_tls_ask_total",
    "On-demand certificate authorization decisions",
    ["decision"],
    registry=registry,
)
edge_assert_total = Counter(
    "custom_domain_edge_assert_total",
    "Routing assertion decisions at the edge",
    ["decision"],
    registry=registry,
)
webhook_deliveries_total = Counter(
    "custom_domain_webhook_deliveries_total",
    "Webhook delivery attempts by outcome",
    ["outcome"],
    registry=registry,
)
reconcile_total = Counter(
    "custom_domain_reconcile_total",
    "Edge reconciliation runs by outcome",
    ["outcome"],
    registry=registry,
)
domains_by_status = Gauge(
    "custom_domain_domains",
    "Live domains by status (set at scrape time)",
    ["status"],
    registry=registry,
)
registrations_total = Counter(
    "custom_domain_registrations_total",
    "Domain registrations by outcome",
    ["outcome"],
    registry=registry,
)


def render_metrics(session) -> bytes:
    """Refresh gauges from the database and render the exposition text."""
    from sqlalchemy import func, select

    from app.models import Domain, DomainStatus

    rows = session.execute(
        select(Domain.status, func.count())
        .where(Domain.deleted_at.is_(None))
        .group_by(Domain.status)
    ).all()
    counts = {status: 0 for status in DomainStatus}
    for status, count in rows:
        counts[status] = count
    for status, count in counts.items():
        domains_by_status.labels(status=status.value).set(count)
    return generate_latest(registry)


_SECRET_PATTERNS = [
    re.compile(r"\bcd_[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bwhsec_[A-Za-z0-9_-]{8,}"),
    re.compile(r"custom-domain-verify=[A-Za-z0-9_-]+"),
    re.compile(r"\bv1\.[^.\s]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
]
_KEY_VALUE = re.compile(
    r"(?i)(password|secret|token|authorization)(=|:\s*)((?:bearer\s+)?[^\s,;\"']+)"
)


def _mask(match: re.Match) -> str:
    value = match.group(0)
    prefix, sep, _ = value.partition("_")
    return f"{prefix}{sep}***" if sep and len(prefix) <= 6 else "***"


def redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_mask, text)
    return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


_original_factory = logging.getLogRecordFactory()


def _redacting_factory(*args, **kwargs) -> logging.LogRecord:
    """Masks credential-looking values in every log record, whatever logger emits it."""
    record = _original_factory(*args, **kwargs)
    try:
        message = record.getMessage()
    except Exception:
        return record
    redacted = redact(message)
    if redacted != message:
        record.msg = redacted
        record.args = ()
    return record


def install_log_redaction() -> None:
    if logging.getLogRecordFactory() is not _redacting_factory:
        logging.setLogRecordFactory(_redacting_factory)
