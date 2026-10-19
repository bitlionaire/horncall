"""`approver setup` (setup.cmd / setup.sh in the ZIP): the whole installation in one guided run.

1. finds the AI coding tools on this PC, 2. hooks up the ones you pick (Claude Code gets the plugin),
3. pairs the phone (QR in the browser), 4. sends a test order that also makes the phone trusted.
One pairing serves every AI on this PC. Running it again is safe: it only adds what is missing.
"""

from __future__ import annotations

import html
import json
import os
import sys
import shutil
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import crypto, install, phones, protocol
from .config import Config, app_dir, home_dir
from .link import Link

MARKETPLACE_ROOT = install.PLUGIN_ROOT.parent  # the unpacked ZIP / repo: .claude-plugin/marketplace.json


@dataclass
class Tool:
    id: str
    title: str
    found: bool
    state: str                   # "missing", "here" (this installation) or "elsewhere" (an older copy)
    changes: str                 # what connecting it changes, shown before asking to continue
    hook_up: Callable[[], str]   # returns a short note for the summary

    @property
    def hooked(self) -> bool:
        return self.state == "here"


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def claude_plugin_state() -> str:
    """'here' when Claude Code's horncall marketplace is this folder, 'elsewhere' for another copy."""
    try:
        installed = json.loads((claude_dir() / "plugins" / "installed_plugins.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "missing"
    if "horncall@horncall" not in json.dumps(installed):
        return "missing"
    try:
        known = json.loads((claude_dir() / "plugins" / "known_marketplaces.json").read_text(encoding="utf-8"))
        path = (known.get("horncall") or {}).get("source", {}).get("path", "")
    except (OSError, ValueError, AttributeError):
        path = ""
    if not path:  # installed from GitHub or unknown: leave it to Claude's own updates
        return "here"
    return "here" if Path(path).resolve() == MARKETPLACE_ROOT.resolve() else "elsewhere"


def claude_plugin_installed() -> bool:
    return claude_plugin_state() != "missing"


def install_claude_plugin(run=subprocess.run) -> str:
    claude = shutil.which("claude")
    if not claude:
        return f"Claude Code: in Claude type /plugin marketplace add {MARKETPLACE_ROOT.as_posix()} then /plugin install horncall@horncall"
    steps = [["plugin", "marketplace", "add", str(MARKETPLACE_ROOT)], ["plugin", "install", "horncall@horncall"]]
    if claude_plugin_state() == "elsewhere":  # an older copy in another folder: point Claude at this one
        steps.insert(0, ["plugin", "marketplace", "remove", "horncall"])
    for args in steps:
        result = run([claude, *args], capture_output=True, text=True, timeout=120)
        # "already" = installed earlier: fine.
        if result.returncode != 0 and "already" not in (result.stdout + result.stderr).lower():
            raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return "Claude Code: plugin installed — restart Claude Code"


def uninstall_claude_plugin(run=subprocess.run) -> str:
    claude = shutil.which("claude")
    if not claude:
        return "Claude Code: in Claude type /plugin uninstall horncall@horncall and /plugin marketplace remove horncall"
    if claude_plugin_state() == "missing":
        return "Claude Code: plugin not installed"
    for args in (["plugin", "uninstall", "horncall@horncall"], ["plugin", "marketplace", "remove", "horncall"]):
        run([claude, *args], capture_output=True, text=True, timeout=120)
    return "Claude Code: plugin removed — restart Claude Code"


def _found(command: str, folder: str) -> bool:
    return bool(shutil.which(command)) or (Path.home() / folder).exists()


def _files(agent: str) -> str:
    return ", ".join(str(path) for path, _ in install.preview(agent))


def detect_tools() -> list[Tool]:
    def hook(agent: str, note: str) -> Callable[[], str]:
        def run() -> str:
            install.INSTALLERS[agent][0]()
            return note
        return run

    claude_found = bool(shutil.which("claude")) or claude_dir().exists()
    if install.packaged():  # pip/pipx: no plugin folder — Claude's hooks go into its settings.json
        claude = Tool("claude", "Claude Code", claude_found, install.state("claude"), _files("claude"),
                      hook("claude", "Claude Code: hooked up — restart it"))
    else:
        claude = Tool("claude", "Claude Code", claude_found, claude_plugin_state(),
                      f"Claude Code plugin from {MARKETPLACE_ROOT}", install_claude_plugin)
    return [
        claude,
        Tool("gemini", "Gemini CLI", _found("gemini", ".gemini"), install.state("gemini"), _files("gemini"),
             hook("gemini", "Gemini CLI: hooked up — start it with:  gemini --yolo   (so the phone is the only question)")),
        Tool("copilot", "GitHub Copilot CLI", _found("copilot", ".copilot"), install.state("copilot"), _files("copilot"),
             hook("copilot", "Copilot CLI: hooked up — restart it")),
        Tool("codex", "OpenAI Codex CLI", _found("codex", ".codex"), install.state("codex"), _files("codex"),
             hook("codex", "Codex CLI: hooked up — in Codex open /hooks and switch the 3 Horncall hooks on")),
    ]


def remove_app_dir() -> str:
    """Deletes the program folder when it is the standard one setup installed (never a repo or a ZIP folder)."""
    app = app_dir()
    if install.packaged():
        return "Program: installed with pip/pipx — remove it with: pipx uninstall horncall"
    if MARKETPLACE_ROOT.resolve() != app.resolve():
        return f"Program folder {MARKETPLACE_ROOT} is not the standard one — left as it is"
    shutil.rmtree(app, ignore_errors=True)
    return f"Program folder {app} removed" if not app.exists() else f"Delete {app} by hand (files in use)"


def headless() -> bool:
    """No browser to open here: an SSH session, or Linux without a display (VPS, container, WSL)."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return True
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def pairing_page(config: Config, uri: str) -> Path:
    """A local page with a big QR code (consoles draw QR codes badly)."""
    import qrcode
    from qrcode.image.svg import SvgPathImage
    svg = qrcode.make(uri, image_factory=SvgPathImage, box_size=12).to_string(encoding="unicode")
    page = home_dir() / "pair.html"
    page.write_text(f"""<!doctype html><meta charset="utf-8"><title>Horncall — pair your phone</title>
<style>body{{background:#0E1013;color:#E8E6E1;font:16px system-ui;text-align:center;padding:24px}}
.qr{{background:#fff;display:inline-block;padding:16px;border-radius:12px}} code{{word-break:break-all;color:#9AA0A8}}
h1{{color:#FFC857;letter-spacing:.08em}}</style>
<h1>HORNCALL</h1><p>Open Horncall on the phone → <b>Scan QR</b> (Polish: <b>Skanuj QR</b>) → scan this code.</p>
<div class="qr">{svg}</div>
<p>PC: <b>{html.escape(config.device_name)}</b></p><p>No camera? Paste this code in the app (<i>Paste link</i>):</p>
<p><code>{html.escape(uri)}</code></p><p>Then go back to the setup window.</p>""", encoding="utf-8")
    return page


def run(ask: Callable[[str], str] = input, say: Callable[[str], None] = print,
        open_page: Callable[[str], bool] = webbrowser.open, link_factory=Link, test_timeout: int = 120) -> int:
    from .cli import pairing_uri, print_qr

    raw_ask = ask

    def ask(question: str) -> str:  # noqa: F811 — closed/redirected input counts as "Enter"
        try:
            return raw_ask(question)
        except EOFError:
            return ""

    def yes(question: str) -> bool:
        return ask(question).strip().lower() in ("", "y", "yes", "t", "tak")

    say("\n=== Horncall setup ===\n")
    say(f"Program: {install.target() if install.packaged() else MARKETPLACE_ROOT}\n")
    say("1/4  AI tools on this PC")
    tools = detect_tools()
    labels = {"here": "hooked up", "elsewhere": "hooked up to an older copy — will be moved here", "missing": "found"}
    for t in tools:
        say(f"   {'✓' if t.found else '·'} {t.title:20} {labels[t.state] if t.found else 'not found'}")
    notes: list[str] = []
    say("\n2/4  Hook up")
    if not any(t.found for t in tools):
        say("   No AI tool found. Install one (e.g. Claude Code) and run setup again.")
    # Ask first, change nothing yet: an older copy is moved without asking (it was chosen before).
    chosen = [t for t in tools if t.found and (t.state == "elsewhere" or (t.state == "missing" and yes(f"   Connect {t.title}? [Y/n] ")))]
    claude = next(t for t in tools if t.id == "claude")
    statusline = claude.found and install.state("claude-statusline") != "here" and (
        install.state("claude-statusline") == "elsewhere"
        or yes("   Show Claude's plan limit (5 h / week) as HP on the phone? It adds a small status line in Claude Code. [Y/n] "))
    if chosen or statusline:
        say("\n   This will change:")
        for t in chosen:
            say(f"   • {t.title}: {t.changes}")
        if statusline:
            say(f"   • Claude Code status line: {_files('claude-statusline')}")
        say("   Other settings in those files stay as they are; each file gets a one-time *.horncall-backup copy.")
        me = "horncall" if install.packaged() else f'python "{install.LAUNCHER}"'
        say(f"   (To see the exact entries: {me} install <tool> --print)")
        if not yes("   Continue? [Y/n] "):
            chosen, statusline = [], False
            say("   Nothing changed.")
    for t in chosen:
        try:
            notes.append(t.hook_up())
            say(f"   ✓ {t.title}")
        except Exception as exc:  # one tool failing must not stop the rest
            say(f"   ✗ {t.title}: {exc}")
    if statusline:
        try:
            install.install_claude_statusline()
            notes.append("Claude Code: status line added (your previous one, if any, still shows) — HP after the next reply")
        except Exception as exc:
            say(f"   ✗ status line: {exc}")

    say("\n3/4  Phone")
    config = Config.load()
    if config is None:
        config = Config(key=crypto.new_key())
        say("   New pairing key created.")
    phones.open_pairing(config)
    config.save()
    if config.phones:
        say(f"   Already paired with {len(config.phones)} phone(s). A new phone can join in the next 10 min.")
    uri = pairing_uri(config)
    opened = False
    if not headless():
        try:
            opened = open_page(pairing_page(config, uri).as_uri())
        except (ImportError, OSError):
            opened = False
    if opened:
        say("   Scan the QR code in the browser (in the app: Scan QR)")
    else:  # over SSH, on a server without a screen, or no browser: the QR right here in the terminal
        say("   Scan this QR code in the app (Scan QR). Too big? Widen the window or zoom out.")
        print_qr(uri, say)
    say(f"   No camera? In the app tap Paste link and paste:\n   {uri}")
    ask("   Press Enter when the phone shows this PC in the barracks… ")

    say("\n4/4  Test order — answer it on the phone")
    link = link_factory(config.key, config.server)
    request = protocol.build_request(
        {"tool_name": "Test", "tool_input": {"description": "Horncall setup", "message": "Hup hup! Horncall is ready."},
         "cwd": None}, config.device_name, test_timeout, config.max_preview_chars)
    request["label"] = "Horncall setup"
    joined: list[str] = []

    def accept(response: dict) -> bool:
        verdict = phones.judge(config, response)
        if verdict == "joined":
            joined.append(crypto.fingerprint(response["pub"]))
        return verdict in ("ok", "joined")

    try:
        response = link.request_and_wait(request, time.time() + test_timeout, accept=accept)
    except Exception as exc:
        response = None
        say(f"   Network problem: {exc.__class__.__name__}")
    if response is None:
        say("   No answer. Check the phone (Listen for orders on, internet), then: " + ("horncall" if install.packaged() else "approver") + " test")
    else:
        say(f"   ✓ The phone answered ({response['decision']})" + (f" — trusted, key {joined[0]}" if joined else ""))

    # The steps left to the user: one per tool, numbered, so they read as a to-do list (Codex's hook
    # approval in /hooks is easy to miss otherwise).
    say("\n=== Done — still to do ===")
    for n, note in enumerate(notes, 1):
        say(f" {n}. {note}")
    if install.packaged():
        say("\nLeaving the desk: horncall on   Back: horncall off")
    else:
        say(f'\nLeaving the desk: /horncall:approver on   Back: /horncall:approver off\n'
            f'(without Claude Code: python "{install.LAUNCHER}" on)')
    ask("\nRead the list above? Press Enter to close… ")  # setup.cmd's window closes after this
    return 0 if response is not None else 1
