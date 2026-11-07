import io
import json
import os

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


def test_codex_keeps_the_description_of_both_shell_shapes():
    c = agents.get("codex")
    old = c.parse({"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "npm test"], "justification": "Run tests"},
                   "cwd": "/r", "session_id": "k"})
    new = c.parse({"tool_name": "Bash", "tool_input": {"command": "npm test", "description": "Run tests"},
                   "cwd": "/r", "session_id": "k"})
    assert old.tool_input["description"] == new.tool_input["description"] == "Run tests"


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
    # After a command (also a failed one) the card must leave the phone: Codex's PostToolUse runs for both.
    assert "posttool codex" in data["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
    # Its questions need no approval: only PreToolUse sees them.
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "request_user_input|request_user_input_async"
    assert len(install.codex_commands()) == 5
    assert install.codex_installed() and install.uninstall_codex() and not install.codex_installed()


def test_install_codebuddy_settings_keeps_the_users_own(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEBUDDY_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"model": "x", "hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine.sh"}]}]}}))
    install.install_codebuddy()
    data = json.loads((tmp_path / "settings.json").read_text())
    assert data["model"] == "x" and data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine.sh"
    assert "hook codebuddy" in data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]
    assert data["hooks"]["PermissionRequest"][0]["hooks"][0]["timeout"] == 1900
    assert "\\" not in data["hooks"]["PermissionRequest"][0]["hooks"][0]["command"]  # Git Bash eats backslashes
    assert len(install.codebuddy_commands()) == 4
    assert install.uninstall_codebuddy() and not install.codebuddy_installed()
    assert json.loads((tmp_path / "settings.json").read_text())["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "mine.sh"


def test_codebuddy_speaks_claudes_format():
    hi = agents.get("codebuddy").parse({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/r", "session_id": "s"})
    assert hi.tool == "Bash" and hi.tool_input["command"] == "ls"
    assert agents.get("codebuddy").allow() == agents.get("claude").allow()
    assert protocol.codename("s", "codebuddy").split()[0] in protocol.FACTION_RANKS["codebuddy"]


def test_install_crush_keeps_the_users_lines_and_quotes_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    rc = tmp_path / "crush" / "crushrc"
    rc.parent.mkdir()
    rc.write_text("permissions allow view\n", encoding="utf-8")
    install.install_crush()
    install.install_crush()  # twice: still one block
    text = rc.read_text(encoding="utf-8")
    assert text.startswith("permissions allow view\n") and text.count(install.CRUSH_BEGIN) == 1
    line = next(l for l in text.splitlines() if l.startswith("hook add PreToolUse"))
    assert "--timeout 1900" in line and "--matcher '^(bash|edit" in line
    command = install.crush_commands()[0]
    assert "\\" not in command and command.startswith("'") and command.endswith("hook crush")  # Crush's shell eats backslashes
    assert len(install.crush_commands()) == 5 and install.state("crush") == "here"
    assert install.uninstall_crush() and rc.read_text(encoding="utf-8") == "permissions allow view\n"
    assert not install.crush_installed()


def test_crush_adapter_names_tools_and_leaves_reads_alone():
    crush = agents.get("crush")
    hi = crush.parse({"tool_name": "bash", "tool_input": {"command": "rm -rf build"}, "cwd": "/r", "session_id": "s"})
    assert hi.tool == "Bash" and hi.tool_input["command"] == "rm -rf build"
    assert crush.parse({"tool_name": "edit", "tool_input": {"file_path": "a.py", "old_string": "a", "new_string": "b"},
                        "session_id": "s"}).tool == "Edit"
    assert crush.parse({"tool_name": "view", "tool_input": {"file_path": "a.py"}, "session_id": "s"}) is None
    q = crush.parse({"tool_name": "question", "session_id": "s", "tool_input": {"questions": [
        {"label": "DB", "type": "single_choice", "question": "Which?", "choices": [{"id": "1", "label": "SQLite"}]}]}})
    assert q.tool == "AskUserQuestion" and q.notify_only and not q.answered_later
    assert q.tool_input["questions"][0] == {"header": "DB", "question": "Which?", "options": [{"id": "1", "label": "SQLite"}]}
    assert crush.allow() == {"decision": "allow"} and crush.deny("no") == {"decision": "deny", "reason": "no"}


def test_install_opencode_plugin_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(tmp_path))
    path = install.install_opencode()
    assert path == tmp_path / "plugins" / "horncall.ts"
    text = path.read_text(encoding="utf-8")
    assert install.OPENCODE_PLACEHOLDER not in text and 'id: "horncall"' in text
    command = json.loads(text.split("const COMMAND: string[] = ", 1)[1].split(" //", 1)[0])
    assert command[-1].endswith("approver_launcher.py")
    assert install.opencode_commands()[0].endswith("approver_launcher.py hook opencode")
    assert install.state("opencode") == "here"
    assert install.uninstall_opencode() and not path.exists() and not install.uninstall_opencode()


def test_opencode_adapter_reads_the_plugins_requests():
    oc = agents.get("opencode")
    shell = oc.parse({"tool_name": "shell", "session_id": "s", "cwd": "/r",
                      "tool_input": {"resources": ["git push --force"], "metadata": {}}})
    assert shell.tool == "Bash" and shell.tool_input == {"command": "git push --force"}
    edit = oc.parse({"tool_name": "edit", "session_id": "s", "tool_input": {"resources": ["note.txt"], "metadata": {
        "files": [{"file": "D:/r/note.txt", "patch": "@@ -1 +1 @@\n-a\n+b", "status": "modified"}]}}})
    assert edit.tool == "Edit" and edit.tool_input["file_path"] == "D:/r/note.txt" and "+b" in edit.tool_input["new_string"]
    assert oc.parse({"tool_name": "external_directory", "session_id": "s",
                     "tool_input": {"resources": ["C:/other/**"]}}).tool == "external_directory"
    assert not oc.blocks_own_prompt  # opencode keeps its question open: no PC window
    assert oc.allow() == {"decision": "allow"}


def _codex_question_run(approver_home, tool, tool_input):
    from approver.config import Config, State
    Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC").save()
    State(mode="on").save()
    sent = []

    class FakeLink:
        def __init__(self, *a):
            pass

        def send_request(self, request):
            sent.append(request)

        def request_and_wait(self, *a, **k):
            raise AssertionError("a Codex question must not wait for the phone")

    out = io.StringIO()
    raw = {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": "/r/kb",
           "session_id": "s-q", "turn_id": "t"}
    assert hook.run(io.StringIO(json.dumps(raw)), out, link_factory=FakeLink, agent="codex") == 0
    return sent, out.getvalue()


def test_codex_async_question_goes_to_the_phone_without_waiting(approver_home):
    sent, out = _codex_question_run(approver_home, "request_user_input_async", {"questions": [
        {"title": "Czy zatwierdzasz dodanie plików podglądu?", "options": ["Tak, zatwierdzam", "Tylko wersja tymczasowa"]}]})
    assert out == ""  # no opinion: Codex shows the question at once
    card = sent[0]
    assert card["tool"] == "AskUserQuestion" and card["needs_pc"] and card["answered_later"]
    assert card["title"].startswith("Question: Czy zatwierdzasz") and "• Tylko wersja tymczasowa" in card["preview"]
    assert card["expires_at"] - card["ts"] == 1800
    assert not list((approver_home / "pending").glob("*"))  # answered later: the next "working" takes it off


def test_codex_sync_question_card_leaves_when_the_tool_returns(approver_home):
    sent, out = _codex_question_run(approver_home, "request_user_input", {"questions": [
        {"id": "db", "header": "Baza", "question": "Którą bazę?", "options": [{"label": "SQLite", "description": "prosto"}]}]})
    assert out == "" and "answered_later" not in sent[0]
    assert "[Baza] Którą bazę?" in sent[0]["preview"] and "SQLite – prosto" in sent[0]["preview"]
    assert len(list((approver_home / "pending").glob("*"))) == 1  # PostToolUse finds it after the answer


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

        def request_and_wait(self, request, deadline, accept=None, stop=None):
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


def test_idle_sends_the_answer_only_when_replies_are_on(tmp_path, approver_home):
    from approver.config import Config, State
    config = Config(server="https://x", key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8", device_name="PC")
    config.save()
    State(mode="on").save()
    sent = []

    class FakeLink:
        def __init__(self, *a):
            pass

        def send_status(self, status):
            sent.append(status)

    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Gotowe, testy przechodzą."}]}}) + "\n", encoding="utf-8")
    payload = json.dumps({"session_id": "s7", "cwd": "/repo/app", "transcript_path": str(transcript)})
    hook.run_event(io.StringIO(payload), "claude", "idle", link_factory=FakeLink)
    config.send_replies = True
    config.save()
    hook.run_event(io.StringIO(payload), "claude", "working", link_factory=FakeLink)
    hook.run_event(io.StringIO(payload), "claude", "idle", link_factory=FakeLink)
    assert "reply" not in sent[0] and "reply" not in sent[1]
    assert sent[2]["reply"] == "Gotowe, testy przechodzą."


def test_late_idle_of_a_dead_session_sends_no_duration(monkeypatch, approver_home):
    """A session killed mid-task says idle days later (or never): that gap is not work."""
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
    for session, gap in (("s7", 7 * 3600), ("s13", 13 * 3600)):
        payload = json.dumps({"session_id": session, "cwd": "/r"})
        hook.run_event(io.StringIO(payload), "claude", "working", link_factory=FakeLink)
        now[0] += gap
        hook.run_event(io.StringIO(payload), "claude", "idle", link_factory=FakeLink)
    idles = [s for s in sent if s["state"] == "idle"]
    assert len(idles) == 2 and all("worked_s" not in s for s in idles)


def test_abandoned_task_starts_are_forgotten_after_12_hours(monkeypatch, approver_home):
    now = [1_800_000_000]
    monkeypatch.setattr(hook.time, "time", lambda: now[0])
    hook.log_path().parent.mkdir(parents=True, exist_ok=True)
    hook._task_clock("old", "working", now[0])
    hook._task_clock("new", "working", now[0] + 11 * 3600)
    hook._task_clock("x", "working", now[0] + 12 * 3600 + 1)
    tasks = json.loads(hook.log_path().with_name("tasks.json").read_text(encoding="utf-8"))
    assert "old" not in tasks and {"new", "x"} <= set(tasks)


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
    assert "event copilot working" in data["hooks"]["userPromptSubmitted"][0]["bash"]
    assert "event copilot idle" in data["hooks"]["agentStop"][0]["powershell"]
    assert "posttool copilot" in data["hooks"]["postToolUse"][0]["bash"]
    assert "command" not in data["hooks"]["permissionRequest"][0]  # Copilot would copy it to PowerShell as is
    assert install.copilot_installed() and len(install.copilot_commands()) == 5
    assert "posttool copilot" in data["hooks"]["postToolUseFailure"][0]["bash"]  # a failed command leaves the phone too
    assert install.uninstall_copilot() and not path.exists()


def test_install_copilot_quotes_paths_with_spaces_for_powershell(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    monkeypatch.setattr(install.sys, "_base_executable", r"C:\Program Files\Python312\python.exe", raising=False)
    entry = json.loads(install.install_copilot().read_text())["hooks"]["permissionRequest"][0]
    assert entry["bash"].startswith('"C:\\Program Files\\Python312\\python.exe" ')
    expected = "& " + entry["bash"] if os.name == "nt" else entry["bash"]
    assert entry["powershell"] == expected


def test_copilot_answer_in_terminal_matches_the_phone_question():
    """permissionRequest (toolInput + description) and postToolUse (toolArgs) give the same fingerprint."""
    c = agents.get("copilot")
    asked = c.parse({"sessionId": "s", "toolName": "powershell",
                     "toolInput": {"command": "npm test", "description": "Run tests"}})
    ran = c.parse({"sessionId": "s", "toolName": "powershell", "toolArgs": {"command": "npm test"},
                   "toolResult": {"resultType": "success"}})
    assert protocol.order_fp(asked.session_id, asked.tool, asked.tool_input) == \
        protocol.order_fp(ran.session_id, ran.tool, ran.tool_input)
