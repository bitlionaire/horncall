"""Agent contract: real input samples (tests/fixtures/agents) and one list of agents everywhere.

How samples are made and what `expect` means: docs/AGENTS.md.
"""

import io
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from approver import agents, compat, hook, install, protocol, setup, vitals

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = sorted((ROOT / "tests" / "fixtures" / "agents").rglob("*.json"))
COMPAT = json.loads((ROOT / "plugin" / "approver" / "compat.json").read_text(encoding="utf-8"))
NAMES = set(agents.AGENTS)


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: "/".join(p.parts[-3:]))
def test_fixture_parses_as_expected(path):
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert sample["agent"] in NAMES and sample["agent"] == path.parts[-3]
    raw, expect = sample["input"], sample.get("expect", {})
    if sample["kind"] in ("permission", "posttool"):
        hi = agents.get(sample["agent"]).parse(raw)
        if expect.get("no_opinion"):
            assert hi is None  # the agent asks the user its own way
            return
        if "tool" in expect:
            assert hi is not None and hi.tool == expect["tool"]
        for field in expect.get("nonempty", []):
            assert hi.tool_input.get(field), f"{field} is empty: the phone would show nothing"
        if "session" in expect:
            assert hi.session_id == expect["session"]
    elif "session" in expect:  # working / idle: run_event finds the chat by this
        assert (raw.get("session_id") or raw.get("sessionId")) == expect["session"]


@pytest.mark.parametrize("paired", [False, True])
@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: "/".join(p.parts[-3:]))
def test_no_opinion_means_empty_stdout(path, paired, capsys):
    """Gemini reads anything on stdout that isn't JSON as allow: the quiet paths must print nothing."""
    from approver import crypto
    from approver.config import Config, State
    if paired:
        Config(key=crypto.new_key(), device_name="PC").save()
        State(mode="off").save()
    sample = json.loads(path.read_text(encoding="utf-8"))
    agent = sample["agent"]
    for data in (json.dumps(sample["input"]), "not json", "[1]"):
        out = io.StringIO()
        assert hook.run(io.StringIO(data), out, agent=agent) == 0 and out.getvalue() == ""
        assert hook.run_posttool(io.StringIO(data), agent) == 0
        for state in ("working", "idle"):
            assert hook.run_event(io.StringIO(data), agent, state) == 0
    assert capsys.readouterr().out == ""


def test_every_agent_hooks_the_events_that_take_a_card_off_the_phone():
    """A card must leave the phone after success AND after failure; Gemini after every tool."""
    plugin_hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event in ("PostToolUse", "PostToolUseFailure"):
        assert event in install.CLAUDE_HOOKS and event in plugin_hooks
        assert plugin_hooks[event][0]["hooks"][0]["command"].endswith("/hooks/run.sh\" posttool claude")
        assert install.CLAUDE_HOOKS[event][1] == "posttool claude"
    assert set(plugin_hooks) == set(install.CLAUDE_HOOKS)  # the plugin and settings.json install stay the same
    for event, (_, args, timeout) in install.CLAUDE_HOOKS.items():
        h = plugin_hooks[event][0]["hooks"][0]
        args = "hook" if args == "hook claude" else args  # the plugin's permission hook: claude is the default agent
        assert h["command"] == f'sh "${{CLAUDE_PLUGIN_ROOT}}/hooks/run.sh" {args}' and h["timeout"] == timeout


def test_plugin_hooks_never_depend_on_a_bare_python():
    """`python` on PATH may be the Microsoft Store stub (only `py` works): every hook failed on a user's PC
    (2026-11-09). The plugin's hooks go through run.sh, which finds a real Python 3."""
    plugin_hooks = json.loads((ROOT / "plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for entries in plugin_hooks.values():
        for h in (h for e in entries for h in e["hooks"]):
            assert "args" not in h and not h["command"].startswith(("python", "py "))


@pytest.mark.skipif(not shutil.which("sh") and not Path(r"C:\Program Files\Git\bin\bash.exe").exists(),
                    reason="no POSIX shell")
def test_run_sh_skips_the_store_stub_and_falls_back_to_py(tmp_path):
    bash = shutil.which("sh") if os.name != "nt" else r"C:\Program Files\Git\bin\bash.exe"
    stub_dir = tmp_path / "Microsoft" / "WindowsApps"
    stub_dir.mkdir(parents=True)
    for name in ("python", "python3"):
        (stub_dir / name).write_text('#!/bin/sh\necho "Python was not found" >&2\nexit 9009\n', newline="\n")
        (stub_dir / name).chmod(0o755)
    real = tmp_path / "real"
    real.mkdir()
    # Stands in for `py` (Windows) — prints which interpreter run.sh picked and the arguments it got.
    (real / "py").write_text('#!/bin/sh\necho "py $*"\n', newline="\n")
    (real / "py").chmod(0o755)
    run_sh = str(ROOT / "plugin" / "hooks" / "run.sh")
    to_posix = lambda p: subprocess.run([bash, "-c", f'cygpath -u "{p}" 2>/dev/null || echo "{p}"'],  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
    path = f"{to_posix(stub_dir)}:{to_posix(real)}:/usr/bin:/bin"
    out = subprocess.run([bash, "-c", f'PATH="{path}" sh "$0" hook claude', run_sh], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("py -3 ") and out.stdout.rstrip().endswith("approver_launcher.py hook claude")
    # No Python at all: a clear message, a non-blocking failure.
    out = subprocess.run([bash, "-c", f'PATH="{to_posix(stub_dir)}:/usr/bin:/bin" sh "$0" hook', run_sh],
                         capture_output=True, text=True)
    assert out.returncode == 1 and "Python 3 not found" in out.stderr
    assert install.GEMINI_HOOKS["AfterTool"][1] == "posttool gemini"
    assert install.CODEX_HOOKS["PostToolUse"][1] == "posttool codex"
    copilot = install.copilot_file_content()["hooks"]
    assert copilot["postToolUse"] == copilot["postToolUseFailure"]


@pytest.mark.parametrize("asked, ran", [
    # older shape: argv list + justification only in the question
    ({"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "npm test"], "justification": "Run tests"}},
     {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "npm test"]}}),
    # newer shape: Claude-like Bash, a description in the question
    ({"tool_name": "Bash", "tool_input": {"command": "npm test", "description": "Run unit tests"}},
     {"tool_name": "Bash", "tool_input": {"command": "npm test"}, "tool_response": {"exit_code": 1}}),
])
def test_codex_posttool_matches_the_question_it_answers(asked, ran):
    c = agents.get("codex")
    before, after = c.parse({**asked, "session_id": "s", "cwd": "/r"}), c.parse({**ran, "session_id": "s", "cwd": "/r"})
    assert protocol.order_fp("s", before.tool, before.tool_input) == protocol.order_fp("s", after.tool, after.tool_input)


def test_every_agent_has_a_sample():
    assert {p.parts[-3] for p in FIXTURES} >= NAMES


def test_one_list_of_agents_everywhere():
    assert set(install.INSTALLERS) - {"claude-statusline"} == NAMES
    assert set(install.COMMANDS) - {"claude-statusline"} == NAMES
    assert set(vitals.READERS) == NAMES
    assert set(protocol.FACTION_RANKS) == NAMES
    assert {tool.id for tool in setup.detect_tools()} == NAMES
    assert set(COMPAT["agents"]) == NAMES


@pytest.mark.parametrize("name", sorted(NAMES))
def test_compat_entry_is_complete(name):
    entry = COMPAT["agents"][name]
    for key in ("title", "npm", "version_command", "baseline", "docs", "config", "hooks", "input_fields", "output", "watch"):
        assert entry.get(key), key
    assert entry["baseline"]["evidence"] in ("live", "field-names", "docs", "unit")
    assert {"permission"} <= {h["role"] for h in entry["hooks"].values()}
    adapter = agents.get(name)
    assert adapter.allow() == entry["output"]["allow"]
    assert set(adapter.deny("x")) == set(entry["output"]["deny"])


@pytest.mark.parametrize("name", sorted(NAMES))
def test_compat_entry_says_how_the_agent_updates_and_what_we_support(name):
    entry = COMPAT["agents"][name]
    assert entry["updates"]["mode"] in ("auto", "notify", "manual", "unknown")
    assert entry["updates"]["source"].startswith("https://") and entry["updates"]["checked"]
    support = entry["support"]
    assert support["policy"] in ("latest", "floor")
    assert support["floor_basis"]
    # the oldest supported version can't be newer than the one we checked
    assert compat.version_key(support["floor"]) and compat.version_key(support["floor"]) <= compat.version_key(entry["baseline"]["version"])


def test_floor_warning_names_the_agent_and_the_needed_version():
    spec = COMPAT["agents"]["gemini"]
    floor = spec["support"]["floor"]
    assert compat.floor_warning(spec, floor) is None
    assert compat.floor_warning(spec, "99.0.0") is None
    assert compat.floor_warning(spec, None) is None  # not installed: nothing to say
    line = compat.floor_warning(spec, "0.1.0")
    assert "Gemini CLI" in line and floor in line


def test_floor_warnings_look_only_at_the_listed_agents(monkeypatch):
    monkeypatch.setattr(compat, "installed_version", lambda spec: "0.0.1")
    assert len(compat.floor_warnings(["codex"])) == 1
    assert len(compat.floor_warnings()) == len(NAMES)
    assert compat.floor_warnings(["no-such-agent"]) == []


def test_status_warns_about_an_agent_below_its_floor(monkeypatch, capsys):
    from approver import cli, crypto
    from approver.config import Config
    Config(key=crypto.new_key(), device_name="PC").save()
    monkeypatch.setattr(cli.Ntfy, "ping", lambda self: True)
    monkeypatch.setattr(cli.Ntfy, "latest_versions", lambda self: {})
    monkeypatch.setitem(install.INSTALLERS, "codex", (*install.INSTALLERS["codex"][:2], lambda: True))
    monkeypatch.setattr(compat, "installed_version", lambda spec: "0.0.1")
    cli.cmd_status(None)
    assert "warning: OpenAI Codex CLI 0.0.1 is older than" in capsys.readouterr().out


# The agents' own config files: an AI editing them could switch the phone off.
CONFIG_FILES = {"claude": "/u/.claude/settings.json", "gemini": "/u/.gemini/settings.json",
                "copilot": "/u/.copilot/hooks/horncall.json", "codex": "/u/.codex/hooks.json",
                "codebuddy": "/u/.codebuddy/settings.json", "crush": "/u/.config/crush/crushrc",
                "opencode": "/u/.config/opencode/plugins/horncall.ts"}


def test_agent_config_files_are_sensitive():
    assert set(CONFIG_FILES) == NAMES
    for path in CONFIG_FILES.values():
        assert protocol.SENSITIVE_PATH.search(path), path


def test_app_knows_every_faction():
    skin = ROOT / "android" / "app" / "src" / "main" / "java" / "app" / "horncall" / "ui" / "skin" / "Skin.kt"
    if not skin.exists():
        pytest.skip("app sources are not in the public repo")
    text = skin.read_text(encoding="utf-8")
    for name in NAMES:
        assert f'Faction("{name}"' in text, name


def test_capture_redacts_values_and_keeps_names(monkeypatch, approver_home):
    monkeypatch.setenv("HORNCALL_CAPTURE", "1")
    monkeypatch.setattr(hook, "_agent_version", lambda agent: "9.9.9")
    raw = {"toolName": "powershell", "sessionId": "secret-id", "timestamp": 5,
           "toolInput": {"command": "type C:/Users/me/.ssh/id_rsa", "args": ["a", "b"]}}
    hook.run(io.StringIO(json.dumps(raw)), io.StringIO(), agent="copilot")
    [path] = (approver_home / "captures" / "copilot" / "9.9.9").glob("permission-powershell-*.json")
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert sample["input"]["toolName"] == "powershell" and sample["input"]["timestamp"] == 5
    assert sample["input"]["toolInput"] == {"command": "<redacted:28>", "args": ["<redacted:1>", "<redacted:1>"]}
    assert "secret" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize("capturing", [True, False])
def test_launcher_runs_the_hook_with_the_phone_off_only_when_capturing(capturing, monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location("launcher_under_test", ROOT / "plugin" / "hooks" / "approver_launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)  # HOME comes from HORNCALL_HOME: no state.json there = phone off
    ran = []
    monkeypatch.setattr(launcher, "venv_python", lambda: tmp_path)  # "the venv exists"
    monkeypatch.setattr(launcher, "run_in_venv", lambda args, **kw: ran.append(args) or 0)
    if capturing:
        monkeypatch.setenv("HORNCALL_CAPTURE", "1")
    for args in (["hook", "gemini"], ["event", "gemini", "working"], ["posttool", "copilot"]):
        monkeypatch.setattr(launcher.sys, "argv", ["approver_launcher.py", *args])
        assert launcher.main() == 0
    assert len(ran) == (3 if capturing else 0)


def test_no_capture_by_default(approver_home):
    hook.run(io.StringIO(json.dumps({"toolName": "bash"})), io.StringIO(), agent="copilot")
    assert not (approver_home / "captures").exists()
