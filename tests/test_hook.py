import io
import json
import time

from approver import crypto, hook
from approver.config import Config, State
from approver.link import Link

HOOK_INPUT = {"hook_event_name": "PermissionRequest", "tool_name": "Bash",
              "tool_input": {"command": "npm test"}, "cwd": "/repo"}


class FakeTransport:
    """In-memory ntfy. `phone` is called with every request the PC publishes."""

    def __init__(self, phone=None):
        self.topics: dict[str, list[str]] = {}
        self.phone = phone

    def publish(self, topic, body, timeout=15, priority=4):
        self.topics.setdefault(topic, []).append(body)
        if self.phone:
            self.phone(topic, body)

    def subscribe(self, topic, since, deadline, on_open=None, stop=None):
        if on_open:
            on_open()
        for body in list(self.topics.get(topic, [])):
            if stop is not None and stop():
                return
            yield body


def make_link_factory(transport):
    return lambda key, server: Link(key, server, transport=transport)


def phone_answering(decision, via="gesture", signer=None):
    def phone(topic, body):
        config = Config.load()
        link = Link(config.key, config.server, transport=transport)
        if topic != link.topic_req:
            return
        msg = crypto.open_envelope(config.key, topic, body)
        if msg["type"] == "request":
            link.send_response(msg["id"], decision, via, signer=signer)
    transport = FakeTransport(phone)
    return transport


def device_key():
    """A software stand-in for the phone's Android Keystore key: (public key, sign)."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.DER,
                                               serialization.PublicFormat.SubjectPublicKeyInfo)
    return crypto.b64u_encode(public), lambda data: crypto.b64u_encode(private.sign(data, ec.ECDSA(hashes.SHA256())))


def test_first_signing_phone_joins_then_only_it_counts():
    setup()
    phone = device_key()
    assert run_hook(phone_answering("allow", signer=phone))["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert [p["pub"] for p in Config.load().phones] == [phone[0]]
    # Someone with a copy of config.json: unsigned or signed with their own key → ignored.
    State(mode="on").save()
    Config.load().save()
    cfg = Config.load()
    cfg.timeout_s = 0
    cfg.save()
    assert run_hook(phone_answering("allow")) is None
    assert run_hook(phone_answering("allow", signer=device_key())) is None
    assert len(Config.load().phones) == 1


def test_new_pairing_never_accepts_unsigned_answers():
    """A pairing made by a signature-aware plugin has no unsigned grace period — not even before a phone joined."""
    setup(signed_only=True, timeout_s=0)
    assert run_hook(phone_answering("allow")) is None
    phone = device_key()
    cfg = Config.load()
    cfg.timeout_s = 120
    cfg.save()
    assert run_hook(phone_answering("allow", signer=phone)) is not None  # the real phone joins with a signature


def test_old_configs_load_with_the_grace_period(approver_home):
    import json
    approver_home.mkdir(parents=True, exist_ok=True)
    (approver_home / "config.json").write_text(json.dumps({"key": crypto.new_key()}))
    assert Config.load().signed_only is False
    assert Config(key=crypto.new_key()).signed_only is True


def test_pairing_window_lets_a_second_phone_join():
    from approver import phones
    setup()
    first, second = device_key(), device_key()
    run_hook(phone_answering("allow", signer=first))
    cfg = Config.load()
    phones.open_pairing(cfg)
    cfg.save()
    assert run_hook(phone_answering("allow", signer=second)) is not None
    assert len(Config.load().phones) == 2


def test_tampered_signature_is_rejected():
    from approver import phones
    pub, sign = device_key()
    response = {"id": "a", "decision": "deny", "ts": 1, "via": "button", "pub": pub}
    response["sig"] = sign(b"horncall-sig/1|a|deny|1|button")
    cfg = Config(key=crypto.new_key())
    assert phones.judge(cfg, dict(response)) == "joined"
    assert phones.judge(cfg, dict(response, decision="allow")) == "bad-signature"
    assert phones.judge(cfg, {"id": "a", "decision": "allow", "ts": 1, "via": "x"}) == "unsigned"


def run_hook(transport):
    out = io.StringIO()
    assert hook.run(io.StringIO(json.dumps(HOOK_INPUT)), out, make_link_factory(transport)) == 0
    return json.loads(out.getvalue()) if out.getvalue() else None


def setup(mode="on", **config):
    # Like a pairing from before signatures (the simulated phone here answers unsigned unless told otherwise).
    config.setdefault("signed_only", False)
    Config(key=crypto.new_key(), device_name="PC", **config).save()
    State(mode=mode).save()


def test_off_mode_prints_nothing():
    setup(mode="off")
    transport = FakeTransport()
    assert run_hook(transport) is None
    assert transport.topics == {}


def test_unpaired_prints_nothing():
    State(mode="on").save()
    assert run_hook(FakeTransport()) is None


def test_expired_on_mode_is_off():
    setup()
    State(mode="on", on_until=time.time() - 1).save()
    assert run_hook(FakeTransport()) is None


def test_allow():
    setup()
    out = run_hook(phone_answering("allow"))
    assert out == {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                          "decision": {"behavior": "allow"}}}


def test_deny_has_message():
    setup()
    decision = run_hook(phone_answering("deny", "button"))["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and "button" in decision["message"]


def sent_types(transport):
    config = Config.load()
    link = Link(config.key, config.server, transport=transport)
    return [crypto.open_envelope(config.key, link.topic_req, b)["type"] for b in transport.topics.get(link.topic_req, [])]


def test_pass_from_a_muted_phone_asks_at_the_pc_straight_away():
    setup()
    transport = phone_answering("pass", "mute")
    assert run_hook(transport) is None  # no opinion: the agent's own dialog decides
    assert sent_types(transport) == ["request"]  # the phone showed no card, so nothing to cancel
    assert not any(hook.pending_dir().iterdir())


def test_unknown_decision_is_still_ignored():
    setup(timeout_s=0)
    transport = phone_answering("maybe")
    assert run_hook(transport) is None
    assert sent_types(transport) == ["request", "cancel"]  # waited, then timed out like an unanswered question


def test_timeout_falls_back_to_dialog_and_clears_the_phone_card():
    setup(timeout_s=0)
    transport = FakeTransport()
    assert run_hook(transport) is None
    assert sent_types(transport) == ["request", "cancel"]
    assert not any(hook.pending_dir().iterdir())  # no marker left behind


def test_old_default_timeout_becomes_long():
    setup(timeout_s=120)
    assert Config.load().timeout_s == 1800


def test_answered_on_pc_drops_the_phone_card():
    """The agent kills the hook when the PC dialog is answered; PostToolUse then tells the phone."""
    setup()
    # The hook was killed while waiting: its marker is still there.
    request = __import__("approver.protocol", fromlist=["x"]).build_request(
        dict(HOOK_INPUT, session_id="s1"), "PC", 60, 1500)
    hook._pending_marker(request["fp"], "s1", request["id"])
    transport = FakeTransport()
    post = dict(HOOK_INPUT, session_id="s1", hook_event_name="PostToolUse")
    hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))
    assert sent_types(transport) == ["resolved"]
    # Another tool call (no open phone question): nothing is sent.
    hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))
    assert sent_types(transport) == ["resolved"]


def test_two_identical_calls_are_resolved_one_by_one():
    setup()
    request = __import__("approver.protocol", fromlist=["x"]).build_request(
        dict(HOOK_INPUT, session_id="s1"), "PC", 60, 1500)
    first = hook._pending_marker(request["fp"], "s1", "req-1")
    import os, time as _t
    os.utime(first, (_t.time() - 5, _t.time() - 5))  # the older one
    hook._pending_marker(request["fp"], "s1", "req-2")
    transport = FakeTransport()
    post = dict(HOOK_INPUT, session_id="s1", hook_event_name="PostToolUse")
    hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))
    config = Config.load()
    link = Link(config.key, config.server, transport=transport)
    sent = [crypto.open_envelope(config.key, link.topic_req, b) for b in transport.topics[link.topic_req]]
    assert [m["request"] for m in sent] == ["req-1"]  # the second call still waits on the phone
    assert [p.name.split(".")[1] for p in hook.pending_dir().iterdir()] == ["req-2"]


def _post(transport, **fields):
    post = {**HOOK_INPUT, "session_id": "s1", "hook_event_name": "PostToolUse", **fields}
    hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))


def _sent(transport):
    config = Config.load()
    link = Link(config.key, config.server, transport=transport)
    return [crypto.open_envelope(config.key, link.topic_req, b) for b in transport.topics.get(link.topic_req, [])]


def test_failed_command_drops_the_phone_card_too():
    """PostToolUseFailure carries the same tool_name and tool_input: same fingerprint, same handler."""
    setup()
    request = __import__("approver.protocol", fromlist=["x"]).build_request(dict(HOOK_INPUT, session_id="s1"), "PC", 60, 1500)
    hook._pending_marker(request["fp"], "s1", request["id"], "Bash")
    transport = FakeTransport()
    _post(transport, hook_event_name="PostToolUseFailure", error="Exit code 1", is_interrupt=False)
    assert [m["type"] for m in _sent(transport)] == ["resolved"]


def test_a_tool_that_comes_back_with_other_input_is_matched_by_chat_and_tool():
    setup()
    hook._pending_marker("f" * 20, "s1", "ask-1", "AskUserQuestion")
    transport = FakeTransport()
    post = dict(session_id="s1", hook_event_name="PostToolUse", tool_name="AskUserQuestion", cwd="/repo",
                tool_input={"questions": [{"question": "Which?"}], "answers": {"Which?": "A"}})
    hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))
    assert [m["request"] for m in _sent(transport)] == ["ask-1"]


def test_no_guessing_with_two_waiting_calls_or_for_shell_commands():
    setup()
    hook._pending_marker("a" * 20, "s1", "w-1", "WebFetch")
    hook._pending_marker("b" * 20, "s1", "w-2", "WebFetch")
    hook._pending_marker("c" * 20, "s1", "sh-1", "Bash")
    transport = FakeTransport()
    for tool, tool_input in (("WebFetch", {"url": "https://x"}), ("Bash", {"command": "something else"})):
        post = dict(session_id="s1", hook_event_name="PostToolUse", tool_name=tool, cwd="/repo", tool_input=tool_input)
        hook.run_posttool(io.StringIO(json.dumps(post)), "claude", make_link_factory(transport))
    assert _sent(transport) == []
    assert len(list(hook.pending_dir().iterdir())) == 3


def test_a_new_task_drops_the_old_cards_of_that_chat_only():
    """Refused on the PC or interrupted with Esc: no hook reports it, the next prompt shows it is over."""
    setup()
    hook._pending_marker("a" * 20, "s1", "r1", "Bash")
    hook._pending_marker("b" * 20, "s1", "r2", "PowerShell")
    other = hook._pending_marker("c" * 20, "s2", "r3", "Bash")
    transport = FakeTransport()
    start = {"session_id": "s1", "cwd": "/repo", "hook_event_name": "UserPromptSubmit"}
    hook.run_event(io.StringIO(json.dumps(start)), "claude", "working", make_link_factory(transport))
    sent = _sent(transport)
    assert sorted(m["request"] for m in sent if m["type"] == "resolved") == ["r1", "r2"]
    assert [p.name for p in hook.pending_dir().iterdir()] == [other.name]


def test_an_allow_for_a_question_answers_nothing_and_clears_the_card():
    """AskUserQuestion needs the answer itself: a bare allow from the phone must not reach Claude Code."""
    setup()
    out = io.StringIO()
    question = {"session_id": "s1", "cwd": "/repo", "tool_name": "AskUserQuestion",
                "tool_input": {"questions": [{"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}}
    transport = phone_answering("allow", "button")
    assert hook.run(io.StringIO(json.dumps(question)), out, make_link_factory(transport)) == 0
    assert out.getvalue() == ""  # no allow: the question stays on the PC
    types = sent_types(transport)
    assert types[0] == "request" and "resolved" in types  # the card leaves the phone quietly
    assert not any(hook.pending_dir().iterdir())


def test_a_deny_for_a_question_still_denies():
    setup()
    out = io.StringIO()
    question = {"session_id": "s1", "cwd": "/repo", "tool_name": "ExitPlanMode", "tool_input": {"plan": "p"}}
    hook.run(io.StringIO(json.dumps(question)), out, make_link_factory(phone_answering("deny", "button")))
    assert json.loads(out.getvalue())["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_hook_gives_up_quietly_once_its_card_was_dropped():
    """Claude Code does not kill the hook when the PC dialog is answered; posttool removes the marker."""
    setup()

    class AnsweredOnPc(FakeTransport):
        def subscribe(self, topic, since, deadline, on_open=None, stop=None):
            if on_open:
                on_open()
            for marker in list(hook.pending_dir().iterdir()):
                marker.unlink()  # what posttool does
            assert stop is not None and stop()
            return
            yield

    transport = AnsweredOnPc()
    assert run_hook(transport) is None
    assert sent_types(transport) == ["request"]  # no "cancel", no answer, no waiting for half an hour


def test_idle_clears_markers_of_that_session():
    setup()
    m1 = hook._pending_marker("aaa", "s1", "r1")
    m2 = hook._pending_marker("bbb", "s2", "r2")
    hook._clear_session_markers("s1")
    assert not m1.exists() and m2.exists()


def test_timeout_deny():
    setup(timeout_s=0, on_timeout="deny")
    assert run_hook(FakeTransport())["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_network_error_falls_back_to_dialog():
    setup()

    class Broken(FakeTransport):
        def publish(self, *a, **k):
            raise OSError("offline")

    assert run_hook(Broken()) is None


def test_response_for_other_request_ignored():
    setup(timeout_s=0)
    transport = FakeTransport()
    config = Config.load()
    Link(config.key, config.server, transport=transport).send_response("someone-else", "allow", "gesture")
    assert run_hook(transport) is None


def test_duty_until_forwarded():
    setup()
    State(mode="on", on_until=time.time() + 3600).save()
    seen = {}

    def phone(topic, body):
        config = Config.load()
        link = Link(config.key, config.server, transport=transport)
        if topic == link.topic_req:
            msg = crypto.open_envelope(config.key, topic, body)
            if msg["type"] == "request":
                seen.update(msg)
                link.send_response(msg["id"], "allow", "gesture")

    transport = FakeTransport(phone)
    run_hook(transport)
    assert seen["duty_until"] > time.time()


def test_cli_name_sets_label(tmp_path, capsys):
    from approver import cli, protocol
    setup()
    cli.main(["name", "--path", str(tmp_path), "Backend", "ORLEN"])
    assert Config.load().labels[protocol.path_key(str(tmp_path))] == "Backend ORLEN"
    cli.main(["name", "--path", str(tmp_path), "--clear"])
    assert Config.load().labels == {}
