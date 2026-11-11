"""Task start/end statuses are sent in the background (0.7.2): the agent's prompt never waits on them."""

import importlib.util
import io
import json
import subprocess
from pathlib import Path

from approver import entry, hook, install

ROOT = Path(__file__).resolve().parent.parent


def _on(approver_home):
    from approver.config import Config, State
    Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC").save()
    State(mode="on").save()


def _fake_link(sent):
    class FakeLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            sent.append(status)
    return FakeLink


def _load_launcher():
    spec = importlib.util.spec_from_file_location("launcher_bg", ROOT / "plugin" / "hooks" / "approver_launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    return launcher


class _Stdin:
    def __init__(self, data: bytes):
        self.buffer = io.BytesIO(data)


def test_claude_task_hooks_are_async_and_permission_hooks_block(tmp_path, monkeypatch):
    plugin = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    monkeypatch.setattr(install, "claude_settings", lambda: tmp_path / "settings.json")
    installed = install.claude_definitions()
    for hooks in (plugin, {k: [v] for k, v in installed.items()}):
        for event in ("UserPromptSubmit", "Stop"):
            assert all(h.get("async") is True for group in hooks[event] for h in group["hooks"])
        for event in ("PermissionRequest", "PostToolUse", "PostToolUseFailure"):
            assert not any(h.get("async") for group in hooks[event] for h in group["hooks"])


def test_launcher_hands_the_event_to_a_detached_process_and_returns(approver_home, monkeypatch, tmp_path):
    _on(approver_home)
    launcher = _load_launcher()
    spawned = []
    monkeypatch.setattr(launcher, "venv_python", lambda: tmp_path)  # "the venv exists"
    monkeypatch.setattr(launcher, "run_in_venv", lambda args: (_ for _ in ()).throw(AssertionError("blocked")))
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
    monkeypatch.setattr(launcher.sys, "stdin", _Stdin(b'{"session_id": "s1"}'))
    monkeypatch.setattr(launcher.sys, "argv", ["approver_launcher.py", "event", "claude", "working"])
    assert launcher.main() == 0
    [(cmd, kw)] = spawned
    assert cmd[-4:] == ["approver", "event", "claude", "working"]
    assert kw["stdin"] is subprocess.DEVNULL and ("creationflags" in kw or kw.get("start_new_session"))
    inbox = Path(kw["env"]["HORNCALL_EVENT_INPUT"])
    assert inbox.read_bytes() == b'{"session_id": "s1"}'
    assert float(kw["env"]["HORNCALL_EVENT_T"]) > 0
    log = (approver_home / "hook.log").read_text(encoding="utf-8")
    assert '"event": "event-called"' in log and '"state": "working"' in log


def test_launcher_with_the_phone_off_spawns_nothing(approver_home, monkeypatch, tmp_path):
    launcher = _load_launcher()
    monkeypatch.setattr(launcher, "venv_python", lambda: tmp_path)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(launcher.sys, "argv", ["approver_launcher.py", "event", "claude", "working"])
    assert launcher.main() == 0


def test_pip_entry_hands_the_event_to_a_detached_process(approver_home, monkeypatch):
    _on(approver_home)
    spawned = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: spawned.append((cmd, kw)))
    monkeypatch.setattr(entry.sys, "stdin", _Stdin(b"{}"))
    monkeypatch.setattr(entry.sys, "argv", ["horncall", "event", "codex", "idle"])
    assert entry.main() == 0
    [(cmd, kw)] = spawned
    assert cmd[1:] == ["-I", "-m", "approver", "event", "codex", "idle"]
    assert Path(kw["env"]["HORNCALL_EVENT_INPUT"]).read_bytes() == b"{}"


def test_background_run_reads_its_input_file_deletes_it_and_sends(approver_home, monkeypatch):
    _on(approver_home)
    sent = []
    inbox = approver_home / "events"
    inbox.mkdir(parents=True)
    path = inbox / "1.json"
    path.write_text(json.dumps({"session_id": "s5", "cwd": "/repo/app"}), encoding="utf-8")
    monkeypatch.setenv("HORNCALL_EVENT_INPUT", str(path))
    monkeypatch.setenv("HORNCALL_EVENT_T", "1800000000.5")
    real_run = hook.run_event
    monkeypatch.setattr(hook, "run_event", lambda stdin, agent, state: real_run(stdin, agent, state, _fake_link(sent)))
    assert hook.main_event("claude", "working") == 0
    assert not path.exists()
    [status] = sent
    assert status["state"] == "working" and status["since"] == 1800000000 and status["project"] == "app"


def test_a_late_start_never_overwrites_a_newer_end(approver_home, monkeypatch):
    """The start's background process lost the race: the end (called later) was already sent."""
    _on(approver_home)
    sent = []
    payload = json.dumps({"session_id": "s6", "cwd": "/repo/app"})
    monkeypatch.setenv("HORNCALL_EVENT_T", "1800000100")
    hook.run_event(io.StringIO(payload), "claude", "idle", link_factory=_fake_link(sent))
    monkeypatch.setenv("HORNCALL_EVENT_T", "1800000000")
    hook.run_event(io.StringIO(payload), "claude", "working", link_factory=_fake_link(sent))
    assert [s["state"] for s in sent] == ["idle"]
    assert '"event": "status-stale"' in (approver_home / "hook.log").read_text(encoding="utf-8")
    monkeypatch.setenv("HORNCALL_EVENT_T", "1800000200")  # the next real task still reports
    hook.run_event(io.StringIO(payload), "claude", "working", link_factory=_fake_link(sent))
    assert [s["state"] for s in sent] == ["idle", "working"]


def test_a_failed_send_is_logged_not_crashed(approver_home):
    """log("error", event=…) used to raise TypeError (two values for `event`): failures left no trace."""
    _on(approver_home)

    class DownLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            raise OSError("relay down")

        def resolve(self, *a, **kw):
            raise OSError("relay down")

    assert hook.run_event(io.StringIO('{"session_id": "s8"}'), "claude", "working", link_factory=DownLink) == 0
    log = (approver_home / "hook.log").read_text(encoding="utf-8")
    assert '"event": "error", "during": "status", "error": "OSError"' in log


def test_status_lock_is_exclusive_and_released(approver_home):
    with hook._status_lock():
        with hook._status_lock(wait_s=0.2) as second:
            assert second.file is None  # could not get it: carried on without
    with hook._status_lock(wait_s=0.2) as again:
        assert again.file is not None


def test_leftover_inputs_are_swept(approver_home):
    import os
    inbox = approver_home / "events"
    inbox.mkdir(parents=True)
    old, fresh = inbox / "old.json", inbox / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    os.utime(old, (1, 1))
    hook._sweep_events(inbox)
    assert not old.exists() and fresh.exists()
