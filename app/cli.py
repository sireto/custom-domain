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

    application = sub.add_parser("application", help="manage applications").add_subparsers(
        dest="application_command"
    )
    create = application.add_parser("create")
    create.add_argument("--slug", required=True)
    create.add_argument("--name", required=True)
    create.add_argument("--cname-target", required=True, help="hostname customers CNAME to")
    create.set_defaults(func=_application_create)
    application.add_parser("list").set_defaults(func=_application_list)

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

    origin = sub.add_parser("origin", help="manage application origins").add_subparsers(
        dest="origin_command"
    )
    register = origin.add_parser("register")
    register.add_argument("--application", required=True)
    register.add_argument("--host", required=True)
    register.add_argument("--scheme", default="https")
    register.add_argument("--port", type=int)
    register.set_defaults(func=_origin_register)

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


def _origin_register(args) -> int:
    with get_session_factory()() as session:
        application = app_service.get_application_by_slug(session, args.application)
        origin = app_service.register_origin(
            session, application, host=args.host, scheme=args.scheme, port=args.port
        )
        session.commit()
        print(f"registered origin {origin.url} ({origin.id}), status {origin.status.value}")
        print(f"verification token: {origin.verification_token}")
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
        session.commit()
        print(f"purged {count} tombstone(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
