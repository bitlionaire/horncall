"""Trusted phones: listing, forgetting an old one, and when each last answered."""
import types

import pytest

from approver import cli, crypto, phones
from approver.config import Config

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def config_with(pubs):
    c = Config(server="https://x", key=KEY, device_name="PC", phones=[{"pub": p, "added": 1_790_000_000} for p in pubs])
    c.save()
    return c


@pytest.fixture
def pubs():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import base64
    out = []
    for _ in range(3):
        der = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        out.append(base64.urlsafe_b64encode(der).rstrip(b"=").decode())
    return out


def test_forget_one_by_the_start_of_its_key(pubs, capsys):
    config_with(pubs[:2])
    old = crypto.fingerprint(pubs[0])
    cli.cmd_phones(types.SimpleNamespace(action="forget", key=old[:5].lower()))
    assert [p["pub"] for p in Config.load().phones] == [pubs[1]]
    assert "Forgotten" in capsys.readouterr().out


def test_never_the_last_phone_and_no_guessing(pubs):
    config_with(pubs[:1])
    with pytest.raises(SystemExit):
        cli.cmd_phones(types.SimpleNamespace(action="forget", key=crypto.fingerprint(pubs[0])))
    assert len(Config.load().phones) == 1
    config_with(pubs[:3])
    with pytest.raises(SystemExit):
        cli.cmd_phones(types.SimpleNamespace(action="forget", key=""))  # matches nothing on purpose
    assert len(Config.load().phones) == 3


def test_list_shows_added_and_last_answer(pubs, capsys):
    config_with(pubs[:2])
    phones._note_use(pubs[1], 1_800_000_000)
    phones._note_use(pubs[1], 1_800_000_100)  # within the hour: not written again
    assert Config.load().phones[1]["last"] == 1_800_000_000
    phones._note_use(pubs[1], 1_800_004_000)
    assert Config.load().phones[1]["last"] == 1_800_004_000
    cli.cmd_phones(types.SimpleNamespace(action="list", key=None))
    out = capsys.readouterr().out
    assert crypto.fingerprint(pubs[0]) in out and "last answer —" in out
