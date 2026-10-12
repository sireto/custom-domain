"""DNS provider detection, Domain Connect discovery and the ``setup`` object."""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.dns import providers
from app.dns.domain_connect import (
    MAX_SETTINGS_BYTES,
    DiscoveryFailed,
    HttpAnswer,
    discover,
    guarded_fetch,
    parse_settings,
    provider_api_host,
)
from app.dns.resolver import DnsNameNotFound
from app.dns.settings import DEFAULT_GUIDE_URL, SetupSettings
from app.dns.worker import process_domain
from app.dns.zones import candidate_zones, default_zone, find_zone, host_in_zone
from app.models import DnsZone, DomainStatus
from app.models.types import utcnow
from app.services import dns_setup
from app.services.applications import issue_credential
from app.services.dns_setup import (
    RETRY_TTL,
    ZONE_TTL,
    ZoneDetector,
    domain_setup,
    select_method,
    setup_counts,
)
from app.services.domains import claim_domain, delete_domain, purge_tombstones
from app.services.origin_verification import OriginVerificationFailed, fetch_token
from tests.test_cli import cli_env  # noqa: F401 - fixture
from tests.test_dns_checks import FakeResolver
from tests.test_dns_worker import KW, _publish, _refresh
from tests.test_operator_api import AUTH, make_app

TEMPLATE = SetupSettings(template_provider_id="provider.example", template_service_id="hostname")
SIGNING = SetupSettings(
    template_provider_id="provider.example", template_service_id="hostname", signing_enabled=True
)
NO_TEMPLATE = SetupSettings()

CF_NS = ["ada.ns.cloudflare.com", "bob.ns.cloudflare.com"]
DC_API = "https://api.dc.example/v4/domainconnect"
SETTINGS_BODY = json.dumps(
    {
        "providerId": "cloudflare.com",
        "providerName": "Cloudflare",
        "providerDisplayName": "Cloudflare",
        "urlSyncUX": "https://dash.dc.example/domainconnect",
        "urlAPI": DC_API,
    }
)
SETTINGS_URL = "https://api.dc.example/v4/domainconnect/v2/customer.example/settings"
TEMPLATE_URL = f"{DC_API}/v2/domainTemplates/providers/provider.example/services/hostname"


class ZoneResolver(FakeResolver):
    """The checks' fake DNS plus NS records."""

    def __init__(self):
        super().__init__()
        self.ns_records: dict[str, list[str]] = {}

    def _known(self, name):
        return super()._known(name) or name in self.ns_records

    def ns(self, name):
        self.queries.append(("NS", name))
        if not self._known(name):
            raise DnsNameNotFound(name)
        return list(self.ns_records.get(name, []))


class FakeHttp:
    """Answers discovery's GETs from a table; anything else fails to connect."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, int]] = []

    def __call__(self, url, max_bytes):
        self.calls.append((url, max_bytes))
        answer = self.routes.get(url)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise DiscoveryFailed("connection_failed", f"no route to {url}")
        return answer


def dc_zone(resolver: ZoneResolver, zone: str = "customer.example") -> None:
    resolver.ns_records[zone] = list(CF_NS)
    resolver.txt_records[f"_domainconnect.{zone}"] = ["api.dc.example/v4/domainconnect"]


# --- zone and host -------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname,zone,host",
    [
        ("forms.customer.example", "customer.example", "forms"),
        ("forms.acme.co.uk", "acme.co.uk", "forms"),
        ("forms.eu.acme.co.uk", "acme.co.uk", "forms.eu"),
        ("forms.acme.com.au", "acme.com.au", "forms"),
        ("forms.school.k12.ca.us", "school.k12.ca.us", "forms"),
        # *.kawasaki.jp is a wildcard suffix with !city.kawasaki.jp as an exception.
        ("forms.city.kawasaki.jp", "city.kawasaki.jp", "forms"),
        ("forms.shop.foo.kawasaki.jp", "shop.foo.kawasaki.jp", "forms"),
    ],
)
def test_zone_and_host_from_the_public_suffix_list(hostname, zone, host):
    assert default_zone(hostname) == zone
    assert host_in_zone(hostname, zone) == host


def test_host_in_zone_edges():
    assert host_in_zone("customer.example", "customer.example") == "@"
    with pytest.raises(ValueError):
        host_in_zone("forms.other.example", "customer.example")
    # The walk never includes the hostname (it holds the CNAME) nor a public suffix.
    assert candidate_zones("forms.eu.acme.co.uk") == ["eu.acme.co.uk", "acme.co.uk"]
    assert candidate_zones("forms.acme.co.uk") == ["acme.co.uk"]


def test_find_zone_walks_up_to_the_closest_delegation():
    resolver = ZoneResolver()
    resolver.ns_records["acme.co.uk"] = ["ns-1.awsdns-01.org."]
    found = find_zone(resolver, "forms.eu.acme.co.uk")
    assert (found.zone, found.nameservers, found.error_code) == (
        "acme.co.uk",
        ("ns-1.awsdns-01.org",),
        None,
    )
    assert ("NS", "co.uk") not in resolver.queries  # never above the registrable domain

    resolver.ns_records["eu.acme.co.uk"] = CF_NS
    found = find_zone(resolver, "forms.eu.acme.co.uk")
    assert found.zone == "eu.acme.co.uk" and found.nameservers == tuple(CF_NS)


def test_find_zone_failures_fall_back_to_the_registrable_domain():
    resolver = ZoneResolver()
    missing = find_zone(resolver, "forms.customer.example")
    assert (missing.zone, missing.error_code, missing.transient) == (
        "customer.example",
        "nameservers_not_found",
        False,
    )
    resolver.unavailable.add("customer.example")
    down = find_zone(resolver, "forms.customer.example")
    assert (down.zone, down.error_code, down.transient) == ("customer.example", "dns_timeout", True)


# --- nameserver fingerprints -----------------------------------------------------------


@pytest.mark.parametrize(
    "nameservers,expected",
    [
        (["ada.ns.cloudflare.com.", "bob.ns.cloudflare.com."], "cloudflare"),
        (["ns51.domaincontrol.com", "ns52.domaincontrol.com"], "godaddy"),
        (["dns1.registrar-servers.com", "dns2.registrar-servers.com"], "namecheap"),
        (["ns-1536.awsdns-00.co.uk", "ns-512.awsdns-00.net"], "route53"),
        (["ns-cloud-a1.googledomains.com"], "google-cloud-dns"),
        (["ns01.squarespacedns.com"], "squarespace"),
        (["curitiba.ns.porkbun.com", "fortaleza.ns.porkbun.com"], "porkbun"),
        (["ns1.dns-parking.com"], "hostinger"),
        (["ns1045.ui-dns.com", "ns1074.ui-dns.de"], "ionos"),
        (["ns1.digitalocean.com"], "digitalocean"),
        (["ns1.vercel-dns.com"], "vercel"),
        (["dns1.p05.nsone.net"], "netlify"),
        (["ns1.dnsowl.com"], "namesilo"),
        (["ns1-01.azure-dns.com", "ns2-01.azure-dns.net"], "azure-dns"),
        (["NS1.Example-Hosting.Example"], "unknown"),
        ([], "unknown"),
    ],
)
def test_nameservers_map_to_provider_ids(nameservers, expected):
    assert providers.provider_for_nameservers(nameservers).id == expected


def test_mixed_nameservers_go_to_the_majority_and_ids_are_stable():
    mixed = ["ada.ns.cloudflare.com", "ns1.dnsowl.com", "ns2.dnsowl.com"]
    assert providers.provider_for_nameservers(mixed).id == "namesilo"
    ids = [p.id for p in providers.PROVIDERS]
    assert len(ids) == len(set(ids))
    assert all(i == i.lower() and " " not in i for i in ids)
    # The providers with their own guide (docs/dns-guides/<id>.md).
    guided = {p.id for p in providers.PROVIDERS if p.guide}
    assert guided == {
        "cloudflare",
        "godaddy",
        "namecheap",
        "route53",
        "google-cloud-dns",
        "squarespace",
        "porkbun",
        "hostinger",
        "ionos",
        "digitalocean",
        "vercel",
        "netlify",
    }
    assert providers.get_provider("nope").id == "unknown"
    assert providers.get_provider("unknown").guide_slug == "generic"
    assert providers.provider_for_domain_connect("GoDaddy.com").id == "godaddy"
    assert providers.provider_for_domain_connect("someone-else") is None


# --- Domain Connect discovery ----------------------------------------------------------


def test_discovery_success_checks_the_configured_template():
    resolver = ZoneResolver()
    dc_zone(resolver)
    http = FakeHttp(
        {SETTINGS_URL: HttpAnswer(200, SETTINGS_BODY), TEMPLATE_URL: HttpAnswer(200, "{}")}
    )
    found = discover(
        resolver,
        "customer.example",
        provider_id="provider.example",
        service_id="hostname",
        fetch=http,
    )
    assert found.template_supported is True and found.error_code is None
    assert found.settings == {
        "provider_id": "cloudflare.com",
        "provider_name": "Cloudflare",
        "provider_display_name": "Cloudflare",
        "url_api": DC_API,
        "url_sync_ux": "https://dash.dc.example/domainconnect",
    }
    assert [url for url, _ in http.calls] == [SETTINGS_URL, TEMPLATE_URL]
    assert http.calls[0][1] == MAX_SETTINGS_BYTES


def test_discovery_without_a_configured_template_only_reads_settings():
    resolver = ZoneResolver()
    dc_zone(resolver)
    http = FakeHttp({SETTINGS_URL: HttpAnswer(200, SETTINGS_BODY)})
    found = discover(
        resolver, "customer.example", provider_id=None, service_id="hostname", fetch=http
    )
    assert found.settings["provider_id"] == "cloudflare.com"
    assert found.template_supported is None and found.error_code is None
    assert len(http.calls) == 1


def test_discovery_with_no_record_makes_no_request():
    resolver = ZoneResolver()
    resolver.ns_records["customer.example"] = CF_NS
    http = FakeHttp()
    found = discover(resolver, "customer.example", provider_id="p", service_id="s", fetch=http)
    assert (found.settings, found.error_code, found.transient) == (None, "no_record", False)
    assert http.calls == []

    resolver.unavailable.add("_domainconnect.customer.example")
    found = discover(resolver, "customer.example", provider_id="p", service_id="s", fetch=http)
    assert found.error_code == "dns_timeout" and found.transient


@pytest.mark.parametrize(
    "value",
    ["http://api.dc.example:8080/x", "user@api.dc.example", "api.dc.example?x=1", "localhost", ""],
)
def test_unusable_txt_values_are_refused(value):
    resolver = ZoneResolver()
    resolver.txt_records["_domainconnect.customer.example"] = [value]
    http = FakeHttp()
    found = discover(resolver, "customer.example", provider_id="p", service_id="s", fetch=http)
    assert found.error_code == "txt_invalid" and http.calls == []


def test_txt_values_are_normalized():
    assert provider_api_host(['"API.dc.example/v4/domainconnect/"']) == (
        "api.dc.example/v4/domainconnect"
    )
    assert provider_api_host(["https://api.dc.example"]) == "api.dc.example"


@pytest.mark.parametrize(
    "answer,code,transient",
    [
        (HttpAnswer(404, ""), "settings_not_found", False),
        (HttpAnswer(302, ""), "redirect_refused", False),
        (HttpAnswer(503, ""), "settings_unavailable", True),
        (HttpAnswer(200, "not json"), "settings_invalid", False),
        (DiscoveryFailed("timeout", "slow"), "timeout", True),
        (DiscoveryFailed("response_too_large", "big"), "response_too_large", False),
        (DiscoveryFailed("private_address_blocked", "private"), "private_address_blocked", False),
    ],
)
def test_settings_failures_are_reported(answer, code, transient):
    resolver = ZoneResolver()
    dc_zone(resolver)
    http = FakeHttp({SETTINGS_URL: answer})
    found = discover(resolver, "customer.example", provider_id="p", service_id="s", fetch=http)
    assert (found.settings, found.error_code, found.transient) == (None, code, transient)
    assert len(http.calls) == 1


def test_a_template_that_is_not_supported():
    resolver = ZoneResolver()
    dc_zone(resolver)
    http = FakeHttp(
        {SETTINGS_URL: HttpAnswer(200, SETTINGS_BODY), TEMPLATE_URL: HttpAnswer(404, "")}
    )
    found = discover(
        resolver,
        "customer.example",
        provider_id="provider.example",
        service_id="hostname",
        fetch=http,
    )
    assert found.template_supported is False and found.error_code == "template_not_supported"
    assert found.settings is not None


def test_settings_must_name_https_endpoints():
    with pytest.raises(DiscoveryFailed):
        parse_settings(json.dumps({"providerId": "x", "urlAPI": "http://api.dc.example"}))
    with pytest.raises(DiscoveryFailed):
        parse_settings(json.dumps(["not", "an", "object"]))
    parsed = parse_settings(
        json.dumps(
            {
                "providerId": "x",
                "urlAPI": "https://api.dc.example",
                "urlSyncUX": "https://user:pw@sync.dc.example",
                "providerDisplayName": "Evil\x00<b>" + "x" * 500,
            }
        )
    )
    assert "url_sync_ux" not in parsed
    assert parsed["provider_display_name"].startswith("Evil<b>")
    assert len(parsed["provider_display_name"]) == 128


def _fail_connect(*args, **kwargs):
    raise AssertionError("discovery must not connect to a refused address")


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1"])
def test_guarded_fetch_refuses_non_public_addresses(monkeypatch, address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(family, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", _fail_connect)
    monkeypatch.setenv("ORIGIN_ALLOW_PRIVATE", "true")  # does not apply to discovery
    with pytest.raises(DiscoveryFailed) as info:
        guarded_fetch(SETTINGS_URL, MAX_SETTINGS_BYTES)
    assert info.value.code == "private_address_blocked"
    assert "public" in info.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://api.dc.example/v2/x/settings",
        "https://api.dc.example:8443/v2/x/settings",
        "https://user:pw@api.dc.example/v2/x/settings",
    ],
)
def test_guarded_fetch_refuses_other_schemes_ports_and_credentials(monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", _fail_connect)
    with pytest.raises(DiscoveryFailed) as info:
        guarded_fetch(url, MAX_SETTINGS_BYTES)
    assert info.value.code == "url_refused"


def test_guarded_fetch_connects_to_the_checked_public_address(monkeypatch):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    dialed = []

    def fake_connect(address, timeout=None):
        dialed.append(address)
        raise OSError("no network in tests")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(socket, "create_connection", fake_connect)
    with pytest.raises(DiscoveryFailed) as info:
        guarded_fetch(SETTINGS_URL, MAX_SETTINGS_BYTES)
    assert info.value.code == "connection_failed"
    assert dialed == [("93.184.216.34", 443)]


class SlowServer:
    """A loopback server that answers with ``body``, optionally one byte at a time."""

    def __init__(self, body: bytes, delay: float = 0.0):
        self.body, self.delay = body, delay
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.recv(4096)
            head = f"HTTP/1.1 200 OK\r\nContent-Length: {len(self.body)}\r\n\r\n".encode()
            try:
                if not self.delay:
                    conn.sendall(head + self.body)
                    return
                for i in range(len(head)):
                    conn.sendall(head[i : i + 1])
                    time.sleep(self.delay)
            except OSError:
                pass

    def close(self):
        self.sock.close()


def test_oversized_answers_are_refused():
    server = SlowServer(b"x" * (MAX_SETTINGS_BYTES + 1))
    try:
        with pytest.raises(OriginVerificationFailed) as info:
            fetch_token(
                "127.0.0.1",
                server.port,
                "http",
                allow_private=True,
                path="/",
                timeout=3,
                max_bytes=MAX_SETTINGS_BYTES,
            )
        assert info.value.code == "response_too_large"
    finally:
        server.close()


def test_a_server_that_answers_too_slowly_overall_times_out():
    # Each byte arrives well within the per-read timeout; the overall limit stops it.
    server = SlowServer(b"{}", delay=0.1)
    try:
        started = time.monotonic()
        with pytest.raises(OriginVerificationFailed) as info:
            fetch_token(
                "127.0.0.1",
                server.port,
                "http",
                allow_private=True,
                path="/",
                timeout=2,
                total_timeout=0.5,
            )
        assert info.value.code == "timeout"
        assert time.monotonic() - started < 2
    finally:
        server.close()


# --- method selection ------------------------------------------------------------------


def test_setup_method_selection():
    key = "provider.example/hostname"
    assert select_method("cloudflare", True, key, SIGNING) == "domain_connect"
    # Supported, but the service cannot sign apply requests.
    assert select_method("cloudflare", True, key, TEMPLATE) == "guided"
    # Support was checked for another template.
    assert select_method("cloudflare", True, "old.example/hostname", SIGNING) == "guided"
    assert select_method("cloudflare", False, key, SIGNING) == "guided"
    assert select_method("namesilo", None, None, SIGNING) == "manual"  # no specific guide
    assert select_method("unknown", None, None, SIGNING) == "manual"
    assert select_method(None, None, None, SIGNING) == "manual"


def test_settings_from_env():
    settings = SetupSettings.from_env({})
    assert settings.template_key is None and settings.guide_url == DEFAULT_GUIDE_URL
    assert not settings.signing_enabled
    settings = SetupSettings.from_env(
        {
            "DOMAIN_CONNECT_PROVIDER_ID": "provider.example",
            "DNS_GUIDE_URL": "https://docs.example/dns/{provider}/",
        }
    )
    assert settings.template_key == "provider.example/hostname"
    assert settings.guide_for("cloudflare") == "https://docs.example/dns/cloudflare/"
    for env in (
        {"DNS_GUIDE_URL": "https://docs.example/dns/"},
        {"DNS_GUIDE_URL": "javascript:alert(1)//{provider}"},
        {"DOMAIN_CONNECT_PROVIDER_ID": "a/b"},
    ):
        with pytest.raises(ValueError):
            SetupSettings.from_env(env)


# --- the worker ---------------------------------------------------------------------------


def _detector(session_factory, resolver, http=None, settings=TEMPLATE):
    return ZoneDetector(session_factory, resolver, settings=settings, fetch=http or FakeHttp())


def test_the_worker_detects_and_caches_the_provider(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    assert domain.zone == "customer.example"
    assert domain_setup(domain, TEMPLATE).method is None  # not looked up yet

    resolver = ZoneResolver()
    dc_zone(resolver)
    _publish(resolver, domain)
    http = FakeHttp(
        {SETTINGS_URL: HttpAnswer(200, SETTINGS_BODY), TEMPLATE_URL: HttpAnswer(200, "{}")}
    )
    detector = _detector(session_factory, resolver, http)
    assert process_domain(session_factory, resolver, domain.id, detector=detector, **KW).done
    _refresh(session)
    zone = session.query(DnsZone).one()
    assert (zone.zone, zone.provider, zone.template_supported) == (
        "customer.example",
        "cloudflare",
        True,
    )
    assert zone.nameservers == CF_NS and zone.error_code is None
    assert zone.expires_at - zone.checked_at == ZONE_TTL
    assert domain.status == DomainStatus.PROVISIONING  # the checks ran as usual

    setup = domain_setup(domain, SIGNING)
    assert (setup.method, setup.provider_id, setup.provider_name) == (
        "domain_connect",
        "cloudflare",
        "Cloudflare",
    )
    assert (setup.zone, setup.host) == ("customer.example", "forms")
    assert setup.guide_url == DEFAULT_GUIDE_URL.replace("{provider}", "cloudflare")
    assert domain_setup(domain, TEMPLATE).method == "guided"

    # A second domain in the same zone uses the cache: no new lookups.
    other = claim_domain(session, acme, "shop.customer.example", "w2")
    session.commit()
    resolver.queries.clear()
    http.calls.clear()
    assert detector.refresh(other.hostname, other.zone) == "customer.example"
    assert http.calls == [] and not [q for q in resolver.queries if q[0] == "NS"]

    # A different template makes the cached answer stale.
    changed = _detector(
        session_factory, resolver, http, SetupSettings(template_provider_id="other.example")
    )
    changed.refresh(other.hostname, other.zone)
    assert http.calls and http.calls[-1][0].endswith("/providers/other.example/services/hostname")


def test_a_delegated_subzone_becomes_the_domains_zone(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.eu.acme.co.uk", "w")
    session.commit()
    resolver = ZoneResolver()
    resolver.ns_records["acme.co.uk"] = CF_NS
    resolver.ns_records["eu.acme.co.uk"] = ["ns1.digitalocean.com", "ns2.digitalocean.com"]
    process_domain(
        session_factory, resolver, domain.id, detector=_detector(session_factory, resolver), **KW
    )
    _refresh(session)
    assert domain.zone == "eu.acme.co.uk"
    setup = domain_setup(domain, TEMPLATE)
    assert (setup.zone, setup.host, setup.provider_id, setup.method) == (
        "eu.acme.co.uk",
        "forms",
        "digitalocean",
        "guided",
    )


def test_detection_failures_never_break_the_checks(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()

    class Broken(ZoneResolver):
        def ns(self, name):
            raise RuntimeError("resolver bug")

    resolver = Broken()
    _publish(resolver, domain)
    assert process_domain(
        session_factory, resolver, domain.id, detector=_detector(session_factory, resolver), **KW
    ).done
    _refresh(session)
    assert domain.status == DomainStatus.PROVISIONING
    assert session.query(DnsZone).count() == 0

    # A transient failure is stored and retried sooner.
    resolver = ZoneResolver()
    dc_zone(resolver)
    _publish(resolver, domain)
    http = FakeHttp({SETTINGS_URL: DiscoveryFailed("timeout", "slow")})
    detector = _detector(session_factory, resolver, http)
    assert detector.refresh(domain.hostname, domain.zone) == "customer.example"
    _refresh(session)
    zone = session.query(DnsZone).one()
    assert zone.provider == "cloudflare" and zone.error_code == "timeout"
    assert zone.expires_at - zone.checked_at == RETRY_TTL
    assert domain_setup(domain, TEMPLATE).method == "guided"


def test_concurrent_detection_of_one_zone_updates_a_single_row(session, session_factory):
    resolver = ZoneResolver()
    resolver.ns_records["customer.example"] = CF_NS
    first = _detector(session_factory, resolver)
    second = _detector(session_factory, resolver)
    now = utcnow()
    first.refresh("forms.customer.example", None, now=now)
    # The second worker read "not fresh" before the first stored: it updates the row.
    second._store(
        find_zone(resolver, "forms.customer.example"),
        discover(resolver, "customer.example", provider_id=None, service_id="hostname"),
        None,
        now,
    )
    assert session.query(DnsZone).count() == 1


def test_expired_unreferenced_zones_are_purged(session, session_factory, make_application):
    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    long_ago = utcnow() - timedelta(days=30)
    for name in ("customer.example", "gone.example"):
        session.add(
            DnsZone(zone=name, provider="unknown", checked_at=long_ago, expires_at=long_ago)
        )
    session.commit()
    purge_tombstones(session)
    session.commit()
    assert [z.zone for z in session.query(DnsZone)] == ["customer.example"]
    assert domain.zone == "customer.example"


# --- the v1 API, the portal and the counts ---------------------------------------------------


def _zone(session, name="customer.example", provider="cloudflare", **values):
    now = utcnow()
    row = DnsZone(
        zone=name,
        provider=provider,
        checked_at=now,
        expires_at=now + ZONE_TTL,
        **values,
    )
    session.add(row)
    session.commit()
    return row


@pytest.fixture
def client(session_factory, session, monkeypatch):
    with TestClient(make_app(session_factory, monkeypatch)) as client:
        yield client


def test_v1_responses_carry_the_setup(client, session, make_application, monkeypatch):
    acme = make_application("acme")
    _, secret = issue_credential(session, acme, label="test")
    session.commit()
    headers = {"Authorization": f"Bearer {secret}"}

    created = client.post(
        "/v1/domains",
        json={"hostname": "forms.eu.customer.example", "reference": "ws"},
        headers=headers,
    )
    assert created.status_code == 201
    assert created.json()["setup"] == {
        "method": None,
        "provider": None,
        "zone": "customer.example",
        "host": "forms.eu",
        "guide_url": None,
        "detected_at": None,
    }

    _zone(
        session,
        nameservers=CF_NS,
        template="provider.example/hostname",
        template_supported=True,
    )
    domain_id = created.json()["id"]
    setup = client.get(f"/v1/domains/{domain_id}", headers=headers).json()["setup"]
    assert setup["method"] == "guided"
    assert setup["provider"] == {"id": "cloudflare", "name": "Cloudflare"}
    assert setup["guide_url"].endswith("/cloudflare.md")
    assert setup["detected_at"] is not None

    monkeypatch.setattr(dns_setup, "setup_settings", lambda: SIGNING)
    listed = client.get("/v1/domains", headers=headers).json()["items"][0]["setup"]
    assert listed["method"] == "domain_connect"


def test_an_unknown_provider_with_domain_connect_is_named_safely(portal, session, make_application):
    from tests.test_portal import sign_in

    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    session.commit()
    _zone(
        session,
        provider="unknown",
        domain_connect={"provider_id": "x", "provider_display_name": "<b>Evil</b> DNS"},
        error_code="template_not_supported",
        message="The provider supports Domain Connect but not this service's template",
        template_supported=False,
        template="provider.example/hostname",
    )
    setup = domain_setup(domain, TEMPLATE)
    assert (setup.provider_id, setup.provider_name, setup.method) == (
        "unknown",
        "<b>Evil</b> DNS",
        "manual",
    )
    assert setup.guide_url.endswith("/generic.md")

    sign_in(portal)
    page = portal.get(f"/portal/applications/acme/domains/{domain.id}")
    assert page.status_code == 200
    assert "&lt;b&gt;Evil&lt;/b&gt; DNS" in page.text and "<b>Evil</b>" not in page.text
    assert "Generic guide" in page.text and "Generic setup guide" in page.text
    assert "Offered, but not for this service" in page.text


def test_the_portal_shows_provider_and_method(portal, session, make_application):
    from tests.test_portal import sign_in

    acme = make_application("acme")
    domain = claim_domain(session, acme, "forms.customer.example", "w")
    pending = claim_domain(session, acme, "forms.elsewhere.example", "w")
    session.commit()
    _zone(session, nameservers=CF_NS)

    sign_in(portal)
    page = portal.get(f"/portal/applications/acme/domains/{domain.id}").text
    assert "DNS provider" in page and "Cloudflare" in page and "Provider guide" in page
    assert "ada.ns.cloudflare.com" in page and "Cloudflare setup guide" in page
    assert "Not offered by this provider" in page
    assert "<script" not in page

    page = portal.get(f"/portal/applications/acme/domains/{pending.id}").text
    assert "Not looked up yet" in page

    overview = portal.get("/portal").text
    assert "DNS providers" in overview and "Cloudflare" in overview


def test_usage_counts_per_provider_and_method(session, make_application, monkeypatch):
    acme = make_application("acme")
    globex = make_application("globex")
    a = claim_domain(session, acme, "forms.customer.example", "w1")
    claim_domain(session, acme, "shop.customer.example", "w2")
    claim_domain(session, acme, "forms.unknown.example", "w3")
    claim_domain(session, globex, "forms.other.example", "w4")
    gone = claim_domain(session, globex, "old.other.example", "w5")
    claim_domain(session, globex, "forms.pending.example", "w6")
    a.status = DomainStatus.READY  # the counts read the status only
    delete_domain(session, globex, gone.id)
    session.commit()
    _zone(
        session,
        "customer.example",
        "cloudflare",
        template="provider.example/hostname",
        template_supported=True,
    )
    _zone(session, "unknown.example", "unknown")
    _zone(session, "other.example", "namesilo")

    rows = {
        (r.provider_id, r.method): (r.domains, r.ready)
        for r in setup_counts(session, settings=SIGNING)
    }
    assert rows == {
        ("cloudflare", "domain_connect"): (2, 1),
        ("unknown", "manual"): (1, 0),
        ("namesilo", "manual"): (1, 0),  # the deleted domain is not counted
        (None, None): (1, 0),  # not looked up yet
    }
    rows = {(r.provider_id, r.method) for r in setup_counts(session, settings=TEMPLATE)}
    assert ("cloudflare", "guided") in rows
    only_globex = setup_counts(session, application=globex, settings=SIGNING)
    assert sorted((r.provider_id or "", r.domains) for r in only_globex) == [
        ("", 1),
        ("namesilo", 1),
    ]
    assert setup_counts(session, since=utcnow() + timedelta(minutes=1)) == []


def test_usage_counts_over_the_operator_api(
    session_factory, session, monkeypatch, make_application
):
    acme = make_application("acme")
    claim_domain(session, acme, "forms.customer.example", "w1")
    session.commit()
    _zone(session, nameservers=CF_NS)
    app = make_app(session_factory, monkeypatch)
    with TestClient(app, client=("10.0.0.5", 1000)) as operator:
        body = operator.get("/operator/v1/dns-setup", headers=AUTH).json()
        assert body["items"] == [
            {
                "provider": {"id": "cloudflare", "name": "Cloudflare"},
                "method": "guided",
                "domains": 1,
                "ready": 0,
            }
        ]
        assert body["domains"] == 1 and body["application"] is None
        assert "forms.customer.example" not in json.dumps(body)  # counts only
        scoped = operator.get("/operator/v1/dns-setup?application=acme&days=7", headers=AUTH)
        assert scoped.json()["application"] == "acme" and scoped.json()["since"]
        missing = operator.get("/operator/v1/dns-setup?application=nope", headers=AUTH)
        assert missing.status_code == 404
        assert operator.get("/operator/v1/dns-setup").status_code == 401


def test_usage_counts_from_the_command(cli_env, capsys):  # noqa: F811
    from app import cli

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
                "acme.edge.example.net",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["domain", "setup-stats"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == ["PROVIDER", "METHOD", "DOMAINS", "READY"]
    assert cli.main(["domain", "setup-stats", "--application", "acme", "--days", "30"]) == 0
    assert cli.main(["domain", "setup-stats", "--days", "0"]) == 2


def test_the_sdk_reads_the_setup():
    from custom_domain import Domain

    from app.v1 import examples

    domain = Domain.from_dict(examples.WAITING_FOR_DNS)
    assert domain.setup.method == "guided"
    assert domain.setup.provider.id == "cloudflare" and domain.setup.host == "forms"
    assert Domain.from_dict(examples.REGISTRATION).setup.provider is None
    legacy = dict(examples.READY)
    legacy.pop("setup")
    assert Domain.from_dict(legacy).setup is None
