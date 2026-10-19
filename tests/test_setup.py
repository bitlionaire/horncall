import json
import subprocess
from pathlib import Path

from approver import setup
from approver.config import Config


def isolated(tmp_path, monkeypatch, found=("gemini",)):
    """A fake home: only the listed tools look installed; no real CLI on PATH."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for var, folder in (("GEMINI_CLI_HOME", ".gemini"), ("COPILOT_HOME", ".copilot"), ("CODEX_HOME", ".codex")):
        monkeypatch.setenv(var, str(tmp_path / folder))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    for folder in found:
        (tmp_path / f".{folder}").mkdir()


class AnsweringLink:
    def __init__(self, *a):
        pass

    def request_and_wait(self, request, deadline, accept=None):
        return {"decision": "allow", "via": "button"}


def run_setup(answers):
    said = []
    code = setup.run(ask=lambda q: answers.pop(0) if answers else "", say=said.append,
                     open_page=lambda url: True, link_factory=AnsweringLink, test_timeout=1)
    return code, "\n".join(said)


def test_detects_and_hooks_up_only_found_tools(tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch, found=("gemini", "codex"))
    code, out = run_setup(["y", "n", ""])  # gemini yes, codex no, Enter after scanning
    assert code == 0
    assert "horncall" in (tmp_path / ".gemini" / "settings.json").read_text()
    assert not (tmp_path / ".codex" / "hooks.json").exists()
    assert not (tmp_path / ".copilot" / "hooks").exists()
    assert "The phone answered" in out
    assert Config.load() is not None and Config.load().pairing_until


def test_running_again_keeps_the_key_and_skips_hooked_tools(tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch)
    run_setup(["y", ""])
    key = Config.load().key
    asked = []
    setup.run(ask=lambda q: asked.append(q) or "", say=lambda s: None, open_page=lambda u: True,
              link_factory=AnsweringLink, test_timeout=1)
    assert Config.load().key == key
    assert not any("Connect" in q for q in asked)  # gemini already hooked up
    entries = json.loads((tmp_path / ".gemini" / "settings.json").read_text())["hooks"]["BeforeTool"]
    assert len(entries) == 1


def test_claude_without_cli_gives_manual_steps(tmp_path, monkeypatch):
    isolated(tmp_path, monkeypatch, found=())
    (tmp_path / ".claude").mkdir()
    code, out = run_setup(["y", ""])
    assert "/plugin install horncall@horncall" in out


def test_claude_plugin_install_runs_both_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))  # not this PC's real Claude
    monkeypatch.setattr(setup.shutil, "which", lambda name: "claude")
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd[1:])
        return subprocess.CompletedProcess(cmd, 0, "", "")

    assert "restart" in setup.install_claude_plugin(run=fake_run)
    assert calls[0][:3] == ["plugin", "marketplace", "add"] and calls[1] == ["plugin", "install", "horncall@horncall"]
