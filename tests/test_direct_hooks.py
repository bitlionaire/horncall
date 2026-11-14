"""ZIP installs on Windows: Claude's hooks.json in exec form (Python started directly, no Git Bash), item 6 of the
0.7.2 plan. Git Bash hung for the whole 15 s timeout on the owner's PC (2026-11-10/11): no status, no approval
reached the phone."""

import json
import os
import sys
from pathlib import Path

import pytest

from approver import install, update

ROOT = Path(__file__).resolve().parents[1]
windows_only = pytest.mark.skipif(os.name != "nt", reason="the direct form is written on Windows only")


def test_exec_form_mirrors_the_settings_hooks():
    python, launcher = Path("C:/Py/python.exe"), Path("C:/Horncall/app/plugin/hooks/approver_launcher.py")
    hooks = install.plugin_exec_hooks(python, launcher)["hooks"]
    assert set(hooks) == set(install.CLAUDE_HOOKS)
    for event, (matcher, args, timeout) in install.CLAUDE_HOOKS.items():
        group = hooks[event][0]
        hook = group["hooks"][0]
        assert hook["command"] == str(python) and hook["args"] == [str(launcher), *args.split()]
        assert hook["timeout"] == timeout and group.get("matcher") == matcher
        # Task start/end only report: never hold up the prompt. Permissions must block.
        assert hook.get("async", False) == (event in install.CLAUDE_ASYNC)


def test_shipped_hooks_report_task_state_in_the_background():
    shipped = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event, entries in shipped.items():
        assert entries[0]["hooks"][0].get("async", False) == (event in install.CLAUDE_ASYNC), event


def test_direct_hooks_python_tells_the_two_forms_apart(tmp_path):
    shell_form = tmp_path / "shell.json"
    shell_form.write_text((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert install.direct_hooks_python(shell_form) is None
    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps(install.plugin_exec_hooks(Path("C:/Py/python.exe"), Path("C:/x/launcher.py"))))
    assert install.direct_hooks_python(direct) == Path("C:/Py/python.exe")
    assert install.direct_hooks_python(tmp_path / "missing.json") is None


def fake_install(tmp_path, monkeypatch, source_path=None):
    """A ZIP install at tmp/app (shell-form hooks.json) and Claude's copy of it in its plugin cache."""
    root = tmp_path / "app"
    hooks_dir = root / "plugin" / "hooks"
    hooks_dir.mkdir(parents=True)
    (hooks_dir / "approver_launcher.py").write_text("# launcher\n")
    shipped = (ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8")
    (hooks_dir / "hooks.json").write_text(shipped, encoding="utf-8")
    claude = tmp_path / ".claude"
    cache = claude / "plugins" / "cache" / "horncall" / "horncall" / "0.7.2"
    (cache / "hooks").mkdir(parents=True)
    (cache / "hooks" / "hooks.json").write_text(shipped, encoding="utf-8")
    (claude / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"horncall": {"source": {"source": "directory", "path": str(source_path or root)}}}))
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"version": 2, "plugins": {"horncall@horncall": [{"scope": "user", "installPath": str(cache), "version": "0.7.2"}]}}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    return root, cache


def test_claude_copy_only_for_a_marketplace_in_this_folder(tmp_path, monkeypatch):
    root, cache = fake_install(tmp_path, monkeypatch)
    assert install.claude_plugin_copy(root) == cache
    other, _ = fake_install(tmp_path / "b", monkeypatch, source_path=tmp_path / "elsewhere")
    assert install.claude_plugin_copy(other) is None
    known = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "known_marketplaces.json"
    known.write_text(json.dumps({"horncall": {"source": {"source": "github", "repo": "bitlionaire/horncall"}}}))
    assert install.claude_plugin_copy(other) is None  # GitHub install: Claude's own business


def test_the_zip_install_is_found_from_claudes_cached_copy(tmp_path, monkeypatch):
    """R4 (review of 0.7.3): run from Claude's cache the install looked like "pip" and nothing was repaired.
    Real folders, no monkeypatched how_installed: the repo itself is a git checkout, the ZIP install is in tmp."""
    root, cache = fake_install(tmp_path, monkeypatch)
    assert update.how_installed(update.INSTALL_ROOT) == "git"
    assert install.zip_install_root() == root
    known = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "plugins" / "known_marketplaces.json"
    known.write_text(json.dumps({"horncall": {"source": {"source": "github", "repo": "bitlionaire/horncall"}}}))
    assert install.zip_install_root() is None  # a GitHub install is Claude's own business


@windows_only
def test_ensure_repairs_claudes_copy_when_run_from_the_cache(tmp_path, monkeypatch):
    root, cache = fake_install(tmp_path, monkeypatch)
    monkeypatch.setattr(install, "hook_python", lambda: Path(sys.executable).resolve())
    written = install.ensure_direct_hooks()
    assert cache / "hooks" / "hooks.json" in written
    assert install.direct_hooks_python(cache / "hooks" / "hooks.json") == Path(sys.executable).resolve()


def test_status_judges_the_hooks_claude_really_runs(tmp_path, monkeypatch, capsys):
    """R8: the ZIP folder had direct hooks, Claude's copy was still in the shell form, Git Bash was missing — and
    status stayed quiet."""
    from approver import cli, crypto, setup
    from approver.config import Config, State
    root, cache = fake_install(tmp_path, monkeypatch)
    python = Path(sys.executable).resolve()
    (root / "plugin" / "hooks" / "hooks.json").write_text(
        json.dumps(install.plugin_exec_hooks(python, root / "plugin" / "hooks" / "approver_launcher.py")))
    Config(key=crypto.b64u_encode(b"k" * 32)).save()
    State(mode="on").save()
    monkeypatch.setattr(setup, "claude_plugin_installed", lambda: True)
    monkeypatch.setattr(setup, "git_bash_found", lambda: False)
    monkeypatch.setattr(cli.Ntfy, "latest_versions", lambda self, timeout=5: {})
    monkeypatch.setattr(cli.Ntfy, "ping", lambda self, timeout=5: True, raising=False)
    cli.main(["status"])
    assert "Git Bash" in capsys.readouterr().out
    # The other way round: Claude's copy runs Python directly, so a missing Git Bash doesn't matter.
    (cache / "hooks" / "hooks.json").write_text(
        json.dumps(install.plugin_exec_hooks(python, root / "plugin" / "hooks" / "approver_launcher.py")))
    cli.main(["status"])
    assert "Git Bash" not in capsys.readouterr().out


@windows_only
def test_hooks_left_on_pythonw_by_an_older_version_are_rewritten(tmp_path, monkeypatch):
    """Review of 0.8.0: 0.7.2/0.7.3 could write pythonw.exe from a background process; ensure must fix it."""
    root, cache = fake_install(tmp_path, monkeypatch)
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_text("")
    for target in (root / "plugin" / "hooks" / "hooks.json", cache / "hooks" / "hooks.json"):
        target.write_text(json.dumps(install.plugin_exec_hooks(pythonw, root / "plugin" / "hooks" / "approver_launcher.py")))
    monkeypatch.setattr(install, "hook_python", lambda: Path(sys.executable).resolve())
    assert len(install.ensure_direct_hooks()) == 2
    assert install.direct_hooks_python(cache / "hooks" / "hooks.json").name.lower() == "python.exe"


def test_hooks_never_get_pythonw(tmp_path, monkeypatch):
    """R6: the background processes run under pythonw.exe; the permission hooks they write must use python.exe."""
    (tmp_path / "python.exe").write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "pythonw.exe"), raising=False)
    assert install.hook_python().name == "python.exe"


@pytest.mark.skipif(os.name == "nt", reason="non-Windows branch")
def test_nothing_is_written_outside_windows(tmp_path, monkeypatch):
    root, _ = fake_install(tmp_path, monkeypatch)
    assert install.write_direct_hooks(root, Path(sys.executable)) == []
    assert install.direct_hooks_python(root / "plugin" / "hooks" / "hooks.json") is None


@windows_only
def test_zip_install_and_claude_copy_get_the_direct_form(tmp_path, monkeypatch):
    root, cache = fake_install(tmp_path, monkeypatch)
    python = Path(sys.executable).resolve()
    written = install.write_direct_hooks(root, python)
    assert set(written) == {root / "plugin" / "hooks" / "hooks.json", cache / "hooks" / "hooks.json"}
    for path in written:
        assert install.direct_hooks_python(path) == python
        hook = json.loads(path.read_text(encoding="utf-8"))["hooks"]["PermissionRequest"][0]["hooks"][0]
        assert hook["args"][0] == str(root / "plugin" / "hooks" / "approver_launcher.py")
    # A missing Python or launcher writes nothing (the shell form stays: better than hooks that can't start).
    assert install.write_direct_hooks(root, tmp_path / "no-python.exe") == []


@windows_only
def test_ensure_restores_the_direct_form_after_an_old_update(tmp_path, monkeypatch):
    root, cache = fake_install(tmp_path, monkeypatch)
    monkeypatch.setattr(update, "INSTALL_ROOT", root)
    monkeypatch.setattr(update, "how_installed", lambda r=None: "zip")
    monkeypatch.setattr(install, "hook_python", lambda: Path(sys.executable).resolve())
    assert len(install.ensure_direct_hooks()) == 2  # both still in the shell form
    assert install.ensure_direct_hooks() == []  # nothing to do any more
    # Claude refreshed its copy from the package: only that one is put back.
    (cache / "hooks" / "hooks.json").write_text((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    assert len(install.ensure_direct_hooks()) == 2 and install.direct_hooks_python(cache / "hooks" / "hooks.json")
    # Other install kinds are never touched.
    monkeypatch.setattr(update, "how_installed", lambda r=None: "marketplace")
    (root / "plugin" / "hooks" / "hooks.json").write_text("{}")
    assert install.ensure_direct_hooks() == []
