"""No console window ever pops up on the PC while a hook runs (2026-11-12: an empty window hung for as long as a
permission question waited — Claude starts the launcher without a console since the exec-form hooks of 0.7.2, and
the venv's python.exe then got a visible one)."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_launcher(home: Path, monkeypatch):
    monkeypatch.setenv("HORNCALL_HOME", str(home))
    spec = importlib.util.spec_from_file_location("launcher_no_window", ROOT / "plugin" / "hooks" / "approver_launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher(approver_home, monkeypatch):
    approver_home.mkdir(parents=True, exist_ok=True)
    (approver_home / "state.json").write_text(json.dumps({"mode": "on", "on_until": None}))
    module = load_launcher(approver_home, monkeypatch)
    monkeypatch.setattr(module, "venv_python", lambda: Path(sys.executable))  # "exists"
    calls = []
    monkeypatch.setattr(module.subprocess, "call", lambda cmd, **kw: calls.append((cmd, kw)) or 0)
    monkeypatch.setattr(module.subprocess, "Popen", lambda cmd, **kw: calls.append((cmd, kw)))
    module.calls = calls
    return module


@pytest.mark.parametrize("argv", [["hook", "claude"], ["hook", "codex"], ["posttool", "claude"]])
def test_agent_calls_run_without_a_window_and_keep_their_streams(launcher, approver_home, monkeypatch, argv):
    if argv[0] == "posttool":  # posttool starts Python only while a question is open
        (approver_home / "pending").mkdir()
        (approver_home / "pending" / "x").write_text("")
    monkeypatch.setattr(sys, "argv", ["approver_launcher.py", *argv])
    launcher.main()
    (cmd, kw), = launcher.calls
    assert cmd[-len(argv):] == argv
    if os.name == "nt":
        assert kw["creationflags"] & launcher.CREATE_NO_WINDOW
        # The hook's own pipes go to the child: the question in, the answer out.
        assert all(name in kw for name in ("stdin", "stdout", "stderr"))
    else:
        assert "creationflags" not in kw


def test_background_events_never_get_a_console(launcher, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["approver_launcher.py", "event", "claude", "working"])
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": type("B", (), {"read": lambda self: b"{}"})()})())
    launcher.main()
    (cmd, kw), = launcher.calls
    if os.name == "nt":
        assert kw["creationflags"] & launcher.CREATE_NO_WINDOW and kw["stdout"] is launcher.subprocess.DEVNULL


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NO_WINDOW is Windows-only")
def test_a_windowless_child_really_gets_the_question_and_returns_the_answer(tmp_path):
    """Not mocked (Codex/Gemini review): a parent with piped streams, as an agent runs a hook, starts a child the
    way run_in_venv does for agent calls; the JSON goes in through stdin and the decision comes back on stdout."""
    import subprocess
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import importlib.util, subprocess, sys\n"
        f"spec = importlib.util.spec_from_file_location('l', r'{ROOT / 'plugin' / 'hooks' / 'approver_launcher.py'}')\n"
        "l = importlib.util.module_from_spec(spec); spec.loader.exec_module(l)\n"
        "child = 'import sys, json; q = json.load(sys.stdin); print(json.dumps({\"decision\": q[\"want\"]}))'\n"
        "sys.exit(subprocess.call([sys.executable, '-c', child], **l.agent_call_options()))\n", encoding="utf-8")
    out = subprocess.run([sys.executable, str(parent)], input='{"want": "allow", "text": "zażółć"}',
                         capture_output=True, text=True, timeout=60, env=dict(os.environ, HORNCALL_HOME=str(tmp_path)),
                         creationflags=0x08000000)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == {"decision": "allow"}


def test_a_missing_stream_is_logged_not_lost_silently(launcher, approver_home, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows-only path")
    monkeypatch.setattr(sys, "stdout", None)
    options = launcher.agent_call_options()
    assert options["stdout"] is launcher.subprocess.DEVNULL
    assert "agent-stream-missing" in (approver_home / "hook.log").read_text(encoding="utf-8")


def test_commands_typed_in_a_terminal_keep_their_console(launcher, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["approver_launcher.py", "status"])
    launcher.main()
    (cmd, kw), = launcher.calls
    assert "creationflags" not in kw  # `status`, `pair`…: the output must show in the user's terminal
