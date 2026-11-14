"""Task reports are sent in the background and can be late (plugin 0.7.2+). Code review of 0.7.3:
R1 - a late "working"/"idle" must not drop a question asked after the event was called."""

import json
import time

from approver import hook
from approver.config import Config
from approver import crypto


class FakeLink:
    sent: list = []

    def __init__(self, *args, **kwargs):
        pass

    def send_resolved(self, fp, request_id=None):
        FakeLink.sent.append(("resolved", fp, request_id))

    def send_status(self, status):
        FakeLink.sent.append(("status", status))


def marker(fp, session, request_id, t):
    path = hook._pending_marker(fp, session, request_id, "Bash")
    data = json.loads(path.read_text(encoding="utf-8"))
    data["t"] = t
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_a_late_working_keeps_questions_asked_after_it_was_called():
    FakeLink.sent = []
    config = Config(key=crypto.b64u_encode(b"k" * 32))
    called_at = time.time() - 5
    old = marker("aaa", "s1", "old", called_at - 10)
    new = marker("bbb", "s1", "new", called_at + 2)  # asked while the "working" waited in the background
    hook._resolve_session_markers("s1", "claude", config, FakeLink, before=called_at)
    assert not old.exists() and new.exists()
    assert [s[2] for s in FakeLink.sent] == ["old"]


def test_a_late_idle_keeps_questions_asked_after_it_was_called():
    called_at = time.time() - 5
    old = marker("ccc", "s2", "old", called_at - 10)
    new = marker("ddd", "s2", "new", called_at + 2)
    hook._clear_session_markers("s2", before=called_at)
    assert not old.exists() and new.exists()


def test_pip_background_sender_can_import_a_user_install(monkeypatch, tmp_path):
    """R2: `python -I` ignored the user site-packages, so `pip install --user` never sent a task report."""
    import subprocess
    import sys
    from approver import entry
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda cmd, **kw: started.append((cmd, kw)))
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": type("B", (), {"read": lambda self: b"{}"})()})())
    entry._event_in_background(["event", "codex", "idle"])
    (cmd, kw), = started
    assert "-I" not in cmd and cmd[-4:] == ["approver", "event", "codex", "idle"]
    from approver.config import home_dir
    assert kw["cwd"] == str(home_dir())  # never the project folder: a planted json.py there stays out of reach


def run_event_at(monkeypatch, state, called_at, session="s9"):
    import io
    from approver.config import State
    monkeypatch.setenv("HORNCALL_EVENT_T", repr(called_at))
    return hook.run_event(io.StringIO(json.dumps({"session_id": session, "cwd": "/p"})), "claude", state, FakeLink)


def ready(approver_home):
    from approver.config import State
    Config(key=crypto.b64u_encode(b"k" * 32)).save()
    State(mode="on").save()


def test_a_late_idle_still_reports_its_task_and_keeps_the_next_turns_start(monkeypatch, approver_home):
    """R3: Stop and UserPromptSubmit milliseconds apart; the background senders raced and the start went first."""
    ready(approver_home)
    FakeLink.sent = []
    t0 = time.time() - 100
    run_event_at(monkeypatch, "working", t0)  # turn 1 starts
    run_event_at(monkeypatch, "working", t0 + 60.002)  # turn 2 starts, handled first...
    run_event_at(monkeypatch, "idle", t0 + 60.001)  # ...turn 1's end arrives late
    statuses = [s[1] for s in FakeLink.sent if s[0] == "status"]
    assert [s["state"] for s in statuses] == ["working", "working", "idle"]
    late = statuses[-1]
    assert late["worked_s"] == 60  # turn 1 is closed with its real length
    assert late["seq"] < statuses[1]["seq"]  # the phone keeps turn 2's start
    # Turn 2 still has its own start: its end reports 30 s, not 90.
    run_event_at(monkeypatch, "idle", t0 + 90.002)
    assert FakeLink.sent[-1][1]["worked_s"] == 30


def test_a_start_in_the_same_millisecond_as_the_end_is_newer(monkeypatch, approver_home):
    ready(approver_home)
    FakeLink.sent = []
    t = time.time() - 10
    run_event_at(monkeypatch, "idle", t)
    run_event_at(monkeypatch, "working", t)
    a, b = [s[1] for s in FakeLink.sent if s[0] == "status"]
    assert b["seq"] > a["seq"] and b["state"] == "working"


def test_a_late_start_is_still_dropped(monkeypatch, approver_home):
    ready(approver_home)
    FakeLink.sent = []
    t = time.time() - 10
    run_event_at(monkeypatch, "idle", t + 5)
    run_event_at(monkeypatch, "working", t)  # older start arriving after the end: nothing to report
    assert [s[1]["state"] for s in FakeLink.sent if s[0] == "status"] == ["idle"]


def test_hook_input_with_a_byte_order_mark_is_read(monkeypatch, approver_home):
    import io
    ready(approver_home)
    FakeLink.sent = []
    monkeypatch.setenv("HORNCALL_EVENT_T", repr(time.time()))
    hook.run_event(io.StringIO("﻿" + json.dumps({"session_id": "bom", "cwd": "/p"})), "claude", "working", FakeLink)
    assert [s[1]["state"] for s in FakeLink.sent if s[0] == "status"] == ["working"]


def test_markers_keep_milliseconds():
    path = hook._pending_marker("eee", "s3", "id", "Bash")
    assert isinstance(json.loads(path.read_text(encoding="utf-8"))["t"], float)
