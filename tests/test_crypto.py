import json

import pytest

from approver import crypto

# Fixed key used to produce cross-language test vectors (shared with the Android app).
VECTOR_KEY = crypto.b64u_encode(bytes(range(32)))  # AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8
TOPIC_REQ = "zz-hfkcnxysccimpsswiwazjqn6"
TOPIC_RES = "zz-7irs6gqqrtxt2bshejqmlu3q"
ENVELOPE_C = "dZ7Bp8VJoYcq2MBbfw69u6Y7InESoxpORSJwMAB9mdFsIiVmnsnqVVOXVqg7FksJTRA"


def test_topics_are_stable_and_distinct():
    req = crypto.derive_topic(VECTOR_KEY, "req")
    res = crypto.derive_topic(VECTOR_KEY, "res")
    assert req != res
    assert req.startswith("zz-") and len(req) == 27
    assert req == crypto.derive_topic(VECTOR_KEY, "req")


def test_topic_vectors():
    # Pinned so the Kotlin implementation can assert the same values.
    assert crypto.derive_topic(VECTOR_KEY, "req") == TOPIC_REQ
    assert crypto.derive_topic(VECTOR_KEY, "res") == TOPIC_RES


def test_roundtrip_unicode():
    key = crypto.new_key()
    msg = {"type": "request", "preview": "zażółć gęślą jaźń 🗡️"}
    env = crypto.seal(key, "ca-x", msg)
    assert crypto.open_envelope(key, "ca-x", env) == msg


def test_wrong_key_wrong_topic_and_tamper_rejected():
    key = crypto.new_key()
    env = crypto.seal(key, "ca-x", {"a": 1})
    with pytest.raises(crypto.CryptoError):
        crypto.open_envelope(crypto.new_key(), "ca-x", env)
    with pytest.raises(crypto.CryptoError):
        crypto.open_envelope(key, "ca-y", env)  # AAD binds the topic
    data = json.loads(env)
    data["c"] = data["c"][:-2] + ("AA" if data["c"][-2:] != "AA" else "BB")
    with pytest.raises(crypto.CryptoError):
        crypto.open_envelope(key, "ca-x", json.dumps(data))
    with pytest.raises(crypto.CryptoError):
        crypto.open_envelope(key, "ca-x", "not json")


def test_key_length_enforced():
    with pytest.raises(crypto.CryptoError):
        crypto.decode_key(crypto.b64u_encode(b"short"))


def test_envelope_vector():
    # Fixed-nonce vector: the Kotlin side must open exactly this envelope.
    env = json.dumps({"v": 1, "n": crypto.b64u_encode(bytes(12)), "c": ENVELOPE_C})
    assert crypto.open_envelope(VECTOR_KEY, "ca-test", env) == {"type": "ping", "txt": "zażółć"}
