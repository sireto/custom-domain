import pytest

from app.edge.assertion import AssertionInvalid, parse_keys, sign, verify

KEYS = parse_keys("2:" + "b" * 40 + ",1:" + "a" * 40)


def _token(**overrides):
    params = dict(
        key_id="2",
        key=KEYS["2"],
        application_id="app-1",
        domain_id="dom-1",
        reference="ws_42",
        hostname="forms.customer.example",
        request_id="req-1",
        now=1_000_000,
        ttl=60,
    )
    params.update(overrides)
    return sign(**params)


def test_sign_and_verify_round_trip():
    token = _token()
    assert token.startswith("v2.") is False and token.startswith("v1.2.")
    assertion = verify(token, KEYS, expected_application_id="app-1", now=1_000_010)
    assert assertion.reference == "ws_42" and assertion.domain_id == "dom-1"
    assert assertion.hostname == "forms.customer.example" and assertion.request_id == "req-1"
    assert assertion.issued_at == 1_000_000 and assertion.expires_at == 1_000_060
    assert assertion.key_id == "2"


def test_rotation_accepts_previous_key_but_signs_with_active():
    old = _token(key_id="1", key=KEYS["1"])
    assert verify(old, KEYS, now=1_000_001).key_id == "1"
    with pytest.raises(AssertionInvalid) as info:
        verify(old, {"2": KEYS["2"]}, now=1_000_001)
    assert info.value.code == "unknown_key"


@pytest.mark.parametrize(
    ("mutate", "code", "kwargs"),
    [
        (lambda t: None, "missing", {}),
        (lambda t: "garbage", "malformed", {}),
        (lambda t: t[:-4] + "AAAA", "bad_signature", {}),
        (lambda t: t.replace("v1.", "v9."), "malformed", {}),
        (lambda t: t, "expired", {"now": 1_000_000 + 60 + 31}),
        (lambda t: t, "not_yet_valid", {"now": 1_000_000 - 31}),
        (lambda t: t, "wrong_application", {"expected_application_id": "app-2"}),
        (lambda t: t, "wrong_hostname", {"expected_hostname": "other.customer.example"}),
    ],
)
def test_verify_rejects(mutate, code, kwargs):
    token = mutate(_token())
    with pytest.raises(AssertionInvalid) as info:
        verify(token, KEYS, now=kwargs.pop("now", 1_000_001), **kwargs)
    assert info.value.code == code


def test_tampered_payload_fails_signature():
    token = _token()
    head, key_id, payload, sig = token.split(".")
    other = _token(reference="ws_99").split(".")[2]
    with pytest.raises(AssertionInvalid) as info:
        verify(f"{head}.{key_id}.{other}.{sig}", KEYS, now=1_000_001)
    assert info.value.code == "bad_signature"


def test_skew_tolerance_at_boundaries():
    token = _token()
    assert verify(token, KEYS, now=1_000_000 + 60 + 30)
    assert verify(token, KEYS, now=1_000_000 - 30)


def test_parse_keys_validation():
    assert list(parse_keys("k1:" + "x" * 32).keys()) == ["k1"]
    with pytest.raises(ValueError):
        parse_keys("k1:short")
    with pytest.raises(ValueError):
        parse_keys("nocolon")
    assert parse_keys("") == {}
