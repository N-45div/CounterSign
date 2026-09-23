import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from handshake.receipt import HouseKey, InvalidReceipt, b64u, b64u_decode, issue, jwk_thumbprint, verify

BODY = {"order_id": "ORD-1", "merchant": "shop", "verdict": "delivered",
        "evidence": [{"kind": "snapshot_after", "sha256": "ab" * 32}]}


def make(key: HouseKey, body=BODY) -> str:
    return issue(key, body, issuer="https://house.example/h/1", issued_at_s=1790000000, receipt_id="r-1")


def jwks(*keys: HouseKey) -> dict:
    return {"keys": [k.public_jwk() for k in keys]}


def test_round_trip():
    key = HouseKey.generate()
    payload = verify(make(key), jwks(key))
    assert payload["verdict"] == "delivered" and payload["jti"] == "r-1"
    assert payload["iss"] == "https://house.example/h/1"


def test_tampered_payload_fails():
    key = HouseKey.generate()
    header, payload, sig = make(key).split(".")
    body = json.loads(b64u_decode(payload))
    body["verdict"] = "not_observed"
    forged = f"{header}.{b64u(json.dumps(body).encode())}.{sig}"
    with pytest.raises(InvalidReceipt, match="signature"):
        verify(forged, jwks(key))


def test_unknown_key_fails():
    with pytest.raises(InvalidReceipt, match="key set"):
        verify(make(HouseKey.generate()), jwks(HouseKey.generate()))


def test_alg_none_is_refused():
    key = HouseKey.generate()
    _, payload, _ = make(key).split(".")
    header = b64u(json.dumps({"alg": "none", "kid": key.kid, "typ": "handshake-receipt+jwt"}).encode())
    with pytest.raises(InvalidReceipt, match="alg"):
        verify(f"{header}.{payload}.", jwks(key))


def test_a_kid_pointing_at_the_wrong_key_fails():
    real, other = HouseKey.generate(), HouseKey.generate()
    swapped = {**other.public_jwk(), "kid": real.kid}
    with pytest.raises(InvalidReceipt):
        verify(make(real), {"keys": [swapped]})


def test_body_cannot_override_registered_claims():
    with pytest.raises(ValueError):
        issue(HouseKey.generate(), {"iss": "someone-else"}, issuer="x", issued_at_s=0, receipt_id="r")


def test_pem_round_trip_keeps_the_key_id():
    key = HouseKey.generate()
    again = HouseKey.from_pem(key.to_pem())
    assert again.kid == key.kid
    assert verify(make(key), jwks(again))["order_id"] == "ORD-1"


# RFC 8037 Appendix A test vectors (Ed25519 key, thumbprint, and JWS).
RFC_D = "nWGxne_9WmC6hEr0kuwsxERJxWl7MmkZcDusAxyuf2A"
RFC_X = "11qYAYKxCrfVS_7TyWQHOg7hcvPapiMlrwIaaPcHURo"
RFC_THUMBPRINT = "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
RFC_JWS = ("eyJhbGciOiJFZERTQSJ9.RXhhbXBsZSBvZiBFZDI1NTE5IHNpZ25pbmc."
           "hgyY0il_MGCjP0JzlnLWG1PPOt7-09PGcvMg3AIbQR6dWbhijcNR4ki4iylGjg5BhVsPt9g7sVvpAr_MuM0KAg")


def test_rfc8037_vectors():
    key = HouseKey(Ed25519PrivateKey.from_private_bytes(b64u_decode(RFC_D)))
    assert key.x == RFC_X
    assert jwk_thumbprint(RFC_X) == RFC_THUMBPRINT == key.kid
    assert key.sign_compact({"alg": "EdDSA"}, b"Example of Ed25519 signing") == RFC_JWS
