import io
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from approver import crypto, update


def package(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return buf.getvalue()


GOOD = {".claude-plugin/marketplace.json": "{}", "plugin/approver/__init__.py": "__version__ = '9.9.9'\n",
        "plugin/hooks/hooks.json": "{}", "setup.cmd": "@echo off\n"}


@pytest.fixture
def release_key(monkeypatch):
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(update, "RELEASE_PUBLIC_KEY", crypto.b64u_encode(pub))
    return lambda data: crypto.b64u_encode(key.sign(data))


def installed(tmp_path):
    root = tmp_path / "horncall"
    (root / "plugin" / "approver").mkdir(parents=True)
    (root / "plugin" / "approver" / "__init__.py").write_text("__version__ = '0.5.9'\n")
    (root / "plugin" / "approver" / "removed_module.py").write_text("old\n")
    (root / "plugin" / "hooks").mkdir()
    (root / "plugin" / "hooks" / "approver_launcher.py").write_text("# launcher\n")
    return root


def test_pip_and_marketplace_installs_point_to_their_own_updater(tmp_path, monkeypatch):
    said = []
    pip_root = tmp_path / "site-packages"
    (pip_root / "plugin" / "approver").mkdir(parents=True)
    assert update.run("https://x", pip_root, say=said.append) == 1 and "pipx upgrade horncall" in said[-1]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    cached = installed(tmp_path / ".claude" / "plugins" / "cache")
    assert update.run("https://x", cached, say=said.append) == 1 and "/plugin marketplace update" in said[-1]


def fake_relay(monkeypatch, version, sig):
    monkeypatch.setattr(update.Ntfy, "latest_versions",
                        lambda self, timeout=5: {"plugin": {"version": version, "zip": "z.zip", "sig": sig}})


def test_signed_release_replaces_the_code(tmp_path, monkeypatch, release_key):
    root = installed(tmp_path)
    data = package(GOOD)
    fake_relay(monkeypatch, "9.9.9", release_key(data))
    assert update.run("https://x", root, fetch=lambda url: data, say=lambda s: None) == 0
    assert "9.9.9" in (root / "plugin" / "approver" / "__init__.py").read_text()
    assert not (root / "plugin" / "approver" / "removed_module.py").exists()
    assert (root / "setup.cmd").exists()


def test_bad_signature_changes_nothing(tmp_path, monkeypatch, release_key):
    root = installed(tmp_path)
    data = package(GOOD)
    fake_relay(monkeypatch, "9.9.9", release_key(b"something else"))
    said = []
    assert update.run("https://x", root, fetch=lambda url: data, say=said.append) == 1
    assert "0.5.9" in (root / "plugin" / "approver" / "__init__.py").read_text()
    assert "NOT signed" in said[0]


def test_unsafe_paths_are_refused(tmp_path):
    with pytest.raises(update.UpdateError):
        update.install_zip(package(dict(GOOD, **{"../evil.txt": "x"})), installed(tmp_path))


def test_git_checkout_is_never_overwritten(tmp_path, monkeypatch):
    root = installed(tmp_path)
    (root / ".git").mkdir()
    assert update.run("https://x", root, fetch=lambda url: b"", say=lambda s: None) == 1


def test_up_to_date_does_nothing(tmp_path, monkeypatch):
    fake_relay(monkeypatch, "0.0.1", "x")
    assert update.run("https://x", installed(tmp_path), fetch=None, say=lambda s: None) == 0
