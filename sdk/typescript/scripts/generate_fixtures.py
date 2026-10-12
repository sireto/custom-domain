"""Write the cross-language fixtures for the TypeScript SDK's verifiers.

Tokens and signatures are produced by the service's own signers
(``app.edge.assertion.sign`` and ``app.webhooks.signature.sign``), and every
expected outcome is what the Python SDK (``custom_domain``) returns for the
case, so the TypeScript tests check the TypeScript verifiers against the
Python ones. Nothing here is hand-written: change a case, rerun, commit.

    uv run python sdk/typescript/scripts/generate_fixtures.py

writes ``sdk/typescript/test/fixtures/assertions.json`` and ``webhooks.json``. The
output is deterministic (fixed clocks and secrets), so a rerun without
changes leaves the files as they are.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from unittest import mock

from custom_domain import AssertionInvalid, SignatureInvalid, WorkspaceResolver, verify_assertion
from custom_domain import verify_webhook as sdk_verify_webhook

from app.edge.assertion import sign as sign_assertion
from app.webhooks.signature import sign as sign_webhook

OUT = Path(__file__).resolve().parent.parent / "test" / "fixtures"

NOW = 1_800_000_000
APP = "3f0c8a52-6d1e-4b7a-9c2f-0e1d2c3b4a59"
OTHER_APP = "9b8a7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
DOMAIN = "6f1c2d3e-4b5a-4c6d-8e9f-0a1b2c3d4e5f"
HOST = "forms.customer.example"
DEPLOYMENT_KEY = ("1", "deployment-key-one-" + "a" * 32)
ROTATED_KEY = ("2", "deployment-key-two-" + "b" * 32)
APP_KEY = ("app_7Qm2", "application-own-key-" + "c" * 32)


def token(key=DEPLOYMENT_KEY, *, now=NOW, ttl=60, reference="ws_8f3a1c", hostname=HOST, app=APP):
    key_id, secret = key
    return sign_assertion(
        key_id=key_id,
        key=secret.encode(),
        application_id=app,
        domain_id=DOMAIN,
        reference=reference,
        hostname=hostname,
        request_id="req-0001",
        now=now,
        ttl=ttl,
    )


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forged_payload(original: str) -> str:
    """The same token with another reference in the payload and the old MAC."""
    version, key_id, _payload, mac = original.split(".")
    payload = {
        "app": APP,
        "dom": DOMAIN,
        "ref": "ws_attacker",
        "host": HOST,
        "iat": NOW,
        "exp": NOW + 60,
        "rid": "req-0001",
    }
    return ".".join([version, key_id, b64(json.dumps(payload).encode()), mac])


def signed_raw(payload: bytes, key=DEPLOYMENT_KEY, *, padded: bool = False) -> str:
    """A correctly signed token around an arbitrary payload (malformed payloads)."""
    key_id, secret = key
    encoded = base64.urlsafe_b64encode(payload).decode() if padded else b64(payload)
    mac = hmac.new(secret.encode(), f"v1.{key_id}.{encoded}".encode(), hashlib.sha256).digest()
    return f"v1.{key_id}.{encoded}.{b64(mac)}"


def payload_json(**overrides) -> bytes:
    payload = {
        "app": APP,
        "dom": DOMAIN,
        "ref": "ws_8f3a1c",
        "host": HOST,
        "iat": NOW,
        "exp": NOW + 60,
        "rid": "req-0001",
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode()


def signed_raw_padded() -> str:
    """A signed payload whose base64url keeps its `=` padding (length not a multiple of 3)."""
    payload = payload_json(rid="req-01")
    assert len(payload) % 3, "choose a payload that needs padding"
    return signed_raw(payload, padded=True)


def string_times() -> bytes:
    return payload_json(iat=str(NOW), exp=str(NOW + 60))


def assertion_dict(assertion) -> dict:
    return {
        "applicationId": assertion.application_id,
        "domainId": assertion.domain_id,
        "reference": assertion.reference,
        "hostname": assertion.hostname,
        "issuedAt": assertion.issued_at,
        "expiresAt": assertion.expires_at,
        "requestId": assertion.request_id,
        "keyId": assertion.key_id,
    }


def outcome(call) -> dict:
    try:
        return {"ok": True, "assertion": assertion_dict(call())}
    except AssertionInvalid as exc:
        return {"ok": False, "code": exc.code}


KEYS = dict([DEPLOYMENT_KEY])
ROTATION_KEYS = dict([ROTATED_KEY, DEPLOYMENT_KEY])


def resolver_case(name, *, headers, host, keys=KEYS, host_header="customer", now=NOW, skew=30):
    resolver = WorkspaceResolver(keys, APP, skew=skew, host_header=host_header)

    # WorkspaceResolver reads the clock itself; pin it through verify_assertion's `now`.
    def pinned(*args, **kwargs):
        return verify_assertion(*args, **{**kwargs, "now": now})

    with mock.patch("custom_domain.middleware.verify_assertion", pinned):
        result = outcome(lambda: resolver.resolve(headers, host))
    return {
        "name": name,
        "kind": "resolve",
        "keys": keys,
        "applicationId": APP,
        "hostHeader": host_header,
        "headers": headers,
        "host": host,
        "now": now,
        "skew": skew,
        "expect": result,
    }


def verify_case(name, *, token_value, keys=KEYS, hostname=HOST, app=APP, now=NOW, skew=30):
    result = outcome(
        lambda: verify_assertion(
            token_value,
            keys,
            expected_application_id=app,
            expected_hostname=hostname,
            now=now,
            skew=skew,
        )
    )
    return {
        "name": name,
        "kind": "verify",
        "token": token_value,
        "keys": keys,
        "applicationId": app,
        "hostname": hostname,
        "now": now,
        "skew": skew,
        "expect": result,
    }


H = "X-Custom-Domain-Assertion"


def assertion_cases() -> list[dict]:
    valid = token()
    return [
        verify_case("valid", token_value=valid),
        verify_case("valid without a hostname check", token_value=valid, hostname=None),
        verify_case("valid at the end of the skew", token_value=valid, now=NOW + 60 + 30),
        verify_case("expired", token_value=valid, now=NOW + 60 + 31),
        verify_case("issued in the future", token_value=valid, now=NOW - 31),
        verify_case("issued at the start of the skew", token_value=valid, now=NOW - 30),
        verify_case("custom skew", token_value=valid, now=NOW + 60 + 31, skew=120),
        verify_case("missing", token_value=None),
        verify_case("empty", token_value=""),
        verify_case("garbage", token_value="garbage"),
        verify_case("three parts", token_value=".".join(valid.split(".")[:3])),
        verify_case("five parts", token_value=valid + ".x"),
        verify_case("other version", token_value="v2." + valid.split(".", 1)[1]),
        verify_case("unknown key id", token_value=token(ROTATED_KEY)),
        verify_case("key id of an object property", token_value=token(("constructor", "x" * 40))),
        verify_case(
            "wrong secret for the key id",
            token_value=token(("1", "not-the-deployment-key-" + "z" * 32)),
        ),
        verify_case("tampered payload", token_value=forged_payload(valid)),
        verify_case("tampered signature", token_value=valid[:-4] + "AAAA"),
        verify_case("signature not base64url", token_value=valid[:-4] + "!!!!"),
        verify_case("signature with padding", token_value=valid + "="),
        verify_case("signature with a stray character", token_value=valid + "!"),
        verify_case("signature with a dangling character", token_value=valid + "A"),
        verify_case("signature with a non-ASCII character", token_value=valid + "é"),
        verify_case(
            "signature in the standard alphabet",
            token_value=valid.rsplit(".", 1)[0]
            + "."
            + valid.rsplit(".", 1)[1].replace("-", "+").replace("_", "/"),
        ),
        verify_case("payload with padding", token_value=signed_raw_padded()),
        verify_case("integer fields as strings", token_value=signed_raw(string_times())),
        verify_case("payload not json", token_value=signed_raw(b"not json")),
        verify_case("payload a list", token_value=signed_raw(b"[1, 2]")),
        verify_case(
            "payload lacks rid",
            token_value=signed_raw(
                json.dumps(
                    {"app": APP, "dom": DOMAIN, "ref": "r", "host": HOST, "iat": NOW, "exp": NOW}
                ).encode()
            ),
        ),
        verify_case("wrong application", token_value=valid, app=OTHER_APP),
        verify_case("signed for another application", token_value=token(app=OTHER_APP)),
        verify_case("wrong hostname", token_value=valid, hostname="other.customer.example"),
        verify_case(
            "hostname case and trailing dot", token_value=valid, hostname=HOST.upper() + "."
        ),
        verify_case(
            "rotation: signed with the new key", token_value=token(ROTATED_KEY), keys=ROTATION_KEYS
        ),
        verify_case("rotation: signed with the old key", token_value=valid, keys=ROTATION_KEYS),
        verify_case(
            "rotation: old key already removed", token_value=valid, keys=dict([ROTATED_KEY])
        ),
        verify_case(
            "application key next to the deployment key",
            token_value=token(APP_KEY),
            keys=dict([DEPLOYMENT_KEY, APP_KEY]),
        ),
        verify_case(
            "unicode reference",
            token_value=token(reference="ws_été_日本"),
        ),
        # The resolver: Host, X-Forwarded-Host and the header lookup.
        resolver_case("resolver: valid", headers={H: valid}, host=HOST),
        resolver_case("resolver: lowercase header name", headers={H.lower(): valid}, host=HOST),
        resolver_case("resolver: Host with a port", headers={H: valid}, host=HOST + ":443"),
        resolver_case(
            "resolver: Host in capitals", headers={H: valid}, host="Forms.Customer.Example"
        ),
        resolver_case("resolver: wrong Host", headers={H: valid}, host="app.saas.example"),
        resolver_case("resolver: no Host", headers={H: valid}, host=None),
        resolver_case("resolver: missing", headers={}, host=HOST),
        resolver_case(
            "resolver: X-Forwarded-Host is ignored in customer mode",
            headers={H: valid, "X-Forwarded-Host": "other.customer.example"},
            host=HOST,
        ),
        resolver_case(
            "origin mode: valid",
            headers={H: valid, "X-Forwarded-Host": HOST},
            host="app.saas.example",
            host_header="origin",
        ),
        resolver_case(
            "origin mode: a proxy appended to X-Forwarded-Host",
            headers={H: valid, "X-Forwarded-Host": HOST + ", proxy.internal"},
            host="app.saas.example",
            host_header="origin",
        ),
        resolver_case(
            "origin mode: X-Forwarded-Host with a port",
            headers={H: valid, "x-forwarded-host": HOST + ":443"},
            host="app.saas.example",
            host_header="origin",
        ),
        resolver_case(
            "origin mode: no X-Forwarded-Host",
            headers={H: valid},
            host="app.saas.example",
            host_header="origin",
        ),
        resolver_case(
            "origin mode: wrong X-Forwarded-Host",
            headers={H: valid, "X-Forwarded-Host": "other.customer.example"},
            host=HOST,
            host_header="origin",
        ),
        resolver_case(
            "origin mode: no assertion",
            headers={"X-Forwarded-Host": HOST},
            host="app.saas.example",
            host_header="origin",
        ),
        resolver_case(
            "origin mode: expired",
            headers={H: valid, "X-Forwarded-Host": HOST},
            host="app.saas.example",
            host_header="origin",
            now=NOW + 1000,
        ),
        resolver_case(
            "origin mode: rotation",
            headers={H: token(ROTATED_KEY), "X-Forwarded-Host": HOST},
            host="app.saas.example",
            host_header="origin",
            keys=ROTATION_KEYS,
        ),
    ]


WEBHOOK_BODY = json.dumps(
    {
        "id": "b7e2c1a0-9d8f-4e7a-b6c5-d4e3f2a1b0c9",
        "type": "domain.ready",
        "created_at": "2026-09-25T15:00:00+00:00",
        "data": {
            "domain": {
                "id": DOMAIN,
                "hostname": HOST,
                "reference": "ws_8f3a1c — café",
                "status": "ready",
                "dns_records": [],
                "checks": [],
                "metadata": None,
                "created_at": "2026-09-25T14:00:00+00:00",
                "updated_at": "2026-09-25T15:00:00+00:00",
                "deleted_at": None,
            }
        },
    },
    separators=(",", ":"),
    ensure_ascii=False,
)
CURRENT = "whsec_current_" + "q" * 32
PREVIOUS = "whsec_previous_" + "p" * 32


def webhook_case(name, *, header, secrets, body=WEBHOOK_BODY, now=NOW, tolerance=300):
    try:
        result = {
            "ok": True,
            "timestamp": sdk_verify_webhook(
                header, body.encode(), secrets, now=now, tolerance=tolerance
            ),
        }
    except SignatureInvalid as exc:
        result = {"ok": False, "code": exc.code}
    return {
        "name": name,
        "header": header,
        "body": body,
        "secrets": secrets,
        "now": now,
        "tolerance": tolerance,
        "expect": result,
    }


def webhook_cases() -> list[dict]:
    raw = WEBHOOK_BODY.encode()
    one = sign_webhook(raw, [CURRENT], timestamp=NOW)
    both = sign_webhook(raw, [CURRENT, PREVIOUS], timestamp=NOW)
    digest = one.split("v1=")[1]
    return [
        webhook_case("valid", header=one, secrets=[CURRENT]),
        webhook_case(
            "rotation: both signatures, consumer has the new secret", header=both, secrets=[CURRENT]
        ),
        webhook_case(
            "rotation: both signatures, consumer has the old secret",
            header=both,
            secrets=[PREVIOUS],
        ),
        webhook_case(
            "rotation: consumer holds both secrets", header=one, secrets=[PREVIOUS, CURRENT]
        ),
        webhook_case("wrong secret", header=one, secrets=["whsec_other_" + "o" * 32]),
        webhook_case("no secrets", header=one, secrets=[]),
        webhook_case(
            "tampered body",
            header=one,
            secrets=[CURRENT],
            body=WEBHOOK_BODY.replace("ready", "deleted", 1),
        ),
        webhook_case(
            "body with a trailing newline", header=one, secrets=[CURRENT], body=WEBHOOK_BODY + "\n"
        ),
        webhook_case("stale", header=one, secrets=[CURRENT], now=NOW + 301),
        webhook_case("at the tolerance", header=one, secrets=[CURRENT], now=NOW + 300),
        webhook_case("from the future", header=one, secrets=[CURRENT], now=NOW - 301),
        webhook_case(
            "custom tolerance", header=one, secrets=[CURRENT], now=NOW + 3000, tolerance=3600
        ),
        webhook_case("missing", header=None, secrets=[CURRENT]),
        webhook_case("empty", header="", secrets=[CURRENT]),
        webhook_case("no timestamp", header=f"v1={digest}", secrets=[CURRENT]),
        webhook_case("no v1", header=f"t={NOW}", secrets=[CURRENT]),
        webhook_case("timestamp not an integer", header=f"t=soon,v1={digest}", secrets=[CURRENT]),
        webhook_case("spaces around parts", header=f" t={NOW} , v1={digest} ", secrets=[CURRENT]),
        webhook_case(
            "unknown parts are ignored", header=f"t={NOW},v0=abc,x,v1={digest}", secrets=[CURRENT]
        ),
        webhook_case("uppercase hex", header=f"t={NOW},v1={digest.upper()}", secrets=[CURRENT]),
        webhook_case("truncated signature", header=f"t={NOW},v1={digest[:-2]}", secrets=[CURRENT]),
        webhook_case(
            "signed for another timestamp",
            header=f"t={NOW + 1},v1={digest}",
            secrets=[CURRENT],
            now=NOW + 1,
        ),
    ]


def write(name: str, cases: list[dict]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    document = {
        "generator": (
            "sdk/typescript/scripts/generate_fixtures.py (expected outcomes from the Python SDK)"
        ),
        "cases": cases,
    }
    (OUT / name).write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def main() -> None:
    write("assertions.json", assertion_cases())
    write("webhooks.json", webhook_cases())


if __name__ == "__main__":
    main()
