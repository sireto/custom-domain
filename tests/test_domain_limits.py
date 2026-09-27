"""Limits on live domains: per application and per deployment (MAX_DOMAINS)."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.legacy import LegacyDomain, import_legacy_domains
from app.models import Application, Domain
from app.models.types import utcnow
from app.services import applications as app_service
from app.services import doctor, limits
from app.services.domains import claim_domain, delete_domain
from app.services.errors import DomainLimitReached, InvalidApplication


def _claim(session, application, n: int):
    domain = claim_domain(session, application, f"d{n}.customer.example", f"ws_{n}")
    session.commit()
    return domain


def test_an_application_limit_refuses_new_domains_and_keeps_existing(session, make_application):
    acme = make_application("acme")
    app_service.set_domain_limit(session, acme, 2)
    session.commit()
    first, second = _claim(session, acme, 1), _claim(session, acme, 2)

    with pytest.raises(DomainLimitReached) as refused:
        _claim(session, acme, 3)
    session.rollback()
    assert refused.value.details == {"scope": "application", "limit": 2, "live": 2}
    assert "acme" in refused.value.message
    assert session.query(Domain).count() == 2  # nothing half-created, nothing removed

    # Deleted domains do not count: deleting one frees a place at once.
    delete_domain(session, acme, first.id)
    session.commit()
    _claim(session, acme, 3)
    assert limits.live_domains(session, acme) == 2
    assert second.deleted_at is None

    # Other applications are unaffected by acme's limit.
    globex = make_application("globex")
    for n in range(10, 15):
        _claim(session, globex, n)


def test_a_limit_can_be_lowered_below_the_count_or_removed(session, make_application):
    acme = make_application("acme")
    for n in range(3):
        _claim(session, acme, n)
    app_service.set_domain_limit(session, acme, 1)
    session.commit()
    assert limits.live_domains(session, acme) == 3  # still served
    with pytest.raises(DomainLimitReached):
        _claim(session, acme, 9)
    session.rollback()

    app_service.set_domain_limit(session, acme, None)
    session.commit()
    _claim(session, acme, 9)

    for bad in (0, -5):
        with pytest.raises(InvalidApplication):
            app_service.set_domain_limit(session, acme, bad)


def test_the_deployment_limit_spans_applications(session, make_application, monkeypatch):
    monkeypatch.setenv("MAX_DOMAINS", "3")
    acme, globex = make_application("acme"), make_application("globex")
    _claim(session, acme, 1)
    _claim(session, globex, 2)
    _claim(session, globex, 3)
    with pytest.raises(DomainLimitReached) as refused:
        _claim(session, acme, 4)
    session.rollback()
    assert refused.value.details == {"scope": "deployment", "limit": 3, "live": 3}


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("  ", None), ("25", 25), (" 7 ", 7)])
def test_max_domains_parsing(raw, expected):
    assert limits.deployment_limit({"MAX_DOMAINS": raw}) == expected
    assert limits.deployment_limit({}) is None


@pytest.mark.parametrize("raw", ["0", "-1", "ten", "2.5"])
def test_an_unusable_max_domains_is_refused(raw, monkeypatch):
    with pytest.raises(limits.InvalidLimit):
        limits.deployment_limit({"MAX_DOMAINS": raw})
    # The API does not start with it, rather than failing each registration.
    monkeypatch.setenv("MAX_DOMAINS", raw)
    for name in ("EDGE_RECONCILE_ENABLED", "DNS_WORKER_ENABLED", "WEBHOOK_WORKER_ENABLED"):
        monkeypatch.setenv(name, "false")
    from app.main import create_app

    with pytest.raises(limits.InvalidLimit), TestClient(create_app()):
        pass


def _race(session_factory, applications, workers=8):
    barrier = threading.Barrier(workers)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        application = applications[index % len(applications)]
        with session_factory() as s:
            row = s.get(Application, application.id)
            s.commit()  # a fresh write transaction for the claim (SQLite snapshots)
            barrier.wait()
            try:
                claim_domain(s, row, f"race{index}.customer.example", f"ws_{index}", now=utcnow())
                s.commit()
                result = "won"
            except DomainLimitReached:
                s.rollback()
                result = "refused"
        with lock:
            outcomes.append(result)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return outcomes


def test_concurrent_registrations_never_exceed_an_application_limit(
    session, session_factory, make_application
):
    acme = make_application("acme")
    app_service.set_domain_limit(session, acme, 3)
    session.commit()
    outcomes = _race(session_factory, [acme])
    assert len(outcomes) == 8 and outcomes.count("won") == 3
    with session_factory() as s:
        assert limits.live_domains(s) == 3


def test_concurrent_registrations_never_exceed_the_deployment_limit(
    session_factory, make_application, monkeypatch
):
    monkeypatch.setenv("MAX_DOMAINS", "3")
    applications = [make_application("acme"), make_application("globex")]
    outcomes = _race(session_factory, applications)
    assert len(outcomes) == 8 and outcomes.count("won") == 3
    with session_factory() as s:
        assert limits.live_domains(s) == 3


def test_legacy_import_reports_hostnames_over_the_limit(session, make_application):
    acme = make_application("acme")
    app_service.set_domain_limit(session, acme, 1)
    session.commit()
    entries = [
        LegacyDomain("one.customer.example", "app.acme.example:443"),
        LegacyDomain("two.customer.example", "app.acme.example:443"),
    ]
    report = import_legacy_domains(session, acme, entries, hostname_as_reference=True)
    assert [d.hostname for d in report.imported] == ["one.customer.example"]
    assert report.skipped == [("two.customer.example", "domain_limit_reached")]


def test_the_doctor_warns_near_a_limit(session, session_factory, make_application, monkeypatch):
    monkeypatch.delenv("MAX_DOMAINS", raising=False)
    acme = make_application("acme")
    assert doctor._limit_findings(session_factory) == []  # no limits: nothing to say

    app_service.set_domain_limit(session, acme, 5)
    session.commit()
    for n in range(3):
        _claim(session, acme, n)
    [finding] = doctor._limit_findings(session_factory)
    assert finding.status == "ok" and "application acme 3 of 5" in finding.detail

    _claim(session, acme, 3)  # 80%
    [finding] = doctor._limit_findings(session_factory)
    assert finding.status == "warn" and "4 of 5" in finding.detail and "80%" in finding.detail

    _claim(session, acme, 4)
    monkeypatch.setenv("MAX_DOMAINS", "100")
    [finding] = doctor._limit_findings(session_factory)  # the deployment is far from its limit
    assert finding.status == "warn" and "at its limit" in finding.detail
    assert "domain_limit_reached" in finding.detail

    monkeypatch.setenv("MAX_DOMAINS", "many")
    [finding] = doctor._limit_findings(session_factory)
    assert finding.status == "fail" and "MAX_DOMAINS" in finding.detail


def test_the_v1_api_refuses_with_409(session, make_application, session_factory, monkeypatch):
    from tests.test_operator_api import make_app

    acme = make_application("acme")
    _, secret = app_service.issue_credential(session, acme, label="test")
    app_service.set_domain_limit(session, acme, 1)
    session.commit()
    headers = {"Authorization": f"Bearer {secret}"}
    with TestClient(make_app(session_factory, monkeypatch)) as client:
        body = {"hostname": "one.customer.example", "reference": "ws_1"}
        assert client.post("/v1/domains", json=body, headers=headers).status_code == 201
        body = {"hostname": "two.customer.example", "reference": "ws_2"}
        refused = client.post("/v1/domains", json=body, headers=headers)
    assert refused.status_code == 409
    error = refused.json()["error"]
    assert error["code"] == "domain_limit_reached"
    assert error["details"] == {"scope": "application", "limit": 1, "live": 1}


def test_the_operator_api_sets_and_clears_the_limit(operator_client):
    from tests.test_operator_api import AUTH

    base = "/operator/v1/applications"
    created = operator_client.post(base, json={"slug": "acme", "name": "Acme"}, headers=AUTH)
    assert created.json()["max_domains"] is None and created.json()["live_domains"] == 0

    patched = operator_client.patch(f"{base}/acme", json={"max_domains": 50}, headers=AUTH)
    assert patched.status_code == 200 and patched.json()["max_domains"] == 50
    # Omitting the field leaves it alone; null removes it.
    renamed = operator_client.patch(f"{base}/acme", json={"name": "Acme Forms"}, headers=AUTH)
    assert renamed.json()["max_domains"] == 50
    cleared = operator_client.patch(f"{base}/acme", json={"max_domains": None}, headers=AUTH)
    assert cleared.json()["max_domains"] is None
    bad = operator_client.patch(f"{base}/acme", json={"max_domains": 0}, headers=AUTH)
    assert bad.status_code == 422


@pytest.fixture
def operator_client(session_factory, session, monkeypatch):
    from tests.test_operator_api import make_app

    app = make_app(session_factory, monkeypatch, OPERATOR_ALLOWED_IPS="93.184.216.34")
    with TestClient(app, client=("10.0.0.5", 1000)) as client:
        yield client


def test_the_cli_sets_the_limit(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "_engine", None)
    monkeypatch.setattr(session_module, "_session_factory", None)
    assert cli.main(["db", "upgrade"]) == 0
    create = ["application", "create", "--slug", "acme", "--name", "Acme"]
    assert cli.main([*create, "--cname-target", "edge.example.net"]) == 0
    capsys.readouterr()

    limit = ["application", "set-domain-limit", "--application", "acme"]
    assert cli.main([*limit, "--max", "10"]) == 0
    assert "at most 10 live domains (0 now)" in capsys.readouterr().out
    assert cli.main([*limit, "--none"]) == 0
    assert "no domain limit" in capsys.readouterr().out
    assert cli.main([*limit, "--max", "0"]) == 2
    assert "invalid_application" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(limit)  # --max or --none is required


def test_the_portal_shows_usage_and_edits_the_limit(portal, monkeypatch):
    from tests.test_portal import csrf_of, sign_in
    from tests.test_portal_manage import _create, _domain

    monkeypatch.setenv("MAX_DOMAINS", "100")
    sign_in(portal)
    csrf = csrf_of(portal)
    _create(portal, csrf)
    settings = portal.get("/portal/applications/acme/settings")
    assert "Domain limit" in settings.text and "This deployment allows 100" in settings.text

    saved = portal.post(
        "/portal/applications/acme/domain-limit",
        data={"csrf": csrf, "max_domains": "1"},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and "ok=domain_limit" in saved.headers["location"]
    _domain(portal, csrf, "forms.customer.example")

    page = portal.get("/portal/applications/acme/domains")
    assert "<b>1 of 1</b> hostnames allowed" in page.text
    assert "New registrations are refused" in page.text
    assert "<b>1 of 100</b>" in page.text

    refused = portal.post(
        "/portal/applications/acme/domains",
        data={"csrf": csrf, "hostname": "two.customer.example", "reference": "ws_2"},
    )
    assert refused.status_code == 400 and "reached its limit of 1" in refused.text

    for bad in ("0", "lots"):
        rejected = portal.post(
            "/portal/applications/acme/domain-limit", data={"csrf": csrf, "max_domains": bad}
        )
        assert rejected.status_code == 400
    cleared = portal.post(
        "/portal/applications/acme/domain-limit",
        data={"csrf": csrf, "max_domains": ""},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    page = portal.get("/portal/applications/acme/domains").text
    assert "This application has" not in page and "This deployment has" in page
