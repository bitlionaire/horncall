"""Fixes after the Codex review of 0.7.0 (2026-11-05)."""
import json

from approver import agents, install, protocol, reply
from test_vitals import write_jsonl


def test_a_multi_file_opencode_edit_shows_and_checks_every_file():
    hi = agents.get("opencode").parse({"tool_name": "edit", "session_id": "s", "tool_input": {"resources": [], "metadata": {
        "files": [{"file": "D:/r/readme.md", "patch": "+hi"}, {"file": "D:/r/.claude/settings.json", "patch": "+hooks"}]}}})
    assert hi.tool_input["patch_files"] == ["D:/r/readme.md", "D:/r/.claude/settings.json"]
    assert "--- D:/r/.claude/settings.json" in hi.tool_input["new_string"]
    assert "Changes permissions, hooks, CI or secrets" in protocol.risk_reasons("Edit", hi.tool_input, "D:/r")


def test_replies_drop_tilde_fenced_code_and_codex_takes_the_newest_answer(tmp_path):
    assert reply.plain("Done.\n~~~\nsecret = 1\n~~~\nAll good.") == "Done. All good."
    older = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                  "content": [{"type": "output_text", "text": "Old answer."}]}}
    newer = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "New answer."}}
    path = write_jsonl(tmp_path / "r.jsonl", older, newer)
    assert reply.read("codex", {"transcript_path": path}) == "New answer."


def test_crushrc_command_survives_odd_characters_in_a_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    launcher = tmp_path / "we$ird`dir" / "approver_launcher.py"
    launcher.parent.mkdir()
    launcher.write_text("")  # exists: the plugin-folder install, not pip
    monkeypatch.setattr(install, "LAUNCHER", launcher)
    install.install_crush()
    line = next(l for l in install.crush_rc().read_text(encoding="utf-8").splitlines() if "PreToolUse" in l)
    assert "\$ird\`dir" in line  # escaped for crushrc's double quotes
    assert "we$ird`dir" in install.crush_commands()[0]  # and read back as it is


def test_a_forgotten_phone_no_longer_counts_in_a_waiting_hook(monkeypatch):
    from approver import phones
    from approver.config import Config
    old = Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC",
                 phones=[{"pub": "P1", "added": 1}, {"pub": "P2", "added": 1}])
    old.save()
    fresh = Config.load()
    fresh.phones = [p for p in fresh.phones if p["pub"] != "P1"]  # `phones forget` while the hook waits
    fresh.save()
    monkeypatch.setattr(phones.crypto, "verify_signature", lambda *a: True)
    assert phones.judge(old, {"pub": "P1", "sig": "s", "decision": "allow"}) == "forgotten"
    assert phones.judge(old, {"pub": "P2", "sig": "s", "decision": "allow"}) == "ok"


def test_git_branch_safe_delete_is_not_dangerous():
    # GitHub issue #1: rules run case-insensitively, so -d (safe delete) was flagged like -D (force).
    reason = "Deletes a branch with unmerged work"
    assert reason not in protocol.risk_reasons("Bash", {"command": "git branch -d merged-feature"}, None)
    assert reason in protocol.risk_reasons("Bash", {"command": "git branch -D wip"}, None)
    for forced in ("git branch -d -f wip", "git branch -fd wip", "git branch -f -d wip", "git branch -Dr wip",
                   "git branch --force --delete wip", "git branch --delete --force wip"):
        assert reason in protocol.risk_reasons("Bash", {"command": forced}, None), forced
    for safe in ("git branch -f wip main", "git branch -r", "git branch --delete wip"):
        assert reason not in protocol.risk_reasons("Bash", {"command": safe}, None), safe
    # Unix flags keep their case (chmod -r takes read away, -R recurses); Windows switches don't care.
    assert not protocol.risk_reasons("Bash", {"command": "chmod -r 777 x"}, None)
    assert protocol.risk_reasons("Bash", {"command": "chmod -R 777 x"}, None)
    assert protocol.risk_reasons("Bash", {"command": "DEL /S x"}, None)
