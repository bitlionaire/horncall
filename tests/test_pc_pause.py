"""Whether this PC sends to the phone is announced to it (owner report 2026-11-12: the phone kept saying "on duty"
while the PC sent nothing). 0.8.0: one way to stop for a while, `off --hours N` (`pause [N]` is its alias), and
`on` / `off` always tell the phone."""

import time

import pytest

from approver import cli, crypto, protocol
from approver.config import Config, State


class FakeLink:
    states: list = []

    def __init__(self, *a):
        pass

    def send_pc_state(self, device, paused_until):
        FakeLink.states.append((device, paused_until))


@pytest.fixture
def paired(monkeypatch, approver_home):
    Config(key=crypto.b64u_encode(b"k" * 32), device_name="DESK").save()
    State(mode="on").save()
    monkeypatch.setattr(cli, "Link", FakeLink)
    FakeLink.states = []


def test_pc_state_message():
    m = protocol.make_pc_state("DESK", 1794500000)
    assert m["type"] == "pc_state" and m["device"] == "DESK" and m["paused_until"] == 1794500000
    assert protocol.is_fresh(m)


@pytest.mark.parametrize("argv", [["off", "--hours", "1"], ["pause", "1"]])
def test_off_for_hours_and_its_alias_pause(paired, argv):
    cli.main(argv)
    state = State.load()
    assert state.mode == "on" and not state.is_on()  # nothing goes to the phone, but it comes back by itself
    (device, until), = FakeLink.states
    assert device == "DESK" and abs(until - (time.time() + 3600)) < 5
    assert State.load().is_on(now=time.time() + 3700)


def test_resume_ends_a_pause(paired):
    cli.main(["pause"])
    cli.main(["resume"])
    assert State.load().is_on() and FakeLink.states[-1] == ("DESK", 0)


def test_on_and_off_always_tell_the_phone(paired):
    cli.main(["on"])
    assert FakeLink.states[-1] == ("DESK", 0)
    cli.main(["off"])
    assert FakeLink.states[-1] == ("DESK", cli.PC_OFF)
    cli.main(["on"])
    cli.main(["pause"])
    cli.main(["on"])
    assert FakeLink.states[-1] == ("DESK", 0) and State.load().is_on()


@pytest.mark.parametrize("hours", ["0", "-1", "25"])
def test_pause_rejects_odd_lengths(paired, hours):
    with pytest.raises(SystemExit):
        cli.main(["pause", hours])
    with pytest.raises(SystemExit):
        cli.main(["off", "--hours", hours])
    assert FakeLink.states == [] and State.load().is_on()


def test_pause_when_already_off(paired):
    State(mode="off").save()
    with pytest.raises(SystemExit):
        cli.main(["off", "--hours", "2"])
    assert FakeLink.states == [] and State.load().mode == "off"


def test_pause_when_on_for_hours_ran_out(paired):
    # `on --hours 1` two hours ago: mode still says "on", but nothing goes to the phone any more.
    State(mode="on", on_until=time.time() - 3600).save()
    with pytest.raises(SystemExit):
        cli.main(["off", "--hours", "2"])
    assert FakeLink.states == [] and State.load().paused_until is None


def test_pairing_pages_are_private(paired, monkeypatch, approver_home):
    # The QR pages hold the key: same protection as config.json (0600; on Windows an ACL for this user).
    import os
    import stat
    pytest.importorskip("qrcode")  # in the plugin's own venv; a bare test Python may lack it
    from approver import config as config_module
    calls = []
    monkeypatch.setattr(config_module, "_restrict_acl", lambda path: calls.append(path.name))
    monkeypatch.setattr("webbrowser.open", lambda url: True)
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    page = cli.open_qr_page(cli.pairing_uri(Config.load()), "DESK")
    assert page and "pair-qr.html" in calls
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(page).st_mode) == 0o600
    cli.main(["pair"])
    assert "pair-qr.svg" in calls


def test_status_summary_says_where_requests_go(paired):
    config, now = Config.load(), time.time()
    on = cli.status_summary(config, State(mode="on"), True, now)
    assert len(on) == 3 and "paired" in on[0] and on[1].startswith("Requests go to the phone") and "reachable" in on[2]
    paused = cli.status_summary(config, State(mode="on", paused_until=now + 600), True, now)[1]
    assert paused.startswith("This PC sends nothing") and time.strftime("%H:%M", time.localtime(now + 600)) in paused
    assert cli.status_summary(config, State(mode="off"), False, now)[1].startswith("Off")
    assert cli.status_summary(config, State(mode="on", on_until=now - 1), True, now)[1].startswith("Off")
    assert "NOT reachable" in cli.status_summary(config, State(mode="on"), False, now)[2]
