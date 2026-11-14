"""0.8.0: Horncall costs its users as few AI tokens as possible (owner 2026-11-13)."""
import io
import json
import re
import sys
from pathlib import Path

from approver import cli, hook
from approver.config import Config
from test_hook import HOOK_INPUT, make_link_factory, phone_answering, run_hook, setup

SKILL = Path(__file__).resolve().parents[1] / "plugin" / "skills" / "approver" / "SKILL.md"


def test_phone_deny_stops_claude_by_default():
    setup()
    decision = run_hook(phone_answering("deny", "button"))["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and decision["interrupt"] is True
    assert "button" in decision["message"] and "wait for the user" in decision["message"]


def test_deny_stops_off_lets_the_rest_go_on_without_workarounds(capsys):
    setup()
    cli.main(["deny-stops", "off"])
    assert "off" in capsys.readouterr().out
    decision = run_hook(phone_answering("deny", "button"))["hookSpecificOutput"]["decision"]
    assert "interrupt" not in decision and "work around" in decision["message"] and "rest of the task" in decision["message"]


def test_timeout_deny_does_not_interrupt():
    setup(timeout_s=0, on_timeout="deny", deny_stops=True)
    from test_hook import FakeTransport
    decision = run_hook(FakeTransport())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and "interrupt" not in decision


def test_codebuddy_gets_no_interrupt_field():
    setup(deny_stops=True)
    out = io.StringIO()
    raw = dict(HOOK_INPUT)
    assert hook.run(io.StringIO(json.dumps(raw)), out, make_link_factory(phone_answering("deny", "button")),
                    agent="codebuddy") == 0
    decision = json.loads(out.getvalue())["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and "interrupt" not in decision


def test_pair_open_shows_the_qr_in_the_browser_and_keeps_the_key_out_of_the_chat(monkeypatch, capsys):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    import pytest
    pytest.importorskip("qrcode")  # in the plugin's own venv; a bare test Python may lack it
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    monkeypatch.setenv("DISPLAY", ":0")
    cli.main(["pair", "--open"])
    out = capsys.readouterr().out
    key = Config.load().key
    assert key not in out and "horncall://" not in out and "█" not in out  # no key, no link, no QR blocks
    assert len(out.strip().splitlines()) == 1 and opened
    page = Path(re.search(r"\((.+pair-qr\.html)\)", out).group(1))
    assert key in page.read_text(encoding="utf-8") and "<svg" in page.read_text(encoding="utf-8")


def test_pair_open_without_a_screen_points_to_the_terminal(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    cli.main(["pair", "--open"])
    out = capsys.readouterr().out
    assert "pair` in a terminal" in out and len(out.strip().splitlines()) == 1 and Config.load().key not in out


def test_skill_runs_in_a_small_cheap_context_and_never_echoes_the_qr():
    text = SKILL.read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert "context: fork" in front and "model: haiku" in front and "background: false" in front
    assert "pair --open" in text and len(text) < 1600


def test_pair_open_over_ssh_points_to_the_terminal(monkeypatch, capsys):
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda url: (_ for _ in ()).throw(AssertionError("no browser over SSH")))
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 10.0.0.1 22")
    cli.main(["pair", "--open"])
    assert "pair` in a terminal" in capsys.readouterr().out
