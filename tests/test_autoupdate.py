"""Automatic plugin updates: Claude Code's own switch for GitHub marketplace installs, a daily signed
update for ZIP installs (autoupdate.py, the launcher's maybe_auto_update)."""

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from approver import autoupdate, crypto, update
from approver.config import Config

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def claude_dir(tmp_path, monkeypatch):
    d = tmp_path / ".claude"
    (d / "plugins").mkdir(parents=True)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    return d


def known(claude_dir, source, auto=None):
    entry = {"source": source, "installLocation": "x"}
    if auto is not None:
        entry["autoUpdate"] = auto
    (claude_dir / "plugins" / "known_marketplaces.json").write_text(json.dumps({"horncall": entry}))


def test_github_marketplace_switch_goes_to_claude_settings(claude_dir, monkeypatch):
    monkeypatch.setattr(update, "how_installed", lambda root=None: "marketplace")
    known(claude_dir, {"source": "github", "repo": "bitlionaire/horncall"})
    settings = claude_dir / "settings.json"
    settings.write_text(json.dumps({"model": "opus", "extraKnownMarketplaces": {"other": {"source": {"source": "github", "repo": "a/b"}}}}))
    assert autoupdate.kind() == "marketplace" and not autoupdate.is_on() and autoupdate.describe() == "off"

    assert "ON" in autoupdate.switch(True)
    data = json.loads(settings.read_text())
    assert data["extraKnownMarketplaces"]["horncall"] == {
        "source": {"source": "github", "repo": "bitlionaire/horncall"}, "autoUpdate": True}
    assert data["model"] == "opus" and "other" in data["extraKnownMarketplaces"]  # the rest stays
    assert (claude_dir / "settings.json.horncall-backup").exists()
    assert autoupdate.is_on()

    autoupdate.switch(False)
    assert json.loads(settings.read_text())["extraKnownMarketplaces"]["horncall"]["autoUpdate"] is False
    assert not autoupdate.is_on()


def test_the_plugin_panel_toggle_counts_when_settings_say_nothing(claude_dir, monkeypatch):
    monkeypatch.setattr(update, "how_installed", lambda root=None: "marketplace")
    known(claude_dir, {"source": "github", "repo": "bitlionaire/horncall"}, auto=True)
    assert autoupdate.is_on()


def test_a_local_marketplace_copy_is_left_alone(claude_dir, monkeypatch):
    monkeypatch.setattr(update, "how_installed", lambda root=None: "marketplace")
    known(claude_dir, {"source": "directory", "path": "C:/Horncall/app"})
    assert autoupdate.kind() == "local"
    assert "local copy" in autoupdate.switch(True)
    assert not (claude_dir / "settings.json").exists()


def test_zip_install_keeps_the_switch_in_its_config(monkeypatch, approver_home):
    monkeypatch.setattr(update, "how_installed", lambda root=None: "zip")
    assert "Pair the phone first" in autoupdate.switch(True)
    Config(key=crypto.b64u_encode(b"k" * 32)).save()
    assert not autoupdate.is_on()
    assert "once a day" in autoupdate.switch(True)
    assert Config.load().auto_update and autoupdate.is_on()


def load_launcher(home):
    os.environ["HORNCALL_HOME"] = str(home)
    spec = importlib.util.spec_from_file_location("launcher_under_test", ROOT / "plugin" / "hooks" / "approver_launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_launcher_checks_at_most_once_a_day_and_only_when_switched_on(approver_home, monkeypatch):
    launcher = load_launcher(approver_home)
    started = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: started.append(cmd))
    approver_home.mkdir(parents=True, exist_ok=True)

    (approver_home / "config.json").write_text(json.dumps({"key": "x", "auto_update": False}))
    launcher.maybe_auto_update()
    assert started == []

    (approver_home / "config.json").write_text(json.dumps({"key": "x", "auto_update": True}))
    launcher.maybe_auto_update()
    assert len(started) == 1 and started[0][-3:] == ["approver", "update", "--auto"][-3:] and "--auto" in started[0]
    launcher.maybe_auto_update()  # same day: nothing more
    assert len(started) == 1

    stamp = approver_home / "autoupdate-checked"
    old = time.time() - 25 * 3600
    os.utime(stamp, (old, old))
    launcher.maybe_auto_update()
    assert len(started) == 2
