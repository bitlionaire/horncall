import json
import os
import subprocess
import sys
from pathlib import Path

from approver import cli, install, setup


def fake_homes(tmp_path, monkeypatch):
    for var, folder in (("GEMINI_CLI_HOME", ".gemini"), ("COPILOT_HOME", ".copilot"), ("CODEX_HOME", ".codex")):
        monkeypatch.setenv(var, str(tmp_path / folder))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / ".claude"))


def test_print_shows_the_entries_and_writes_nothing(tmp_path, monkeypatch, capsys):
    fake_homes(tmp_path, monkeypatch)
    cli.main(["install", "codex", "--print"])
    out = capsys.readouterr().out
    assert "hooks.json" in out and '"PermissionRequest"' in out and "hook codex" in out
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_print_matches_what_install_writes(tmp_path, monkeypatch):
    fake_homes(tmp_path, monkeypatch)
    [(path, part)] = install.preview("gemini")
    install.install_gemini()
    written = json.loads(path.read_text())["hooks"]
    for event, [definition] in json.loads(part)["hooks"].items():
        assert definition in written[event]


def test_hooks_of_an_older_copy_are_detected_and_moved(tmp_path, monkeypatch):
    fake_homes(tmp_path, monkeypatch)
    install.install_codex()
    assert install.state("codex") == "here"
    path = install.codex_hooks_file()
    path.write_text(path.read_text().replace(str(install.LAUNCHER).replace("\\", "\\\\"), "C:\\\\Narzedzia\\\\old\\\\launcher.py"))
    assert install.state("codex") == "elsewhere"
    install.install_codex()
    assert install.state("codex") == "here"
    assert len(json.loads(path.read_text())["hooks"]["PermissionRequest"]) == 1


def test_setup_asks_before_changing_and_no_means_nothing(tmp_path, monkeypatch):
    fake_homes(tmp_path, monkeypatch)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(setup.shutil, "which", lambda name: None)
    (tmp_path / ".gemini").mkdir()
    answers = ["y", "n", ""]  # connect gemini, then "Continue?" → no, Enter after the QR

    class Link:
        def __init__(self, *a):
            pass

        def request_and_wait(self, *a, **k):
            return None

    said = []
    setup.run(ask=lambda q: answers.pop(0) if answers else "", say=said.append, open_page=lambda u: True,
              link_factory=Link, test_timeout=1)
    text = "\n".join(said)
    assert "This will change" in text and "settings.json" in text and "Nothing changed" in text
    assert not (tmp_path / ".gemini" / "settings.json").exists()


def as_pipx_install(tmp_path, monkeypatch) -> Path:
    """This install as pip/pipx sees it: no launcher next to the package, a `horncall` script in the venv."""
    monkeypatch.setattr(install, "LAUNCHER", tmp_path / "nowhere" / "approver_launcher.py")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    scripts = tmp_path / "venv" / ("Scripts" if os.name == "nt" else "bin")
    scripts.mkdir(parents=True)
    exe = scripts / ("horncall.exe" if os.name == "nt" else "horncall")
    exe.write_text("")
    return exe


def test_pipx_install_hooks_run_the_horncall_command(tmp_path, monkeypatch):
    fake_homes(tmp_path, monkeypatch)
    exe = as_pipx_install(tmp_path, monkeypatch)
    assert install.packaged() and install.target() == exe
    assert install.hook_command("codex", "event codex idle").endswith("horncall" + (".exe" if os.name == "nt" else "")
                                                                      + " event codex idle")
    install.install_codex()
    assert install.state("codex") == "here"


def test_install_claude_adds_the_plugin_hooks_to_settings_and_keeps_the_rest(tmp_path, monkeypatch):
    fake_homes(tmp_path, monkeypatch)
    as_pipx_install(tmp_path, monkeypatch)
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    other = {"type": "command", "command": "my-linter --check"}
    settings.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [other]}]}}))
    install.install_claude()
    install.install_claude()  # twice: still one of ours per event
    data = json.loads(settings.read_text())
    assert data["model"] == "opus" and set(data["hooks"]) == set(install.CLAUDE_HOOKS)
    assert [h["command"] for e in data["hooks"]["Stop"] for h in e["hooks"]].count(other["command"]) == 1
    assert len(install.claude_commands()) == 4 and install.state("claude") == "here"
    assert "name" not in json.dumps(data["hooks"])  # Claude's settings have no name field for hooks
    install.uninstall_claude()
    assert json.loads(settings.read_text())["hooks"] == {"Stop": [{"hooks": [other]}]}


def test_horncall_command_hooks_cost_nothing_while_off(capsys):
    from approver import entry
    assert entry.main(["hook", "claude"]) == 0 and entry.main(["posttool", "claude"]) == 0
    assert capsys.readouterr().out == ""


def test_statusline_notes_claude_limits_for_the_phone(approver_home, monkeypatch, capsys):
    import io
    from approver import statusline
    payload = {"session_id": "s1", "rate_limits": {"five_hour": {"used_percentage": 40}},
               "context_window": {"used_percentage": 12, "remaining_percentage": 88}}
    monkeypatch.setattr(sys, "stdin", type("In", (), {"buffer": io.BytesIO(json.dumps(payload).encode())})())
    assert statusline.main() == 0
    assert "5h 40%" in capsys.readouterr().out
    assert json.loads((approver_home / "claude-vitals.json").read_text())["s1"]["ctx_left"] == 88


def test_launcher_copies_a_downloaded_package_to_the_app_folder(tmp_path):
    root = Path(install.PLUGIN_ROOT).parent
    download = tmp_path / "Downloads" / "horncall"
    for name in (".claude-plugin", "plugin"):
        subprocess.run([sys.executable, "-c", f"import shutil; shutil.copytree(r'{root / name}', r'{download / name}', "
                        "ignore=shutil.ignore_patterns('__pycache__','.hypothesis'))"], check=True)
    app = tmp_path / "app"
    code = (f"import sys, os; os.environ['HORNCALL_APP_DIR']=r'{app}'; sys.path.insert(0, r'{download / 'plugin' / 'hooks'}');"
            "import approver_launcher as l; print(l.install_copy())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
    assert Path(out) == app
    assert (app / "plugin" / "approver" / "setup.py").exists() and (app / ".claude-plugin" / "marketplace.json").exists()
    # The developer copy (with .git) and the app folder itself run in place.
    again = subprocess.run([sys.executable, "-c", code.replace(str(download / "plugin" / "hooks"), str(app / "plugin" / "hooks"))],
                           capture_output=True, text=True).stdout.strip()
    assert again == "None"
