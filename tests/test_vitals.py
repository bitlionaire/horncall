"""HP readers on trimmed samples of each AI's real files (numbers kept, content removed)."""
import json
import subprocess
import sys
from pathlib import Path

from approver import install, vitals

CODEX = {"timestamp": "2026-10-16T21:53:52Z", "type": "event_msg", "payload": {"type": "token_count", "info": {
    "last_token_usage": {"input_tokens": 122837, "cached_input_tokens": 122240, "output_tokens": 132},
    "model_context_window": 258400}, "rate_limits": {"limit_id": "codex", "primary": {
        "used_percent": 3.0, "window_minutes": 43200, "resets_at": 1794777480}, "secondary": None,
    "plan_type": "free", "rate_limit_reached_type": None}}}

COPILOT = {"type": "model.model_call_success", "data": {"quotaSnapshots": {
    "chat": {"isUnlimitedEntitlement": False, "entitlementRequests": 200, "remainingPercentage": 99.8,
             "resetDate": "2026-11-01T00:00:00Z"},
    "premium_interactions": {"isUnlimitedEntitlement": False, "entitlementRequests": 0, "remainingPercentage": 0}}}}


def write_jsonl(path: Path, *rows) -> str:
    path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n", encoding="utf-8")
    return str(path)


def test_codex_plan_and_context(tmp_path):
    path = write_jsonl(tmp_path / "rollout.jsonl", {"type": "session_meta"}, CODEX)
    assert vitals.read("codex", {"transcript_path": path}) == {
        "ctx_left": 52, "plan_left": 97, "plan_window": "month", "plan_resets": 1794777480}


def test_copilot_monthly_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    folder = tmp_path / "session-state" / "s1"
    folder.mkdir(parents=True)
    write_jsonl(folder / "events.jsonl", COPILOT, *[{"type": "assistant.message", "n": i} for i in range(5000)])
    v = vitals.read("copilot", {"sessionId": "s1"})
    assert v["plan_left"] == 100 and v["plan_window"] == "month" and v["plan_resets"] > 1_790_000_000
    assert vitals.read("copilot", {"sessionId": "../evil"}) is None


def test_gemini_context_then_out_of_quota(tmp_path):
    ok = {"type": "gemini", "tokens": {"input": 17688, "output": 67, "total": 18017}, "model": "gemini-3.8-flash"}
    path = write_jsonl(tmp_path / "chat.jsonl", ok)
    assert vitals.read("gemini", {"transcript_path": path}) == {"ctx_left": 98}
    path = write_jsonl(tmp_path / "chat.jsonl", ok, {"type": "error", "content": "[API Error: You have exhausted your daily quota on this model.]"})
    v = vitals.read("gemini", {"transcript_path": path})
    assert v["plan_left"] == 0 and v["exhausted"] is True


def test_claude_context_from_transcript_and_plan_from_status_line(tmp_path, approver_home):
    entry = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 4250, "cache_read_input_tokens": 75748, "output_tokens": 589}}}
    path = write_jsonl(tmp_path / "t.jsonl", entry)
    assert vitals.read("claude", {"transcript_path": path, "session_id": "s"}) == {"ctx_left": 60}
    approver_home.mkdir(parents=True, exist_ok=True)
    import time
    (approver_home / "claude-vitals.json").write_text(json.dumps({"s": {
        "t": time.time(), "five_hour": {"used_percentage": 23.5, "resets_at": 1738425600},
        "seven_day": {"used_percentage": 41.2, "resets_at": 1738857600}, "ctx_left": 92}}))
    assert vitals.read("claude", {"transcript_path": path, "session_id": "s"}) == {
        "plan_left": 59, "plan_window": "7d", "plan_resets": 1738857600, "ctx_left": 92}


def test_broken_files_mean_no_bar(tmp_path):
    (tmp_path / "x.jsonl").write_text("not json\n{\n")
    for agent in ("claude", "codex", "gemini"):
        assert vitals.read(agent, {"transcript_path": str(tmp_path / "x.jsonl")}) is None
        assert vitals.read(agent, {"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert vitals.read("claude", "nonsense") is None


def test_status_line_records_limits_and_wraps_the_old_one(tmp_path, monkeypatch, approver_home):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "settings.json").write_text(json.dumps({"statusLine": {"type": "command", "command": "echo MINE"}}))
    install.install_claude_statusline()
    assert install.claude_statusline_installed()
    launcher = Path(install.LAUNCHER)
    payload = {"session_id": "s9", "model": {"display_name": "Opus"},
               "rate_limits": {"five_hour": {"used_percentage": 10, "resets_at": 1}},
               "context_window": {"used_percentage": 30, "remaining_percentage": 70}}
    out = subprocess.run([sys.executable, str(launcher), "statusline"], input=json.dumps(payload).encode(),
                         capture_output=True, env={**__import__("os").environ, "HORNCALL_HOME": str(approver_home)})
    assert b"MINE" in out.stdout  # the user's own status line still shows
    saved = json.loads((approver_home / "claude-vitals.json").read_text())
    assert saved["s9"]["five_hour"]["used_percentage"] == 10 and saved["s9"]["ctx_left"] == 70
    assert install.uninstall_claude_statusline()
    assert json.loads((tmp_path / "settings.json").read_text())["statusLine"]["command"] == "echo MINE"
