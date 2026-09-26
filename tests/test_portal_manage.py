"""The redesigned portal: deletes and edits, webhooks, DNS status and plain-language states."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app import cli
from app.models import CheckStatus, CheckType, OriginStatus
from app.models.types import utcnow
from app.portal import presenters
from app.services import applications as app_service
from app.services import domains as domain_service
from app.services.errors import (
    ApplicationNotEmpty,
    ConfirmationMismatch,
    CredentialInUse,
    OriginInUse,
)
from tests.test_portal import csrf_of, session_scope, sign_in


def _create(client, csrf, slug="acme", target="edge.example.net"):
    response = client.post(
        "/portal/applications",
        data={"csrf": csrf, "slug": slug, "name": slug.title(), "cname_target": target},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return response


def _domain(client, csrf, hostname, slug="acme", reference="ws_1"):
    response = client.post(
        f"/portal/applications/{slug}/domains",
        data={"csrf": csrf, "hostname": hostname, "reference": reference},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"].split("?")[0].rsplit("/", 1)[-1]


def test_every_page_renders_with_the_navigation(portal):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    _domain(client, csrf, "forms.customer.example")
    for path in (
        "/portal",
        "/portal/applications",
        "/portal/new-application",
        "/portal/applications/acme",
        "/portal/applications/acme/domains",
        "/portal/applications/acme/origins",
        "/portal/applications/acme/credentials",
        "/portal/applications/acme/webhooks",
        "/portal/applications/acme/settings",
        "/portal/edge",
        "/portal/edge?verify=1",
        "/portal/legacy",
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert "Edge &amp; DNS" in page.text and "Health checks" in page.text, path
    assert client.get("/portal/applications/acme/nope").status_code == 404


def test_application_overview_shows_setup_progress_and_target_dns(portal):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    page = client.get("/portal/applications/acme?ok=application_created").text
    assert "Application created" in page
    assert "1 of 6 done" in page  # the CNAME target resolves; nothing else is set up
    assert "edge.example.net resolves to 93.184.216.34" in page
    _create(client, csrf, "beta", "domains.beta.example")
    beta = client.get("/portal/applications/beta").text
    assert "0 of 6 done" in beta and "no A or AAAA record in public DNS yet" in beta


def test_notices_are_codes_never_reflected_text(portal):
    client = portal
    sign_in(client)
    page = client.get("/portal?ok=Your+account+is+locked,+call+us").text
    assert "Your account is locked" not in page
    assert "Old deleted-domain records purged" in client.get("/portal?ok=purged").text


def test_domain_page_shows_each_dns_record_with_what_dns_returns(portal, session):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    domain_id = _domain(client, csrf, "forms.customer.example")
    page = client.get(f"/portal/applications/acme/domains/{domain_id}").text
    assert "Waiting for DNS" in page and "Not checked yet" in page
    assert "_custom-domain-challenge.forms" in page  # the provider-relative name

    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        domain = domain_service.get_domain(s, acme, _uuid(domain_id))
        domain_service.record_check(
            s,
            domain,
            CheckType.OWNERSHIP,
            CheckStatus.FAILING,
            error_code="txt_token_mismatch",
            message="TXT records found, but none is this registration's value.",
            details={"observed": ["custom-domain-verify=old"]},
        )
        domain_service.record_check(
            s,
            domain,
            CheckType.ROUTING,
            CheckStatus.PASSING,
            details={"chain": ["edge.example.net"]},
        )
        s.commit()
    page = client.get(f"/portal/applications/acme/domains/{domain_id}").text
    assert "Wrong value" in page and "custom-domain-verify=old" in page
    assert "Found" in page
    listing = client.get("/portal/applications/acme/domains").text
    assert "TXT missing or wrong" in listing
    assert "Ownership check failing" in page  # the history, in words


def test_domains_can_be_searched_and_filtered(portal):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    _domain(client, csrf, "forms.alpha.example", reference="ws_alpha")
    _domain(client, csrf, "shop.beta.example", reference="ws_beta")
    found = client.get("/portal/applications/acme/domains?q=alpha").text
    assert "forms.alpha.example" in found and "shop.beta.example" not in found
    by_reference = client.get("/portal/applications/acme/domains?q=WS_BETA").text
    assert "shop.beta.example" in by_reference and "forms.alpha.example" not in by_reference
    wildcard = client.get("/portal/applications/acme/domains?q=%25").text
    assert "No matching hostnames" in wildcard  # LIKE wildcards match literally
    ready = client.get("/portal/applications/acme/domains?status=ready").text
    assert "No matching hostnames" in ready


def test_application_rename_and_delete(portal, session):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    renamed = client.post(
        "/portal/applications/acme/name", data={"csrf": csrf, "name": "Acme Forms"}
    )
    assert renamed.status_code == 200 and "Acme Forms" in renamed.text
    domain_id = _domain(client, csrf, "forms.customer.example")
    client.post("/portal/applications/acme/credentials", data={"csrf": csrf, "label": "backend"})
    client.post(
        "/portal/applications/acme/origins",
        data={"csrf": csrf, "host": "app.acme.example", "scheme": "https"},
    )

    wrong = client.post("/portal/applications/acme/delete", data={"csrf": csrf, "confirm": "acm"})
    assert wrong.status_code == 400 and "Type the application" in wrong.text
    busy = client.post("/portal/applications/acme/delete", data={"csrf": csrf, "confirm": "acme"})
    assert busy.status_code == 400 and "still has 1 live domain" in busy.text

    gone = client.post(
        "/portal/applications/acme/delete",
        data={"csrf": csrf, "confirm": "acme", "delete_domains": "true"},
        follow_redirects=False,
    )
    assert gone.status_code == 303 and gone.headers["location"].startswith("/portal/applications")
    assert client.get("/portal/applications/acme").status_code == 404
    with session_scope(session) as s:
        from sqlalchemy import func, select

        from app.models import ApiCredential, Domain, DomainEvent, VerifiedOrigin

        for model in (ApiCredential, VerifiedOrigin, Domain, DomainEvent):
            assert s.scalar(select(func.count()).select_from(model)) == 0, model
    # The hostname can be registered again by another application.
    _create(client, csrf, "beta")
    assert _domain(client, csrf, "forms.customer.example", slug="beta") != domain_id


def test_origin_and_credential_deletes_refuse_what_is_in_use(portal, session):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    client.post("/portal/applications/acme/credentials", data={"csrf": csrf, "label": "backend"})
    client.post(
        "/portal/applications/acme/origins",
        data={"csrf": csrf, "host": "app.acme.example", "scheme": "https"},
    )
    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        credential_id = app_service.list_credentials(s, acme)[0].id
        origin_id = app_service.list_origins(s, acme)[0].id

    base = "/portal/applications/acme"
    active = client.post(f"{base}/credentials/{credential_id}/delete", data={"csrf": csrf})
    assert active.status_code == 400 and "Revoke the credential" in active.text
    client.post(f"{base}/credentials/{credential_id}/revoke", data={"csrf": csrf})
    assert (
        client.post(
            f"{base}/credentials/{credential_id}/delete",
            data={"csrf": csrf},
            follow_redirects=False,
        ).status_code
        == 303
    )
    page = client.get(f"{base}/origins").text
    assert "Waiting for verification" in page and "custom-domain-origin-verification" in page
    assert (
        client.post(
            f"{base}/origins/{origin_id}/delete", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    with session_scope(session) as s:
        acme = app_service.get_application_by_slug(s, "acme")
        assert app_service.list_credentials(s, acme) == []
        assert app_service.list_origins(s, acme) == []


def test_webhooks_are_managed_from_the_portal(portal, session):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    base = "/portal/applications/acme/webhooks"
    created = client.post(
        base,
        data={"csrf": csrf, "url": "http://localhost:9/hook", "events": ["domain.ready"]},
    )
    assert created.status_code == 200 and "whsec_" in created.text
    assert "shown only once" in created.text
    with session_scope(session) as s:
        from app.services import webhooks as webhook_service

        acme = app_service.get_application_by_slug(s, "acme")
        subscription = webhook_service.list_subscriptions(s, acme)[0]
        subscription_id, first = subscription.id, subscription.secret
    assert first not in client.get(base).text  # never shown again
    rotated = client.post(f"{base}/{subscription_id}/rotate", data={"csrf": csrf})
    assert rotated.status_code == 200 and "whsec_" in rotated.text and first not in rotated.text
    assert client.get(f"{base}/{subscription_id}").status_code == 200
    early = client.post(f"{base}/{subscription_id}/delete", data={"csrf": csrf})
    assert early.status_code == 400 and "Revoke the webhook" in early.text
    client.post(f"{base}/{subscription_id}/revoke", data={"csrf": csrf})
    assert (
        client.post(
            f"{base}/{subscription_id}/delete", data={"csrf": csrf}, follow_redirects=False
        ).status_code
        == 303
    )
    bad = client.post(base, data={"csrf": csrf, "url": "ftp://x.example/", "events": ["x"]})
    assert bad.status_code == 400


def test_edge_page_lists_names_with_their_dns(portal):
    client = portal
    sign_in(client)
    csrf = csrf_of(client)
    _create(client, csrf)
    _create(client, csrf, "beta", "domains.beta.example")
    page = client.get("/portal/edge").text
    assert "edge.example.net" in page and "93.184.216.34" in page and "Resolves" in page
    assert "domains.beta.example does not exist in public DNS" in page
    verified = client.get("/portal/edge?verify=1").text
    assert "Reaches this edge" in verified


def test_service_deletes_are_guarded(session):
    acme = app_service.create_application(
        session, slug="acme", name="Acme", cname_target="edge.example.net"
    )
    domain_service.claim_domain(session, acme, "forms.customer.example", "ws_1")
    with pytest.raises(ConfirmationMismatch):
        app_service.delete_application(session, acme, confirm_slug="beta")
    with pytest.raises(ApplicationNotEmpty):
        app_service.delete_application(session, acme, confirm_slug="acme")

    origin = app_service.register_origin(session, acme, host="app.acme.example")
    origin.status = OriginStatus.VERIFIED
    app_service.activate_origin(session, origin)
    with pytest.raises(OriginInUse):
        app_service.delete_origin(session, origin)
    credential, _ = app_service.issue_credential(session, acme, label="backend")
    with pytest.raises(CredentialInUse):
        app_service.delete_credential(session, acme, credential.id)
    expired, _ = app_service.issue_credential(
        session, acme, label="old", expires_at=utcnow() - timedelta(seconds=1)
    )
    app_service.delete_credential(session, acme, expired.id)  # expired: removable

    assert (
        app_service.delete_application(session, acme, confirm_slug="acme", delete_domains=True) == 1
    )
    assert app_service.list_applications(session) == []


def test_cli_offers_the_new_actions(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    assert (
        cli.main(
            [
                "application",
                "create",
                "--slug",
                "acme",
                "--name",
                "Acme",
                "--cname-target",
                "edge.example.net",
            ]
        )
        == 0
    )
    assert cli.main(["application", "rename", "--application", "acme", "--name", "Acme 2"]) == 0
    assert cli.main(["origin", "register", "--application", "acme", "--host", "a.example"]) == 0
    assert cli.main(["origin", "delete", "--application", "acme", "--host", "a.example"]) == 0
    assert cli.main(["application", "delete", "--application", "acme", "--confirm", "x"]) == 2
    assert cli.main(["application", "delete", "--application", "acme", "--confirm", "acme"]) == 0
    assert "deleted application acme" in capsys.readouterr().out


def test_presenters_describe_states_in_words():
    assert presenters._relative(
        "_custom-domain-challenge.forms.customer.example", "forms.customer.example"
    ) == ("_custom-domain-challenge.forms")
    assert presenters._relative("customer.example", "customer.example") == "@"
    assert presenters._relative("shop.customer.example", "shop.customer.example") == "shop"

    class Event:
        event_type = "domain.status_changed"
        payload = {"from": "provisioning", "to": "ready", "reason": "all_checks_passing"}

    assert presenters.describe_event(Event()) == (
        "Status changed",
        "Provisioning → Live (all checks passing)",
    )


def _uuid(value):
    import uuid

    return uuid.UUID(value)
