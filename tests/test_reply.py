"""Answer previews on trimmed samples of each AI's real chat files (shapes checked 2026-11-02, texts made up)."""
import json

from approver import reply, vitals
from test_vitals import write_jsonl

CLAUDE = {"type": "assistant", "message": {"role": "assistant", "content": [
    {"type": "text", "text": "**Gotowe.** Dodałem testy:\n\n```python\nassert x\n```\n- `reply.py` czyta odpowiedź"}]}}
CLAUDE_TOOL = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}
CODEX = {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer",
                                              "content": [{"type": "output_text", "text": "Plik utworzony."}]}}
CODEX_DONE = {"type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Zrobione z task_complete."}}
GEMINI = {"type": "gemini", "content": "The command execution was denied.", "tokens": {"input": 11655}}
GEMINI_TOOL = {"type": "gemini", "content": "", "tokens": {"input": 11700}}
COPILOT = {"type": "assistant.message", "data": {"content": "Done.", "toolRequests": [], "phase": "final_answer"}}


def test_each_ai_gives_its_last_answer(tmp_path, monkeypatch):
    path = write_jsonl(tmp_path / "c.jsonl", CLAUDE, CLAUDE_TOOL, {"type": "cost-state"})
    assert reply.read("claude", {"transcript_path": path}) == "Gotowe. Dodałem testy: reply.py czyta odpowiedź"
    path = write_jsonl(tmp_path / "x.jsonl", CODEX, {"type": "event_msg", "payload": {"type": "token_count"}})
    assert reply.read("codex", {"transcript_path": path}) == "Plik utworzony."
    path = write_jsonl(tmp_path / "x2.jsonl", CODEX_DONE)
    assert reply.read("codex", {"transcript_path": path}) == "Zrobione z task_complete."
    # Codex's Stop hook carries the answer itself: no file needed.
    assert reply.read("codex", {"last_assistant_message": "Prosto z hooka.", "transcript_path": path}) == "Prosto z hooka."
    path = write_jsonl(tmp_path / "g.jsonl", GEMINI, GEMINI_TOOL, {"$set": {"lastUpdated": "x"}})
    assert reply.read("gemini", {"transcript_path": path}) == "The command execution was denied."
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    (tmp_path / "session-state" / "s1").mkdir(parents=True)
    write_jsonl(tmp_path / "session-state" / "s1" / "events.jsonl", COPILOT, {"type": "assistant.turn_end"})
    assert reply.read("copilot", {"sessionId": "s1"}) == "Done."
    assert reply.read("copilot", {"sessionId": "../s1"}) is None
    assert reply.read("codebuddy", {"last_assistant_message": "Gotowe."}) == "Gotowe."


def test_long_answers_are_cut_on_a_word():
    text = reply.plain("słowo " * 100)
    assert len(text) <= reply.MAX_CHARS + 1 and text.endswith("słowo…")
    assert reply.plain("```\nonly code\n```") is None
    assert reply.plain("# Nagłówek\n> cytat\n1. [link](http://x) i ~~skreślone~~") == "Nagłówek cytat link i skreślone"


def test_broken_or_missing_files_mean_no_preview(tmp_path):
    (tmp_path / "bad.jsonl").write_text("not json\n{\n")
    for agent in ("claude", "codex", "gemini", "copilot", "unknown"):
        assert reply.read(agent, {"transcript_path": str(tmp_path / "bad.jsonl")}) is None
        assert reply.read(agent, {"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert reply.read("claude", "nonsense") is None


def test_copilot_context_from_the_last_model_call(tmp_path, monkeypatch):
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    (tmp_path / "session-state" / "s2").mkdir(parents=True)
    usage = {"type": "session.usage_record", "data": {"usage": {"inputTokens": 68000, "maxPromptTokens": 272000}}}
    write_jsonl(tmp_path / "session-state" / "s2" / "events.jsonl", usage, COPILOT)
    assert vitals.read("copilot", {"sessionId": "s2"}) == {"ctx_left": 75}
