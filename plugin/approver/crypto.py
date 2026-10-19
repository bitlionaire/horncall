"""Key handling, topic derivation and the encrypted envelope (SPEC section 6)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import load_der_public_key

KEY_BYTES = 32
NONCE_BYTES = 12
ENVELOPE_VERSION = 1


class CryptoError(Exception):
    pass


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def new_key() -> str:
    return b64u_encode(os.urandom(KEY_BYTES))


def decode_key(key: str) -> bytes:
    raw = b64u_decode(key)
    if len(raw) != KEY_BYTES:
        raise CryptoError("key must be 32 bytes")
    return raw


def derive_topic(key: str, purpose: str) -> str:
    """ntfy topic name derived from the key; purpose is "req" or "res"."""
    # Protocol constant from before the rename — changing it would change every topic.
    digest = hmac.new(decode_key(key), f"zug-zug/topic-{purpose}".encode(), hashlib.sha256).digest()
    return "zz-" + base64.b32encode(digest[:15]).decode("ascii").lower()


def seal(key: str, topic: str, message: dict) -> str:
    nonce = os.urandom(NONCE_BYTES)
    plaintext = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(decode_key(key)).encrypt(nonce, plaintext, topic.encode("utf-8"))
    return json.dumps({"v": ENVELOPE_VERSION, "n": b64u_encode(nonce), "c": b64u_encode(ciphertext)},
                      separators=(",", ":"))


def open_envelope(key: str, topic: str, envelope: str) -> dict:
    try:
        env = json.loads(envelope)
        if env.get("v") != ENVELOPE_VERSION:
            raise CryptoError(f"unsupported envelope version {env.get('v')!r}")
        plaintext = AESGCM(decode_key(key)).decrypt(
            b64u_decode(env["n"]), b64u_decode(env["c"]), topic.encode("utf-8"))
        message = json.loads(plaintext.decode("utf-8"))
    except CryptoError:
        raise
    except Exception as exc:  # malformed JSON, bad tag, wrong key...
        raise CryptoError(f"cannot open envelope: {exc.__class__.__name__}") from exc
    if not isinstance(message, dict):
        raise CryptoError("message is not an object")
    return message


# Phone signatures (SPEC section 6.3): the shared key encrypts, but only the phone's own key — kept
# in Android Keystore, never on the PC — can sign a decision. A copy of config.json can't approve.

def verify_signature(public_key: str, signature: str, data: bytes) -> bool:
    """ECDSA P-256 / SHA-256; public_key = base64url X.509 DER, signature = base64url DER."""
    try:
        key = load_der_public_key(b64u_decode(public_key))
        if not isinstance(key, ec.EllipticCurvePublicKey) or key.curve.name != "secp256r1":
            return False
        key.verify(b64u_decode(signature), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def fingerprint(public_key: str) -> str:
    """Short, human-comparable id of a phone key ("3F:9A:0C:…")."""
    digest = hashlib.sha256(b64u_decode(public_key)).digest()
    return ":".join(f"{b:02X}" for b in digest[:6])
