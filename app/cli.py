"""Operator command line: migrations, applications, credentials, origins, imports.

Run with ``uv run custom-domain --help``.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import timedelta
from pathlib import Path

from dotenv import load_dotenv

from app.db import migrate
from app.db.session import get_session_factory
from app.legacy import import_legacy_domains, parse_legacy_config
from app.models.types import utcnow
from app.services import applications as app_service
from app.services import domains as domain_service
from app.services import idempotency
from app.services.errors import ServiceError


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    try:
        return args.func(args) or 0
    except ServiceError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="custom-domain")
    sub = parser.add_subparsers(dest="command")

    db = sub.add_parser("db", help="database migrations").add_subparsers(dest="db_command")
    up = db.add_parser("upgrade", help="apply migrations")
    up.add_argument("--revision", default="head")
    up.set_defaults(func=lambda a: migrate.upgrade(revision=a.revision))
    down = db.add_parser("downgrade", help="revert migrations")
    down.add_argument("--revision", default="-1")
    down.set_defaults(func=lambda a: migrate.downgrade(revision=a.revision))
    libpq = db.add_parser("libpq-url", help="print DATABASE_URL in the form pg_dump accepts")
    libpq.set_defaults(func=_db_libpq_url)

    application = sub.add_parser("application", help="manage applications").add_subparsers(
        dest="application_command"
    )
    create = application.add_parser("create")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--cname-target", required=True, help="hostname customers CNAME to")
    create.set_defaults(func=_application_create)
    application.add_parser("list").set_defaults(func=_application_list)
    probe_flag = application.add_parser(
        "set-workspace-probe",
        help="require (or not) the origin's workspace echo for readiness",
    )
    probe_flag.add_argument("--application", required=True)
    group = probe_flag.add_mutually_exclusive_group(required=True)
    group.add_argument("--enabled", action="store_true")
    group.add_argument("--disabled", action="store_true")
    probe_flag.set_defaults(func=_application_set_workspace_probe)

    credential = sub.add_parser("credential", help="manage API credentials").add_subparsers(
        dest="credential_command"
    )
    issue = credential.add_parser("issue")
    issue.add_argument("--application", required=True, help="application slug")
    issue.add_argument("--label", required=True)
    issue.add_argument("--expires-in-days", type=int)
    issue.set_defaults(func=_credential_issue)
    revoke = credential.add_parser("revoke")
    revoke.add_argument("--application", required=True)
    revoke.add_argument("--id", required=True)
    revoke.set_defaults(func=_credential_revoke)
    listing = credential.add_parser("list")
    listing.add_argument("--application", required=True)
    listing.set_defaults(func=_credential_list)
    rotate = credential.add_parser(
        "rotate", help="issue a replacement and expire the old credential after a grace period"
    )
    rotate.add_argument("--application", required=True)
    rotate.add_argument("--id", required=True, help="credential to replace")
    rotate.add_argument("--label", help="label for the new credential (default: same as old)")
    rotate.add_argument("--grace-hours", type=float, default=24.0)
    rotate.set_defaults(func=_credential_rotate)

    origin = sub.add_parser("origin", help="manage application origins").add_subparsers(
        dest="origin_command"
    )
    register = origin.add_parser("register")
    register.add_argument("--application", required=True)
    register.add_argument("--host", required=True)
    register.add_argument("--scheme", default="https")
    register.add_argument("--port", type=int)
    register.set_defaults(func=_origin_register)
    origin_list = origin.add_parser("list")
    origin_list.add_argument("--application", required=True)
    origin_list.set_defaults(func=_origin_list)
    verify = origin.add_parser(
        "verify", help="prove control of the origin by fetching its verification token"
    )
    verify.add_argument("--application", required=True)
    verify.add_argument("--id", help="origin id (or use --host)")
    verify.add_argument("--host", help="origin host")
    verify.add_argument("--activate", action="store_true", help="activate when verified")
    verify.add_argument(
        "--allow-private",
        action="store_true",
        help="allow non-public origin addresses (trusted self-hosted deployments only; "
        "ORIGIN_ALLOW_PRIVATE=true has the same effect)",
    )
    verify.add_argument("--timeout", type=float, default=5.0)
    verify.set_defaults(func=_origin_verify)
    activate = origin.add_parser("activate", help="route the application's traffic to this origin")
    activate.add_argument("--application", required=True)
    activate.add_argument("--id")
    activate.add_argument("--host")
    activate.set_defaults(func=_origin_activate)

    legacy = sub.add_parser("legacy", help="import from the volume-based deployment")
    legacy_sub = legacy.add_subparsers(dest="legacy_command")
    imp = legacy_sub.add_parser("import")
    imp.add_argument("--application", required=True)
    imp.add_argument("--file", default="domains/caddy.json")
    imp.add_argument("--port", type=int, default=443)
    imp.add_argument("--reference-map", help="JSON file mapping hostname to workspace reference")
    imp.add_argument(
        "--hostname-as-reference",
        action="store_true",
        help="use the hostname as the workspace reference when the map has no entry "
        "(only for applications that resolve workspaces by hostname)",
    )
    imp.add_argument(
        "--grandfather",
        action="store_true",
        help="treat existing hostnames as ownership-verified by import",
    )
    imp.add_argument(
        "--allow-skipped",
        action="store_true",
        help="commit a partial import even if some hostnames were skipped",
    )
    imp.add_argument("--dry-run", action="store_true", help="report without writing")
    imp.set_defaults(func=_legacy_import)

    domain = sub.add_parser("domain", help="domain maintenance").add_subparsers(
        dest="domain_command"
    )
    domain.add_parser("purge-tombstones").set_defaults(func=_domain_purge)

    edge = sub.add_parser("edge", help="Caddy configuration derived from the database")
    edge_sub = edge.add_subparsers(dest="edge_command")
    show = edge_sub.add_parser("config", help="print the desired Caddy config (secrets masked)")
    show.set_defaults(func=_edge_config)
    reconcile = edge_sub.add_parser("reconcile", help="apply the desired config to Caddy")
    reconcile.add_argument(
        "--dry-run", action="store_true", help="report what would change without contacting Caddy"
    )
    reconcile.set_defaults(func=_edge_reconcile)
    bootstrap = edge_sub.add_parser(
        "bootstrap",
        help="write the Caddy bootstrap config (admin listener, storage, empty server)",
    )
    bootstrap.add_argument(
        "--output", default="-", help="file path (created 0600), or - for stdout"
    )
    bootstrap.set_defaults(func=_edge_bootstrap)

    checks = sub.add_parser("checks", help="lifecycle checks").add_subparsers(dest="checks_command")
    run_checks = checks.add_parser("run", help="run all due DNS checks once")
    run_checks.add_argument("--limit", type=int, default=50)
    run_checks.set_defaults(func=_checks_run)
    dns_check = checks.add_parser("dns", help="run the DNS checks for one domain now")
    dns_check.add_argument("--application", required=True)
    dns_check.add_argument("--hostname", required=True)
    dns_check.set_defaults(func=_checks_dns)
    edge_check = checks.add_parser(
        "edge", help="run the certificate and origin checks for one domain now"
    )
    edge_check.add_argument("--application", required=True)
    edge_check.add_argument("--hostname", required=True)
    edge_check.set_defaults(func=_checks_edge)

    worker = sub.add_parser(
        "worker", help="run the lifecycle checks and edge reconciler outside the API process"
    )
    worker_sub = worker.add_subparsers(dest="worker_command")
    worker_run = worker_sub.add_parser("run")
    worker_run.add_argument("--once", action="store_true", help="one pass, then exit")
    worker_run.add_argument("--interval", type=float, help="seconds between passes")
    worker_run.set_defaults(func=_worker_run)

    openapi = sub.add_parser("openapi", help="API contract").add_subparsers(dest="openapi_command")
    export = openapi.add_parser("export", help="write the OpenAPI document as JSON")
    export.add_argument("--output", default="-", help="file path, or - for stdout")
    export.set_defaults(func=_openapi_export)

    return parser


def _application_create(args) -> int:
    with get_session_factory()() as session:
        application = app_service.create_application(
            session, slug=args.slug, name=args.name, cname_target=args.cname_target
        )
        session.commit()
        print(f"created application {application.slug} ({application.id})")
    return 0


def _application_list(args) -> int:
    with get_session_factory()() as session:
        for application in app_service.list_applications(session):
            print(
                f"{application.slug}\t{application.status.value}\t"
                f"{application.cname_target}\t{application.id}"
            )
    return 0


def _application_set_workspace_probe(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        application.workspace_probe_enabled = bool(args.enabled)
        session.commit()
        state = "required" if application.workspace_probe_enabled else "not required"
        print(f"workspace probe {state} for {application.slug}")
    return 0


def _worker_run(args) -> int:
    import threading

    from app.dns.settings import DnsSettings
    from app.dns.worker import ChecksWorker
    from app.edge.caddy_client import CaddyClient
    from app.edge.reconcile import Reconciler
    from app.services.edge_checks import SystemEdgeProber

    settings = _edge_settings()
    if settings is None:
        return 2
    dns_settings = DnsSettings.from_env()
    reconciler = None
    if settings.reconcile_enabled:
        reconciler = Reconciler(get_session_factory(), CaddyClient(settings.admin_url), settings)
    worker = ChecksWorker(
        get_session_factory(),
        _resolver(),
        prober=SystemEdgeProber(settings),
        settings=settings,
        batch_size=dns_settings.batch_size,
        on_status_change=reconciler.run_once if reconciler else None,
    )
    if args.once:
        result = worker.run_once()
        line = f"checks: {result.processed} processed, {result.failed} failed, "
        line += f"{result.status_changes} status change(s)"
        if reconciler is not None:
            edge = reconciler.run_once()
            line += f"; edge: {'applied' if edge.changed else edge.error or 'unchanged'}"
        print(line)
        return 0 if result.failed == 0 else 3
    interval = args.interval or dns_settings.worker_interval
    stop = threading.Event()
    threads = [threading.Thread(target=worker.run_forever, args=(stop, interval), daemon=True)]
    if reconciler is not None:
        threads.append(
            threading.Thread(
                target=reconciler.run_forever, args=(stop, settings.reconcile_interval), daemon=True
            )
        )
    for thread in threads:
        thread.start()
    print(f"worker running every {interval:g}s (Ctrl-C to stop)")
    try:
        while True:
            stop.wait(3600)
    except KeyboardInterrupt:
        stop.set()
    return 0


def _credential_issue(args) -> int:
    expires_at = None
    if args.expires_in_days:
        expires_at = utcnow() + timedelta(days=args.expires_in_days)
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        credential, secret = app_service.issue_credential(
            session, application, label=args.label, expires_at=expires_at
        )
        session.commit()
        print(f"credential id: {credential.id}")
        print(f"prefix:        {credential.key_prefix}")
        print("secret (shown once, store it now):")
        print(secret)
    return 0


def _credential_revoke(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        credential = app_service.revoke_credential(session, application, uuid.UUID(args.id))
        session.commit()
        print(f"revoked {credential.key_prefix} ({credential.id})")
    return 0


def _credential_list(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        for credential in app_service.list_credentials(session, application):
            state = "revoked" if credential.revoked_at else "active"
            print(f"{credential.key_prefix}\t{state}\t{credential.label}\t{credential.id}")
    return 0


def _credential_rotate(args) -> int:
    from datetime import timedelta

    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        new, secret, old = app_service.rotate_credential(
            session,
            application,
            uuid.UUID(args.id),
            label=args.label,
            grace=timedelta(hours=args.grace_hours),
        )
        session.commit()
        print(f"new credential id: {new.id}")
        print(f"prefix:            {new.key_prefix}")
        print("secret (shown once, store it now):")
        print(secret)
        print(f"old credential {old.key_prefix} expires at {old.expires_at.isoformat()}")
    return 0


def _origin_list(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        for origin in app_service.list_origins(session, application):
            state = "active" if origin.is_active else "inactive"
            error = f"\t{origin.last_error_code}" if origin.last_error_code else ""
            print(f"{origin.url}\t{origin.status.value}\t{state}\t{origin.id}{error}")
    return 0


def _origin_verify(args) -> int:
    from app.services.origin_verification import (
        WELL_KNOWN_PATH,
        OriginVerificationFailed,
        allow_private_from_env,
        verify_origin,
    )

    allow_private = args.allow_private or allow_private_from_env()
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        origin = app_service.get_origin(
            session,
            application,
            origin_id=uuid.UUID(args.id) if args.id else None,
            host=args.host,
        )
        try:
            verify_origin(session, origin, allow_private=allow_private, timeout=args.timeout)
        except OriginVerificationFailed as exc:
            session.commit()  # keep the diagnostic on the origin row
            print(f"verification failed [{exc.code}]: {exc.message}", file=sys.stderr)
            print(
                f"expected: GET {origin.scheme}://{origin.host}:{origin.port}{WELL_KNOWN_PATH} "
                f"-> 200 text/plain body {origin.verification_token}",
                file=sys.stderr,
            )
            return 3
        if args.activate:
            app_service.activate_origin(session, origin)
        session.commit()
        state = "verified and active" if args.activate else "verified"
        print(f"origin {origin.url} {state} ({origin.id})")
    return 0


def _origin_activate(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        origin = app_service.get_origin(
            session,
            application,
            origin_id=uuid.UUID(args.id) if args.id else None,
            host=args.host,
        )
        app_service.activate_origin(session, origin)
        session.commit()
        print(f"origin {origin.url} is now active for {application.slug}")
    return 0


def _origin_register(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        origin = app_service.register_origin(
            session, application, host=args.host, scheme=args.scheme, port=args.port
        )
        session.commit()
        print(f"registered origin {origin.url} ({origin.id}), status {origin.status.value}")
        print(f"verification token: {origin.verification_token}")
        print(
            "next: serve that token as the plain-text body of "
            f"{origin.scheme}://{origin.host}:{origin.port}"
            "/.well-known/custom-domain-origin-verification, then run "
            f"`custom-domain origin verify --application {application.slug} "
            f"--host {origin.host} --activate`"
        )
    return 0


EXIT_INCOMPLETE_IMPORT = 3


def _legacy_import(args) -> int:
    config = json.loads(Path(args.file).read_text())
    entries = parse_legacy_config(config, port=args.port)
    references = {}
    if args.reference_map:
        references = json.loads(Path(args.reference_map).read_text())
    elif not args.hostname_as_reference:
        print(
            "error: --reference-map is required unless --hostname-as-reference is given",
            file=sys.stderr,
        )
        return 2

    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        report = import_legacy_domains(
            session,
            application,
            entries,
            references=references,
            grandfather=args.grandfather,
            hostname_as_reference=args.hostname_as_reference,
        )
        verb = "would-import" if args.dry_run else "imported"
        for domain in report.imported:
            print(f"{verb}\t{domain.hostname}\t{domain.status.value}\t{domain.reference}")
        for domain in report.existing:
            print(f"existing\t{domain.hostname}\t{domain.status.value}\t{domain.reference}")
        for hostname, reason in report.skipped:
            print(f"skipped\t{hostname}\t{reason}", file=sys.stderr)

        summary = (
            f"{len(report.imported)} to import, {len(report.existing)} existing, "
            f"{len(report.skipped)} skipped"
        )
        if args.dry_run:
            session.rollback()
            print(f"dry run: {summary}; nothing written")
            return EXIT_INCOMPLETE_IMPORT if report.skipped else 0
        if report.skipped and not args.allow_skipped:
            session.rollback()
            print(
                f"error: {summary}; nothing written. Fix the skipped hostnames or "
                "pass --allow-skipped to commit a partial import.",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE_IMPORT
        session.commit()
        print(f"done: {summary}")
    return 0


def _domain_purge(args) -> int:
    with get_session_factory()() as session:
        count = domain_service.purge_tombstones(session)
        keys = idempotency.purge_expired(session)
        session.commit()
        print(f"purged {count} tombstone(s) and {keys} expired idempotency key(s)")
    return 0


def _edge_settings():
    from app.edge.settings import EdgeConfigurationError, EdgeSettings

    try:
        return EdgeSettings.from_env()
    except EdgeConfigurationError as exc:
        print(f"error [edge_configuration]: {exc}", file=sys.stderr)
        return None


def _db_libpq_url(args) -> int:
    from app.db.session import get_database_url

    url = get_database_url()
    if not url.startswith("postgresql"):
        print("error [database_url]: only PostgreSQL URLs have a libpq form", file=sys.stderr)
        return 2
    scheme, _, rest = url.partition("://")
    print(f"postgresql://{rest}")
    return 0


def _edge_bootstrap(args) -> int:
    import os

    from app.edge.config import build_bootstrap
    from app.edge.settings import redact

    settings = _edge_settings()
    if settings is None:
        return 2
    config = build_bootstrap(settings)
    if args.output == "-":
        print(json.dumps(redact(config), indent=2, sort_keys=True))
        return 0
    document = json.dumps(config, indent=2, sort_keys=True) + "\n"
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(document)
    os.chmod(args.output, 0o600)
    print(f"wrote {args.output}")
    return 0


def _edge_config(args) -> int:
    from app.edge.config import build_caddy_config
    from app.edge.settings import redact

    settings = _edge_settings()
    if settings is None:
        return 2
    with get_session_factory()() as session:
        config = build_caddy_config(session, settings)
    print(json.dumps(redact(config), indent=2, sort_keys=True))
    return 0


def _edge_reconcile(args) -> int:
    from app.edge.caddy_client import CaddyClient
    from app.edge.config import app_route_count, build_apps, config_digest, hostnames_in
    from app.edge.reconcile import Reconciler

    settings = _edge_settings()
    if settings is None:
        return 2
    if args.dry_run:
        with get_session_factory()() as session:
            apps = build_apps(session, settings)
        routes = app_route_count(apps)
        print(
            f"dry run: {routes} route(s), {len(hostnames_in({'apps': apps}))} hostname(s), "
            f"digest {config_digest(apps)}; Caddy not contacted"
        )
        return 0
    if settings.legacy_api_enabled:
        print(
            "error [edge_configuration]: refusing to apply while ENABLE_LEGACY_API is true; "
            "the legacy API owns the Caddy configuration",
            file=sys.stderr,
        )
        return 2
    reconciler = Reconciler(get_session_factory(), CaddyClient(settings.admin_url), settings)
    result = reconciler.run_once()
    if not result.ok:
        print(f"error [{result.error}]: {result.detail}", file=sys.stderr)
        return 3
    state = "applied" if result.changed else "unchanged"
    print(
        f"{state}: {result.routes} route(s), {result.hostnames} hostname(s), "
        f"digest {result.desired_digest}"
    )
    return 0


def _resolver():
    from app.dns.resolver import SystemResolver
    from app.dns.settings import DnsSettings

    settings = DnsSettings.from_env()
    return SystemResolver(settings.nameservers or None, timeout=settings.timeout)


def _edge_prober():
    from app.services.edge_checks import SystemEdgeProber

    settings = _edge_settings()
    if settings is None:
        raise SystemExit(2)
    return SystemEdgeProber(settings), settings


def _checks_run(args) -> int:
    from app.dns.worker import run_due_checks

    prober, settings = _edge_prober()
    result = run_due_checks(
        get_session_factory(), _resolver(), prober=prober, settings=settings, limit=args.limit
    )
    print(f"processed {result.processed} domain(s), {result.failed} failed")
    return 0 if result.failed == 0 else 3


def _checks_dns(args) -> int:
    from app.hostname import canonicalize
    from app.models import Domain
    from app.services.dns_checks import run_dns_checks
    from app.services.domains import _domain_query

    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        domain = session.scalar(
            _domain_query(include_deleted=False).where(
                Domain.application_id == application.id,
                Domain.hostname == canonicalize(args.hostname),
            )
        )
        if domain is None:
            print("error [domain_not_found]: no live domain with that hostname", file=sys.stderr)
            return 2
        ownership, routing = run_dns_checks(session, domain, _resolver())
        session.commit()
        for check in (ownership, routing):
            line = f"{check.check_type.value}\t{check.status.value}"
            if check.error_code:
                line += f"\t{check.error_code}\t{check.message}"
            print(line)
        print(f"status\t{domain.status.value}\tnext check {ownership.next_check_at.isoformat()}")
        failing = any(c.status.value == "failing" for c in (ownership, routing))
    return 3 if failing else 0


def _checks_edge(args) -> int:
    from app.hostname import canonicalize
    from app.models import Domain
    from app.services.domains import _domain_query
    from app.services.edge_checks import eligible_for_edge_checks, run_edge_checks

    prober, settings = _edge_prober()
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        domain = session.scalar(
            _domain_query(include_deleted=False).where(
                Domain.application_id == application.id,
                Domain.hostname == canonicalize(args.hostname),
            )
        )
        if domain is None:
            print("error [domain_not_found]: no live domain with that hostname", file=sys.stderr)
            return 2
        if not eligible_for_edge_checks(domain):
            print(
                f"error [not_eligible]: domain is {domain.status.value} and its claim is "
                f"{domain.active_claim.status.value if domain.active_claim else 'missing'}; "
                "ownership must be verified first",
                file=sys.stderr,
            )
            return 3
        certificate, origin = run_edge_checks(session, domain, prober, settings)
        session.commit()
        for check in (certificate, origin):
            line = f"{check.check_type.value}\t{check.status.value}"
            if check.error_code:
                line += f"\t{check.error_code}\t{check.message}"
            print(line)
        print(f"status\t{domain.status.value}")
        failing = any(c.status.value == "failing" for c in (certificate, origin))
    return 3 if failing else 0


def _openapi_export(args) -> int:
    from app.main import create_app

    document = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(document)
    else:
        Path(args.output).write_text(document)
        print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
