from pathlib import Path

from approver import config


def test_old_home_is_moved_to_horncall(tmp_path, monkeypatch):
    monkeypatch.delenv("HORNCALL_HOME", raising=False)
    monkeypatch.delenv("ZUGZUG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    old = tmp_path / ".zug-zug"
    old.mkdir()
    (old / "config.json").write_text("{}")
    home = config.home_dir()
    assert home == tmp_path / ".horncall"
    assert (home / "config.json").exists() and not old.exists()


def test_fresh_install_uses_horncall(tmp_path, monkeypatch):
    monkeypatch.delenv("HORNCALL_HOME", raising=False)
    monkeypatch.delenv("ZUGZUG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert config.home_dir() == tmp_path / ".horncall"


def test_env_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("HORNCALL_HOME", str(tmp_path / "x"))
    assert config.home_dir() == tmp_path / "x"
