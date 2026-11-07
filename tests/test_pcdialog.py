"""The PC window next to the phone for agents that show nothing while our hook waits (Codex, Gemini, Copilot)."""
import io
import json

from approver import crypto, hook, pcdialog, protocol
from approver.link import Link
from approver.config import Config, State
from test_hook import FakeTransport, _sent, make_link_factory, phone_answering

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
CODEX_BASH = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "session_id": "s1", "cwd": "/repo/app",
              "tool_input": {"command": "npm test", "description": "Run tests"}}


def setup(pc_dialog=True):
    # signed_only=False: the fake phone doesn't sign (signatures are tested in test_hook.py).
    Config(server="https://x", key=KEY, device_name="PC", pc_dialog=pc_dialog, signed_only=False).save()
    State(mode="on").save()


class FakeDialog:
    """Stands in for the window process: answers what the test says, records that it was closed."""
    opened: list = []

    def __init__(self, answer):
        self._answer, self.closed = answer, False

    def answer(self):
        return self._answer

    def close(self):
        self.closed = True


def fake_window(monkeypatch, answer):
    FakeDialog.opened = []

    def start(request, agent_title, folder):
        d = FakeDialog(answer)
        d.request, d.agent_title = request, agent_title
        FakeDialog.opened.append(d)
        return d

    monkeypatch.setattr(pcdialog, "start", start)


class SlowTransport(FakeTransport):
    """A phone that never answers; accepts the shorter stop check the window asks for."""

    def subscribe(self, topic, since, deadline, on_open=None, stop=None, stop_check_s=None):
        if on_open:
            on_open()
        if stop is not None:
            assert stop_check_s == 2  # a click on the PC must count within seconds
            assert stop()
        return iter(())


def run(agent, raw, transport):
    out = io.StringIO()
    hook.run(io.StringIO(json.dumps(raw)), out, link_factory=make_link_factory(transport), agent=agent)
    return out.getvalue()


def test_an_answer_on_the_pc_wins_and_takes_the_card_off_the_phone(monkeypatch):
    setup()
    fake_window(monkeypatch, "allow")
    transport = SlowTransport()
    out = json.loads(run("codex", CODEX_BASH, transport))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert [m["type"] for m in _sent(transport)] == ["request", "resolved"]
    (window,) = FakeDialog.opened
    assert window.closed and window.agent_title == "OpenAI Codex CLI" and window.request["title"].startswith("Bash")
    log = [json.loads(line) for line in hook.log_path().read_text(encoding="utf-8").splitlines()]
    assert any(e.get("event") == "response" and e.get("via") == "pc" for e in log)


def test_deny_from_the_pc(monkeypatch):
    setup()
    fake_window(monkeypatch, "deny")
    out = json.loads(run("codex", CODEX_BASH, SlowTransport()))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_off_duty_hands_the_order_back_and_pauses_this_pc(monkeypatch):
    setup()
    fake_window(monkeypatch, "offduty")
    transport = SlowTransport()
    assert run("codex", CODEX_BASH, transport) == ""  # no opinion: the AI asks in its own way
    assert [m["type"] for m in _sent(transport)] == ["request", "cancel"]  # the card leaves the phone
    state = State.load()
    assert not state.is_on() and state.paused_until > __import__("time").time() + 7000
    assert state.is_on(now=state.paused_until + 1)  # duty comes back by itself


def test_turning_windows_off_keeps_the_order_on_the_phone_and_the_app_turns_them_back_on(monkeypatch):
    setup()
    fake_window(monkeypatch, "off")
    out = json.loads(run("codex", CODEX_BASH, phone_answering("allow")))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "allow" and FakeDialog.opened[0].closed
    assert Config.load().pc_dialog is False
    run("codex", CODEX_BASH, phone_answering("allow"))
    assert len(FakeDialog.opened) == 1  # off: no new window

    def phone_with_switch(topic, body):  # the app sends its "windows on the PC" switch with the answer
        config = Config.load()
        link = Link(config.key, config.server, transport=transport)
        msg = crypto.open_envelope(config.key, topic, body) if topic == link.topic_req else None
        if msg and msg["type"] == "request":
            response = protocol.make_response(msg["id"], "allow", "button")
            response["pc_window"] = True
            transport.publish(link.topic_res, crypto.seal(config.key, link.topic_res, response))
    transport = FakeTransport(phone_with_switch)
    run("codex", CODEX_BASH, transport)
    assert Config.load().pc_dialog is True

def test_the_phone_answering_first_closes_the_window(monkeypatch):
    setup()
    fake_window(monkeypatch, None)
    out = json.loads(run("codex", CODEX_BASH, phone_answering("deny")))
    assert out["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert FakeDialog.opened[0].closed


def test_no_window_for_claude_questions_or_when_switched_off(monkeypatch):
    fake_window(monkeypatch, "allow")
    setup()
    claude = dict(CODEX_BASH, hook_event_name="PermissionRequest")
    run("claude", claude, phone_answering("allow"))  # Claude Code shows its own dialog alongside
    plan = dict(CODEX_BASH, tool_name="ExitPlanMode", tool_input={"plan": "x"})
    run("codex", plan, FakeTransport())  # an allow answers nothing for a plan
    setup(pc_dialog=False)
    run("codex", CODEX_BASH, phone_answering("allow"))
    assert FakeDialog.opened == []


def test_without_a_screen_the_terminal_asks_after_the_short_wait(monkeypatch):
    setup()
    waits = []

    class NoPhone(SlowTransport):
        def subscribe(self, topic, since, deadline, on_open=None, stop=None, stop_check_s=None):
            waits.append(deadline - __import__("time").time())
            if on_open:
                on_open()
            return iter(())

    # conftest sets HORNCALL_NO_PC_DIALOG=1: like an SSH session, no window can open
    config = Config.load()
    config.on_timeout = "deny"  # even so: the point of the short wait is the terminal's own question
    config.save()
    assert run("codex", CODEX_BASH, NoPhone()) == ""
    assert waits and waits[0] <= 121
    config.noscreen_wait_s = 0  # phone only
    config.save()
    out = run("codex", CODEX_BASH, NoPhone())
    assert waits[1] > 500 and json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_the_window_files_and_answers(tmp_path, monkeypatch):
    monkeypatch.delenv("HORNCALL_NO_PC_DIALOG", raising=False)
    monkeypatch.setattr(pcdialog.subprocess, "Popen", lambda *a, **k: None)  # no real process
    monkeypatch.setattr(pcdialog, "can_show", lambda: True)
    request = {"fp": "f" * 20, "id": "r1", "title": "Bash: npm test", "preview": "npm test", "risk": "normal"}
    dialog = pcdialog.start(request, "OpenAI Codex CLI", tmp_path)
    assert json.loads(dialog.order_file.read_text(encoding="utf-8"))["agent"] == "OpenAI Codex CLI"
    assert dialog.answer() is None
    dialog.answer_file.write_text("garbage")
    assert dialog.answer() is None
    dialog.answer_file.write_text("allow")
    assert dialog.answer() == "allow"
    dialog.close()
    assert not dialog.order_file.exists() and not dialog.answer_file.exists()


def test_no_window_without_a_screen(monkeypatch):
    monkeypatch.setenv("HORNCALL_NO_PC_DIALOG", "1")
    assert pcdialog.start({"fp": "a", "id": "b"}, "x", hook.pcdialog_dir()) is None
    monkeypatch.delenv("HORNCALL_NO_PC_DIALOG")
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 5 6.7.8.9 22")
    assert not pcdialog.can_show()
