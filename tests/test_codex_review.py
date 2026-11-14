"""Codex "Approve for me" (owner report 2026-11-12): its own reviewer decides, so the phone gets no card — except
for risky commands. The hook input is identical in both modes (Codex 0.167); the mode is read from the session
file (turn_context.approvals_reviewer, matched by turn_id)."""

import io
import json

from approver import crypto, hook
from approver.config import Config, State


def rollout(tmp_path, turns):
    """A Codex session file with one turn_context line per (turn_id, reviewer), plus unrelated lines."""
    path = tmp_path / "rollout-2026-11-12T21-28-22-test.jsonl"
    lines = [json.dumps({"type": "session_meta", "payload": {"id": "s", "source": "vscode"}})]
    for turn_id, reviewer in turns:
        lines.append(json.dumps({"timestamp": "2026-11-12T20:28:28Z", "type": "turn_context", "payload": {
            "turn_id": turn_id, "approval_policy": "on-request", "approvals_reviewer": reviewer, "cwd": "/r"}}))
        lines.append(json.dumps({"type": "response_item", "payload": {"type": "message", "content": "..."}}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(path)


def test_the_reviewer_of_this_turn_is_read_never_an_older_one(tmp_path):
    path = rollout(tmp_path, [("t1", "auto_review"), ("t2", "user"), ("t3", "auto_review"), ("t4", "user")])
    assert hook.codex_reviewer({"transcript_path": path, "turn_id": "t3"}, retry_s=0) == "auto_review"
    assert hook.codex_reviewer({"transcript_path": path, "turn_id": "t4"}, retry_s=0) == "user"
    assert hook.codex_reviewer({"transcript_path": path, "turn_id": "t5"}, retry_s=0) is None  # not written yet
    assert hook.codex_reviewer({"transcript_path": path}, retry_s=0) is None  # no turn id: unknown
    assert hook.codex_reviewer({"transcript_path": str(tmp_path / "missing.jsonl"), "turn_id": "t1"}, retry_s=0) is None


def test_a_long_turn_still_finds_its_reviewer(tmp_path):
    """Review of 0.8.0: a turn writes a lot after its turn_context line; 256 KB of tail missed it."""
    path = rollout(tmp_path, [("t1", "auto_review")])
    with open(path, "a", encoding="utf-8") as f:
        for _ in range(1500):  # ~1 MB of tool output after the line
            f.write(json.dumps({"type": "response_item", "payload": {"output": "x" * 700}}) + "\n")
    assert hook.codex_reviewer({"transcript_path": path, "turn_id": "t1"}, retry_s=0) == "auto_review"


class FakeLink:
    sent: list = []

    def __init__(self, *a):
        pass

    def send_request(self, request):
        FakeLink.sent.append(request)

    def request_and_wait(self, request, deadline, accept=None, stop=None, stop_every_s=None):
        FakeLink.sent.append(request)
        return {"decision": "allow", "via": "button"}

    def send_cancel(self, *a):
        pass


def ask(approver_home, monkeypatch, tmp_path, command, reviewer, mode="follow"):
    Config(key=crypto.b64u_encode(b"k" * 32), codex_auto_review=mode).save()
    State(mode="on").save()
    monkeypatch.setattr(hook.phones, "judge", lambda config, response: "ok")
    FakeLink.sent = []
    path = rollout(tmp_path, [("t1", reviewer)])
    raw = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": command},
           "cwd": "/r", "session_id": "s", "turn_id": "t1", "permission_mode": "default", "transcript_path": path}
    out = io.StringIO()
    assert hook.run(io.StringIO(json.dumps(raw)), out, link_factory=FakeLink, agent="codex") == 0
    return FakeLink.sent, out.getvalue()


def test_approve_for_me_sends_no_card_and_no_opinion(approver_home, monkeypatch, tmp_path):
    sent, out = ask(approver_home, monkeypatch, tmp_path, "curl -I https://example.com", "auto_review")
    assert sent == [] and out == ""  # Codex's reviewer decides
    assert '"codex-auto-review"' in (approver_home / "hook.log").read_text(encoding="utf-8")


def test_the_normal_mode_still_asks_the_phone(approver_home, monkeypatch, tmp_path):
    sent, out = ask(approver_home, monkeypatch, tmp_path, "curl -I https://example.com", "user")
    assert len(sent) == 1 and json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_risky_commands_still_come_to_the_phone_in_approve_for_me(approver_home, monkeypatch, tmp_path):
    sent, out = ask(approver_home, monkeypatch, tmp_path, "git push --force origin main", "auto_review")
    assert len(sent) == 1 and sent[0]["risk"] == "danger"


def test_always_ask_sends_every_question(approver_home, monkeypatch, tmp_path):
    sent, out = ask(approver_home, monkeypatch, tmp_path, "curl -I https://example.com", "auto_review", mode="always-ask")
    assert len(sent) == 1
