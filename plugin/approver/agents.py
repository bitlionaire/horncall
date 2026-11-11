"""Adapters for the AI coding agents Horncall can answer for.

Each agent calls our hook with its own JSON on stdin and expects its own JSON on stdout. An adapter
turns the input into the common shape `protocol.build_request` takes (Claude Code's: tool_name,
tool_input, cwd, session_id) and the phone's decision back into the agent's answer. Printing nothing
always means "no opinion": the agent then asks the user its normal way.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from . import protocol


@dataclass
class HookInput:
    tool: str            # our tool name: Bash, Write, Edit, … (Claude Code's vocabulary)
    tool_input: dict
    cwd: str | None
    session_id: str | None
    # A question the agent shows on the PC no matter what (Codex request_user_input): the phone only hears about it.
    notify_only: bool = False
    # Answered later as a new user message (Codex request_user_input_async), not by the tool returning.
    answered_later: bool = False

    def as_claude(self) -> dict:
        return {"tool_name": self.tool, "tool_input": self.tool_input, "cwd": self.cwd, "session_id": self.session_id}


class Agent:
    name = "?"
    title = "?"
    # The agent kills the hook after its configured timeout; wait a bit less than that.
    max_wait_s = 1850
    # The agent shows nothing of its own while our hook waits ("Running hook"): Horncall opens a window on the PC
    # next to the phone (pcdialog). Claude Code shows its own dialog alongside, so it doesn't need one.
    blocks_own_prompt = True

    def parse(self, raw: dict) -> HookInput | None:
        """None = not something to ask the phone about (let the agent decide)."""
        raise NotImplementedError

    def allow(self) -> dict:
        raise NotImplementedError

    def deny(self, message: str) -> dict:
        raise NotImplementedError


class Claude(Agent):
    """Claude Code `PermissionRequest` hook (the plugin's hooks.json)."""
    name, title = "claude", "Claude Code"
    blocks_own_prompt = False

    def parse(self, raw):
        tool_input = raw.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            tool_input = {"input": tool_input}
        return HookInput(str(raw.get("tool_name", "?")), tool_input, raw.get("cwd"), raw.get("session_id"))

    def allow(self):
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}

    def deny(self, message):
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest",
                                       "decision": {"behavior": "deny", "message": message}}}


class Gemini(Agent):
    """Gemini CLI `BeforeTool` hook. It fires for every tool, so only risky ones go to the phone."""
    name, title = "gemini", "Gemini CLI"

    # Gemini tool → (our tool, how to map its arguments)
    TOOLS = {
        "run_shell_command": "Bash",
        "write_file": "Write",
        "replace": "Edit",
    }

    # MCP tools (mcp_<server>_<tool>) whose name says they only look: no phone card for those.
    READ_ONLY_WORDS = {"get", "list", "read", "search", "find", "fetch", "view", "show", "query", "describe",
                       "lookup", "status", "info", "count", "check"}

    def parse(self, raw):
        tool = str(raw.get("tool_name", ""))
        ours = self.TOOLS.get(tool)
        args = raw.get("tool_input") or {}
        if not isinstance(args, dict):
            args = {"input": args}
        if tool == "ask_user":
            # Gemini 0.28+ asks the user itself (also with --yolo) and waits: a card for the phone, the answer is
            # given in Gemini. Claude's shape already: header, question, options [{label, description}], multiSelect.
            questions = [{k: v for k, v in {"header": q.get("header"), "question": q.get("question"),
                                             "options": q.get("options"), "multiSelect": q.get("multiSelect")}.items() if v}
                         for q in args.get("questions") or [] if isinstance(q, dict)]
            return HookInput("AskUserQuestion", {"questions": questions}, raw.get("cwd"), raw.get("session_id"),
                             notify_only=True)
        if ours is None and tool.startswith("mcp_"):
            # With `gemini --yolo` nothing else stops them, so ask unless the name says read-only.
            words = protocol.tool_words(tool)
            if words & self.READ_ONLY_WORDS and not words & protocol.RISKY_TOOL_WORDS:
                return None
            return HookInput(tool, args, raw.get("cwd"), raw.get("session_id"))
        if ours is None:
            return None  # reads, searches, web fetches: Gemini's own rules decide
        if ours == "Bash":
            tool_input = {"command": args.get("command", ""), "description": args.get("description")}
            if args.get("dir_path") or args.get("directory"):
                tool_input["description"] = (tool_input["description"] or "") + f" (in {args.get('dir_path') or args.get('directory')})"
        elif ours == "Write":
            tool_input = {"file_path": args.get("file_path") or args.get("path", ""), "content": args.get("content", "")}
        else:
            tool_input = {"file_path": args.get("file_path") or args.get("path", ""),
                          "old_string": args.get("old_string", ""), "new_string": args.get("new_string", "")}
        tool_input = {k: v for k, v in tool_input.items() if v is not None}
        return HookInput(ours, tool_input, raw.get("cwd"), raw.get("session_id"))

    def allow(self):
        return {"decision": "allow"}

    def deny(self, message):
        return {"decision": "deny", "reason": message}


class Copilot(Agent):
    """GitHub Copilot CLI `permissionRequest` hook (runs only when Copilot would ask the user).

    Its input schema isn't documented; this accepts the camelCase and snake_case shapes Copilot uses
    for its other hooks. `hook.py` logs the input's field NAMES (never values) so a real sample can
    confirm the mapping.
    """
    name, title = "copilot", "GitHub Copilot CLI"

    SHELL = {"bash", "shell", "powershell", "run", "terminal", "run_in_terminal"}
    WRITE = {"write", "create", "create_file", "write_file"}
    EDIT = {"edit", "str_replace", "str_replace_editor", "replace", "edit_file"}

    def parse(self, raw):
        tool = str(raw.get("toolName") or raw.get("tool_name") or raw.get("kind") or raw.get("tool") or "?")
        args = (raw.get("toolInput") or raw.get("toolArgs") or raw.get("tool_input") or raw.get("arguments")
                or raw.get("args") or {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"command": args}
        if not isinstance(args, dict):
            args = {"input": args}
        if args.get("requestSandboxBypass"):
            return None  # Copilot ignores an allow for leaving its sandbox: a phone card would promise too much
        low = tool.lower()
        if low in self.SHELL or "command" in args and low not in self.WRITE | self.EDIT:
            ours = "PowerShell" if low == "powershell" else "Bash"
            tool_input = {"command": args.get("command") or args.get("fullCommandText") or "",
                                        "description": args.get("description")}
        elif low in self.WRITE:
            ours, tool_input = "Write", {"file_path": args.get("path") or args.get("file_path", ""),
                                         "content": args.get("content") or args.get("file_text", "")}
        elif low in self.EDIT:
            ours, tool_input = "Edit", {"file_path": args.get("path") or args.get("file_path", ""),
                                        "old_string": args.get("old_str") or args.get("old_string", ""),
                                        "new_string": args.get("new_str") or args.get("new_string")
                                                      or args.get("diff", "")}  # 1.0.98 `edit`: {file_path, diff}
        else:
            ours, tool_input = tool, args
        tool_input = {k: v for k, v in tool_input.items() if v is not None}
        return HookInput(ours, tool_input, raw.get("cwd"), raw.get("sessionId") or raw.get("session_id"))

    def allow(self):
        return {"behavior": "allow"}

    def deny(self, message):
        return {"behavior": "deny", "message": message}


class Codex(Claude):
    """OpenAI Codex CLI `PermissionRequest` hook (~/.codex/hooks.json): Claude Code's wire format.

    Its shell tool may pass the command as an argv list, and edits come as one apply_patch text.
    """
    name, title = "codex", "OpenAI Codex CLI"
    blocks_own_prompt = True  # its PermissionRequest hook runs before its own prompt (Claude's subclass, not its UI)
    # Its hooks.json keeps timeout 600: changing a trusted hook makes the user re-approve it in /hooks.
    max_wait_s = 580

    SHELL = {"shell", "bash", "exec_command", "local_shell", "container.exec", "unified_exec"}
    # Its questions to the user: they need no approval, so only PreToolUse sees them (hook.run sends a card, waits for nothing).
    QUESTIONS = {"request_user_input", "request_user_input_async"}

    def parse(self, raw):
        hi = super().parse(raw)
        low = hi.tool.lower()
        args = hi.tool_input
        if low in self.QUESTIONS:
            # request_user_input: Claude's shape (header, question, options {label, description});
            # request_user_input_async: {title, options: [text]}, answered later as a new user message.
            questions = []
            for q in args.get("questions") or []:
                if not isinstance(q, dict):
                    continue
                options = [o if isinstance(o, dict) else {"label": str(o)} for o in q.get("options") or []]
                questions.append({k: v for k, v in {"header": q.get("header"), "question": q.get("question") or q.get("title"),
                                                     "options": options}.items() if v})
            return HookInput("AskUserQuestion", {"questions": questions}, hi.cwd, hi.session_id,
                             notify_only=True, answered_later=low.endswith("_async"))
        if low in self.SHELL or isinstance(args.get("command"), list):
            command = args.get("command") or args.get("cmd") or ""
            if isinstance(command, list):
                # ["bash", "-lc", "npm test"] → the script; anything else → joined argv
                command = command[-1] if len(command) >= 3 and command[1] in ("-lc", "-c") else " ".join(map(str, command))
            # The older shape explains itself in `justification`, the Claude-like `Bash` shape in `description`.
            hi = HookInput("Bash", {"command": str(command),
                                    "description": args.get("description") or args.get("justification")},
                           hi.cwd, hi.session_id)
        elif low in ("apply_patch", "applypatch"):
            patch = str(args.get("input") or args.get("patch") or args.get("command") or json.dumps(args))
            files, deletes = protocol.patch_files(patch, hi.cwd)
            hi = HookInput("Edit", {"file_path": files[0] if files else protocol.PATCH_UNKNOWN, "old_string": "",
                                    "new_string": patch, "patch_files": files, "patch_deletes": deletes},
                           hi.cwd, hi.session_id)
        hi.tool_input = {k: v for k, v in hi.tool_input.items() if v is not None}
        return hi


class CodeBuddy(Claude):
    """Tencent CodeBuddy Code `PermissionRequest` hook (~/.codebuddy/settings.json): Claude Code's wire format,
    the same tool names (Bash, Write, Edit, …). PermissionRequest exists since 2.97.0 (its CHANGELOG)."""
    name, title = "codebuddy", "CodeBuddy Code"
    # Unverified whether it shows its own prompt next to the hook like Claude Code: until a live test says so,
    # Horncall's PC window shows the order too (a second dialog beats none).
    blocks_own_prompt = True


class Crush(Agent):
    """Charm's Crush `PreToolUse` hook (~/.config/crush/crushrc): Claude-like payload, lower-case tool names.

    It runs before Crush's own permission check: "allow" pre-approves and skips its prompt, "deny" blocks,
    silence falls through to its prompt. It fires for every matched tool, also ones Crush would let through
    unasked, so the crushrc matcher lists only the tools that change things.
    """
    name, title = "crush", "Crush"
    TOOLS = {"bash": "Bash", "edit": "Edit", "multiedit": "MultiEdit", "write": "Write", "download": "WebFetch",
             "fetch": "WebFetch", "web_fetch": "WebFetch"}

    def parse(self, raw):
        tool = str(raw.get("tool_name") or "")
        args = raw.get("tool_input") or {}
        if not isinstance(args, dict):
            args = {"input": args}
        cwd, session = raw.get("cwd"), raw.get("session_id")
        if tool == "question":
            # Crush asks the user itself and waits: a card for the phone, the answer is given in Crush.
            questions = [{k: v for k, v in {"header": q.get("label"), "question": q.get("question"),
                                             "options": q.get("choices") or q.get("options")}.items() if v}
                         for q in args.get("questions") or [] if isinstance(q, dict)]
            return HookInput("AskUserQuestion", {"questions": questions}, cwd, session, notify_only=True)
        ours = self.TOOLS.get(tool)
        if ours is None and not tool.startswith(("mcp_", "lsp_rename", "lsp_replace_symbol")):
            return None  # reads and lookups: Crush decides
        return HookInput(ours or tool, args, cwd, session)

    def allow(self):
        return {"decision": "allow"}

    def deny(self, message):
        return {"decision": "deny", "reason": message}


class Opencode(Agent):
    """opencode 2, through our plugin (opencode_plugin.ts): it hands over each `permission.asked` request as
    {tool_name: action, tool_input: {resources, metadata}} and replies with what we print. opencode keeps its own
    question open meanwhile (like Claude Code), so no PC window is needed."""
    name, title = "opencode", "opencode"
    blocks_own_prompt = False

    def parse(self, raw):
        action = str(raw.get("tool_name") or "")
        args = raw.get("tool_input") or {}
        resources = [str(r) for r in args.get("resources") or []]
        meta = args.get("metadata") or {}
        cwd, session = raw.get("cwd"), raw.get("session_id")
        first = resources[0] if resources else ""
        if action == "question":
            # opencode's own question to the user (its `question` tool, seen in session.tool.called, 2026-11-11):
            # a card for the phone, the answer is given in opencode.
            questions = [{k: v for k, v in {"header": q.get("header"), "question": q.get("question"),
                                             "options": q.get("options"), "multiSelect": q.get("multiple")}.items() if v}
                         for q in args.get("questions") or [] if isinstance(q, dict)]
            return HookInput("AskUserQuestion", {"questions": questions}, cwd, session, notify_only=True)
        if action in ("shell", "bash"):
            command = {"command": "\n".join(resources)}
            if meta.get("description"):
                command["description"] = str(meta["description"])
            return HookInput("Bash", command, cwd, session)
        if action == "edit":
            files = [f for f in meta.get("files") or [] if isinstance(f, dict)]
            paths = [str(f.get("file")) for f in files if f.get("file")] or resources
            # Each patch under its file's name, and every path in patch_files: risk checks see a later file too.
            patch = "\n\n".join(f"--- {f.get('file')}\n{f.get('patch') or ''}" for f in files).strip()
            return HookInput("Edit", {"file_path": paths[0] if paths else first, "old_string": "",
                                      "new_string": patch or "(no diff)", "patch_files": paths}, cwd, session)
        if action in ("webfetch", "websearch"):
            key = "url" if action == "webfetch" else "query"
            return HookInput("WebFetch" if action == "webfetch" else "WebSearch", {key: first}, cwd, session)
        if action in ("read", "glob", "grep", "list"):
            return HookInput("Read", {"file_path": first}, cwd, session)
        # external_directory, skill, subagent, MCP tools (<server>_<tool>) …: shown as they come
        return HookInput(action or "?", {"resources": resources}, cwd, session)

    def allow(self):
        return {"decision": "allow"}

    def deny(self, message):
        return {"decision": "deny", "reason": message}


AGENTS: dict[str, Agent] = {a.name: a for a in (Claude(), Gemini(), Copilot(), Codex(), CodeBuddy(), Crush(), Opencode())}


def get(name: str | None) -> Agent:
    return AGENTS.get((name or "claude").lower(), AGENTS["claude"])
