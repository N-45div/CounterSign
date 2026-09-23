"""Signed delivery receipts.

A receipt is a compact JWS (RFC 7515) signed with the house's Ed25519 key
(EdDSA, RFC 8037). Merchants verify it with any JOSE library against the
house's published JWK set; nothing here is Handshake-specific on their side.

The receipt carries the verdict and SHA-256 hashes of the evidence frames,
never the frames themselves. If a dispute escalates, the resident can reveal
a frame and anyone can check it against the hash in the signed receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

TYPE = "handshake-receipt+jwt"


class InvalidReceipt(ValueError):
    pass


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode()


def _raw_public(public_key: Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def jwk_thumbprint(x: str) -> str:
    """RFC 7638 thumbprint of an Ed25519 public JWK: required members, sorted, no spaces."""
    return b64u(hashlib.sha256(_json({"crv": "Ed25519", "kty": "OKP", "x": x})).digest())


class HouseKey:
    """The house's signing key. One per house; the private half never leaves it."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._key = private_key
        self.x = b64u(_raw_public(private_key.public_key()))
        self.kid = jwk_thumbprint(self.x)

    @classmethod
    def generate(cls) -> "HouseKey":
        return cls(Ed25519PrivateKey.generate())

    @classmethod
    def from_pem(cls, pem: bytes) -> "HouseKey":
        key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("house keys are Ed25519")
        return cls(key)

    def to_pem(self) -> bytes:
        return self._key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )

    def public_jwk(self) -> dict:
        return {"kty": "OKP", "crv": "Ed25519", "x": self.x, "kid": self.kid, "use": "sig", "alg": "EdDSA"}

    def sign_compact(self, header: dict, payload: bytes) -> str:
        signing_input = f"{b64u(json.dumps(header, separators=(',', ':')).encode())}.{b64u(payload)}"
        return f"{signing_input}.{b64u(self._key.sign(signing_input.encode()))}"


def issue(key: HouseKey, body: dict, *, issuer: str, issued_at_s: int, receipt_id: str) -> str:
    """Sign a receipt. `body` holds the claim, verdict and evidence hashes."""
    reserved = {"iss", "iat", "jti"} & body.keys()
    if reserved:
        raise ValueError(f"body may not set {sorted(reserved)}")
    payload = {"iss": issuer, "iat": issued_at_s, "jti": receipt_id, **body}
    return key.sign_compact({"alg": "EdDSA", "kid": key.kid, "typ": TYPE}, _json(payload))


def verify(token: str, jwks: dict | list[dict]) -> dict:
    """Check a receipt against a JWK set and return its payload."""
    keys = jwks.get("keys", []) if isinstance(jwks, dict) else jwks
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        header = json.loads(b64u_decode(header_b64))
        payload = json.loads(b64u_decode(payload_b64))
        signature = b64u_decode(sig_b64)
    except ValueError as exc:
        raise InvalidReceipt(f"malformed receipt: {exc}") from exc
    if header.get("alg") != "EdDSA":
        raise InvalidReceipt(f"unsupported alg {header.get('alg')!r}")
    if header.get("typ") != TYPE:
        raise InvalidReceipt(f"not a Handshake receipt (typ {header.get('typ')!r})")
    jwk = next((k for k in keys if k.get("kid") == header.get("kid")), None)
    if jwk is None or jwk.get("kty") != "OKP" or jwk.get("crv") != "Ed25519":
        raise InvalidReceipt("signing key not in the house's key set")
    if jwk_thumbprint(jwk["x"]) != jwk["kid"]:
        raise InvalidReceipt("key id does not match the key")
    public_key = Ed25519PublicKey.from_public_bytes(b64u_decode(jwk["x"]))
    try:
        public_key.verify(signature, f"{header_b64}.{payload_b64}".encode())
    except InvalidSignature as exc:
        raise InvalidReceipt("signature does not match") from exc
    return payload


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
