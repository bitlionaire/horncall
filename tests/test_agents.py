import io
import json

from approver import agents, hook, install, protocol


def test_gemini_shell_maps_to_bash_and_answers_in_gemini_format():
    g = agents.get("gemini")
    hi = g.parse({"tool_name": "run_shell_command", "tool_input": {"command": "rm -rf build", "description": "clean"},
                  "cwd": "/repo", "session_id": "s1", "hook_event_name": "BeforeTool"})
    assert hi.tool == "Bash" and hi.tool_input["command"] == "rm -rf build" and hi.session_id == "s1"
    assert g.allow() == {"decision": "allow"}
    assert g.deny("no") == {"decision": "deny", "reason": "no"}


def test_gemini_reads_are_not_asked():
    assert agents.get("gemini").parse({"tool_name": "read_file", "tool_input": {"absolute_path": "/a"}}) is None


def test_gemini_edit_maps_to_edit():
    hi = agents.get("gemini").parse({"tool_name": "replace", "tool_input": {
        "file_path": "/repo/a.py", "old_string": "a", "new_string": "b"}, "cwd": "/repo"})
    assert hi.tool == "Edit" and hi.tool_input["new_string"] == "b"


def test_copilot_camel_case_shell_and_json_string_args():
    c = agents.get("copilot")
    hi = c.parse({"sessionId": "x", "cwd": "/repo", "toolName": "bash", "toolArgs": json.dumps({"command": "git push -f"})})
    assert hi.tool == "Bash" and hi.tool_input["command"] == "git push -f" and hi.session_id == "x"
    assert c.allow() == {"behavior": "allow"}
    assert c.deny("no") == {"behavior": "deny", "message": "no"}


def test_copilot_real_permission_request_shape():
    # Field names captured from Copilot CLI 1.0.98 (values made up).
    hi = agents.get("copilot").parse({"cwd": "D:/r", "hookName": "permissionRequest", "permissionSuggestions": [],
                                      "sessionId": "abc", "timestamp": 1, "toolName": "powershell",
                                      "toolInput": {"command": "Remove-Item -Recurse -Force build"}})
    assert hi.tool == "PowerShell" and hi.tool_input["command"].startswith("Remove-Item")
    assert protocol.risk_reasons(hi.tool, hi.tool_input, hi.cwd) == ["Deletes folders recursively (PowerShell)"]


def test_codex_argv_shell_and_claude_style_answer():
    c = agents.get("codex")
    hi = c.parse({"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "git push --force"]},
                  "cwd": "/r", "session_id": "k", "hook_event_name": "PermissionRequest"})
    assert hi.tool == "Bash" and hi.tool_input["command"] == "git push --force"
    assert c.allow()["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    assert protocol.risk_reasons(hi.tool, hi.tool_input, hi.cwd)  # force push is flagged


def _codex_patch(patch, cwd="/r"):
    hi = agents.get("codex").parse({"tool_name": "apply_patch", "tool_input": {"command": patch}, "cwd": cwd})
    return hi, protocol.risk_reasons(hi.tool, hi.tool_input, hi.cwd)


def test_codex_patch_in_project_is_normal():
    hi, reasons = _codex_patch("*** Begin Patch\n*** Update File: src/a.py\n@@\n-x\n+y\n*** End Patch")
    assert hi.tool == "Edit" and hi.tool_input["file_path"].replace("\\", "/").endswith("src/a.py")
    assert reasons == []


def test_codex_patch_checks_every_file():
    _, reasons = _codex_patch("*** Begin Patch\n*** Add File: notes.md\n+hi\n*** Update File: .env\n@@\n-A=1\n+A=2\n"
                              "*** Delete File: old.txt\n*** End Patch")
    assert "Changes permissions, hooks, CI or secrets" in reasons and "Deletes files" in reasons


def test_codex_patch_without_file_headers_is_dangerous():
    _, reasons = _codex_patch("something odd")
    assert reasons == ["Patch whose files cannot be checked"]


def test_install_codex_hooks_json(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    install.install_codex()
    data = json.loads((tmp_path / "hooks.json").read_text())
    assert "hook codex" in data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert "event codex idle" in data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert install.codex_installed() and install.uninstall_codex() and not install.codex_installed()


def test_claude_adapter_keeps_the_old_answer_shape():
    assert hook.decision_output("allow") == {
        "hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}


def test_request_carries_agent_and_faction_codename():
    r = protocol.build_request({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/r", "session_id": "abc"},
                               "PC", 60, 500, agent="gemini")
    assert r["agent"] == "gemini"
    assert r["session"].split()[0] in protocol.FACTION_RANKS["gemini"]
    # Claude chats keep the names they had before factions existed.
    assert protocol.codename("abc") == protocol.codename("abc", "claude")


def test_gemini_hook_end_to_end_allow(monkeypatch, approver_home):
    from approver.config import Config, State
    Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC").save()
    State(mode="on").save()

    class FakeLink:
        def __init__(self, *a):
            pass

        def request_and_wait(self, request, deadline, accept=None):
            assert request["agent"] == "gemini" and request["tool"] == "Bash"
            return {"decision": "allow", "via": "gesture"}

    out = io.StringIO()
    stdin = io.StringIO(json.dumps({"tool_name": "run_shell_command", "tool_input": {"command": "npm test"},
                                    "cwd": "/r", "session_id": "s"}))
    hook.run(stdin, out, link_factory=FakeLink, agent="gemini")
    assert json.loads(out.getvalue()) == {"decision": "allow"}


def test_task_events_send_working_then_idle_with_duration(monkeypatch, approver_home):
    from approver.config import Config, State
    Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC").save()
    State(mode="on").save()
    sent = []

    class FakeLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            sent.append(status)

    now = [1_800_000_000.0]
    monkeypatch.setattr(hook.time, "time", lambda: now[0])
    payload = json.dumps({"session_id": "s9", "cwd": "/repo/app"})
    hook.run_event(io.StringIO(payload), "claude", "working", link_factory=FakeLink)
    now[0] += 180
    hook.run_event(io.StringIO(payload), "claude", "idle", link_factory=FakeLink)
    assert [s["state"] for s in sent] == ["working", "idle"]
    assert sent[1]["worked_s"] == 180 and sent[1]["project"] == "app" and sent[1]["type"] == "status"
    assert sent[0]["session"] == sent[1]["session"]


def test_task_events_are_silent_when_off(approver_home):
    sent = []

    class FakeLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            sent.append(status)

    hook.run_event(io.StringIO("{}"), "claude", "working", link_factory=FakeLink)
    assert sent == []


def test_end_with_another_session_id_is_matched_by_transcript(monkeypatch, approver_home):
    """Gemini CLI once reported the end of a turn with a different session id than its start."""
    from approver.config import Config, State
    Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC").save()
    State(mode="on").save()
    sent = []

    class FakeLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            sent.append(status)

    now = [1_800_000_000.0]
    monkeypatch.setattr(hook.time, "time", lambda: now[0])
    start = {"session_id": "main-1", "cwd": "/r", "transcript_path": "/t/session-main.jsonl"}
    end = {"session_id": "other-2", "cwd": "/r", "transcript_path": "/t/session-main.jsonl"}
    hook.run_event(io.StringIO(json.dumps(start)), "gemini", "working", link_factory=FakeLink)
    now[0] += 80
    hook.run_event(io.StringIO(json.dumps(end)), "gemini", "idle", link_factory=FakeLink)
    assert sent[1]["session"] == sent[0]["session"] == protocol.codename("main-1", "gemini")
    assert sent[1]["worked_s"] == 80


def test_install_gemini_is_idempotent_and_keeps_other_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark", "hooks": {"BeforeTool": [
        {"matcher": "x", "hooks": [{"type": "command", "name": "other", "command": "echo"}]}]}}))
    install.install_gemini()
    install.install_gemini()
    data = json.loads((tmp_path / "settings.json").read_text())
    ours = [h for e in data["hooks"]["BeforeTool"] for h in e["hooks"] if h["name"] == "horncall"]
    assert len(ours) == 1 and data["theme"] == "dark"
    assert ours[0]["timeout"] == 1_900_000 and "hook gemini" in ours[0]["command"]
    assert "event gemini idle" in data["hooks"]["AfterAgent"][0]["hooks"][0]["command"]
    assert install.gemini_installed()
    assert install.uninstall_gemini() and not install.gemini_installed()
    left = json.loads((tmp_path / "settings.json").read_text())["hooks"]["BeforeTool"]
    assert left[0]["hooks"][0]["name"] == "other"
    assert (tmp_path / "settings.json.horncall-backup").exists()


def test_install_copilot_writes_own_file(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    path = install.install_copilot()
    data = json.loads(path.read_text())
    assert data["version"] == 1 and data["hooks"]["permissionRequest"][0]["timeoutSec"] == 1900
    assert "event copilot working" in data["hooks"]["userPromptSubmitted"][0]["command"]
    assert "event copilot idle" in data["hooks"]["agentStop"][0]["command"]
    assert "posttool copilot" in data["hooks"]["postToolUse"][0]["command"]
    assert install.uninstall_copilot() and not path.exists()


def test_copilot_answer_in_terminal_matches_the_phone_question():
    """permissionRequest (toolInput + description) and postToolUse (toolArgs) give the same fingerprint."""
    c = agents.get("copilot")
    asked = c.parse({"sessionId": "s", "toolName": "powershell",
                     "toolInput": {"command": "npm test", "description": "Run tests"}})
    ran = c.parse({"sessionId": "s", "toolName": "powershell", "toolArgs": {"command": "npm test"},
                   "toolResult": {"resultType": "success"}})
    assert protocol.order_fp(asked.session_id, asked.tool, asked.tool_input) == \
        protocol.order_fp(ran.session_id, ran.tool, ran.tool_input)
