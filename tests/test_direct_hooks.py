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
