import pytest

from app.hostname import InvalidHostname, canonicalize


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("forms.example.com", "forms.example.com"),
        ("  Forms.Example.COM.  ", "forms.example.com"),
        ("bücher.example.com", "xn--bcher-kva.example.com"),
        ("a.b.example.co.uk", "a.b.example.co.uk"),
        ("forms.customer.co.uk", "forms.customer.co.uk"),
        ("app.pages.github.io", "app.pages.github.io"),
        ("XN--BCHER-KVA.example.com", "xn--bcher-kva.example.com"),
    ],
)
def test_canonicalize_accepts_and_normalizes(raw, expected):
    assert canonicalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("", "empty_hostname"),
        ("   ", "empty_hostname"),
        (None, "empty_hostname"),
        ("*.example.com", "wildcard_not_supported"),
        ("192.168.1.1", "ip_literal_not_supported"),
        ("[::1]", "ip_literal_not_supported"),
        ("example.com", "apex_not_supported"),
        ("localhost", "reserved_name"),
        ("app.corp.internal", "reserved_name"),
        ("github.io", "public_suffix"),
        ("www.github.io", "apex_not_supported"),
        ("customer.co.uk", "apex_not_supported"),
        ("forms.example.123", "invalid_hostname"),
        ("a" * 64 + ".example.com", "invalid_hostname"),
        (".".join(["abcdefghij"] * 25) + ".example.com", "hostname_too_long"),
    ],
)
def test_canonicalize_rejects_with_stable_code(raw, code):
    with pytest.raises(InvalidHostname) as info:
        canonicalize(raw)
    assert info.value.code == code


@pytest.mark.parametrize(
    "raw",
    [
        "-forms.example.com",
        "forms-.example.com",
        "fo_rms.example.com",
        "a..example.com",
        "a b.example.com",
    ],
)
def test_canonicalize_rejects_malformed_labels(raw):
    with pytest.raises(InvalidHostname):
        canonicalize(raw)


def test_allow_apex_for_operator_controlled_hosts():
    assert canonicalize("Example.com", allow_apex=True) == "example.com"
    with pytest.raises(InvalidHostname):
        canonicalize("*.example.com", allow_apex=True)
