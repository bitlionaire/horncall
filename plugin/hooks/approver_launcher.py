"""Stdlib-only entry point used by the hook and the /approver skill.

- `hook`: exits immediately (normal dialog) unless the approver is ON and set up;
  otherwise runs the real hook inside the private venv.
- `setup`: (re)creates ~/.horncall/venv, then runs the guided `approver setup` (setup.cmd calls this).
- anything else: forwarded to `python -m approver ...` inside the venv (set up on first use).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
def home_dir() -> Path:
    """Same as approver.config.home_dir (kept stdlib-only here): ~/.horncall, migrating ~/.zug-zug."""
    for var in ("HORNCALL_HOME", "ZUGZUG_HOME"):
        if os.environ.get(var):
            return Path(os.environ[var])
    new, old = Path.home() / ".horncall", Path.home() / ".zug-zug"
    if not new.exists() and old.exists():
        try:
            old.rename(new)
        except OSError:
            return old
    return new


def app_dir() -> Path:
    """Same as approver.config.app_dir: the standard per-user program folder (no folder to pick)."""
    if os.environ.get("HORNCALL_APP_DIR"):
        return Path(os.environ["HORNCALL_APP_DIR"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Horncall" / "app"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Horncall" / "app"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "horncall" / "app"


PACKAGE_ITEMS = (".claude-plugin", "plugin", "setup.cmd", "setup.sh", "LICENSE", "CZYTAJ.txt", "README.md")


def install_copy():
    """setup run from a downloaded/unpacked folder: copy the program to app_dir() and return it, so the ZIP
    folder can be deleted afterwards. A git checkout (developer copy), app_dir() itself and Claude Code's
    own copy (installed from the marketplace, updated by Claude) run in place."""
    root, target = PLUGIN_ROOT.parent, app_dir()
    claude_plugins = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins"
    if (root / ".git").exists() or root.resolve() == target.resolve() or claude_plugins.resolve() in root.resolve().parents:
        return None
    target.mkdir(parents=True, exist_ok=True)
    skip = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".hypothesis")
    for name in PACKAGE_ITEMS:
        src, dst = root / name, target / name
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)  # whole folder: files removed in a new version must go
            shutil.copytree(src, dst, ignore=skip)
        elif src.is_file():
            shutil.copy2(src, dst)
    return target


HOME = home_dir()
VENV = HOME / "venv"
REQUIREMENTS = PLUGIN_ROOT / "requirements.txt"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def is_on() -> bool:
    try:
        state = json.loads((HOME / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    until = state.get("on_until")
    return state.get("mode") == "on" and (until is None or time.time() < until)


def setup() -> int:
    """Creates the venv if missing (an existing one is kept: hooks may be using it) and installs dependencies."""
    HOME.mkdir(parents=True, exist_ok=True)
    if not venv_python().exists():
        print(f"Creating environment in {VENV} …", file=sys.stderr)
        venv.EnvBuilder(with_pip=True, clear=VENV.exists()).create(VENV)
    return subprocess.call([str(venv_python()), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "-r", str(REQUIREMENTS)], stdout=sys.stderr)


CREATE_NO_WINDOW = 0x08000000


def agent_call_options() -> dict:
    """For a hook the agent runs (it waits on us): no console window on Windows. Agents start our launcher
    without a console (Claude's exec-form hooks since 0.7.2), so the venv's python.exe got a new, visible, empty
    window for as long as a permission question waited (2026-11-12). CREATE_NO_WINDOW hides it; the hook's own
    stdin/stdout/stderr are passed explicitly, or the question and the answer would go to that hidden console."""
    if os.name != "nt":
        return {}
    streams = {}
    for name, stream in (("stdin", sys.stdin), ("stdout", sys.stdout), ("stderr", sys.stderr)):
        try:
            stream.fileno()
            streams[name] = stream
        except (AttributeError, OSError, ValueError):
            streams[name] = subprocess.DEVNULL  # no such handle: nothing to pass on
            if name != "stderr":  # the question can't come in or the answer can't go out: say so (review, 2026-11-12)
                log_called("agent-stream-missing", [name])
    return {"creationflags": CREATE_NO_WINDOW, **streams}


def run_in_venv(args: list, agent_call: bool = False) -> int:
    # Run from the plugin folder, never the project: `python -m` puts the working directory first on
    # sys.path, so a json.py / hmac.py planted in the project would otherwise run inside the hook.
    # The caller's folder is passed on for `approver name`.
    env = dict(os.environ, PYTHONPATH=str(PLUGIN_ROOT), PYTHONIOENCODING="utf-8", HORNCALL_CWD=os.getcwd())
    extra = agent_call_options() if agent_call else {}
    return subprocess.call([str(venv_python()), "-m", "approver", *args], env=env, cwd=str(PLUGIN_ROOT), **extra)


# No console for background work: DETACHED_PROCESS alone wasn't enough — the venv's python.exe only redirects to
# the real interpreter, which then got a new, visible console window (seen from opencode, 2026-11-11). So the
# GUI-subsystem pythonw.exe (never a console) where it exists, and CREATE_NO_WINDOW instead of a detached console.
DETACHED = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}


def background_python() -> Path:
    if os.name == "nt" and (VENV / "Scripts" / "pythonw.exe").exists():
        return VENV / "Scripts" / "pythonw.exe"
    return venv_python()


def run_event_in_background(args: list) -> None:
    """Task start/end (`event …`) only tell the phone: the venv's Python (~1 s of imports) and the network
    send run in a detached process, and the hook returns at once, so the agent's prompt never waits on
    them (a slow mobile network once held every prompt for 15 s, 2026-11-10). The hook's JSON goes to a
    file the background process reads and deletes; HORNCALL_EVENT_T keeps the moment the agent called,
    so a late start never overwrites a newer end on the phone (approver.hook.run_event)."""
    t = time.time()
    try:
        data = sys.stdin.buffer.read()
        inbox = HOME / "events"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{t:.6f}-{os.getpid()}.json"
        path.write_bytes(data)
        env = dict(os.environ, PYTHONPATH=str(PLUGIN_ROOT), PYTHONIOENCODING="utf-8", HORNCALL_CWD=os.getcwd(),
                   HORNCALL_EVENT_T=repr(t), HORNCALL_EVENT_INPUT=str(path))
        subprocess.Popen([str(background_python()), "-m", "approver", *args], env=env, cwd=str(PLUGIN_ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **DETACHED)
    except (OSError, ValueError):
        pass


def statusline() -> int:
    """Claude Code status line: the stdlib-only code lives in the package (shared with pip installs)."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from approver.statusline import main as run
    return run()


AUTO_UPDATE_EVERY_S = 24 * 3600  # approver.autoupdate.CHECK_EVERY_S


def maybe_auto_update() -> None:
    """At a task end, at most once a day: `approver update --auto` in the background, if the user switched
    automatic updates on (`approver autoupdate on`, ZIP installs). It checks the install kind and the release
    signature itself and logs to hook.log. Never delays or fails the hook."""
    try:
        if not json.loads((HOME / "config.json").read_text(encoding="utf-8")).get("auto_update"):
            return
        stamp = HOME / "autoupdate-checked"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < AUTO_UPDATE_EVERY_S:
            return
        stamp.touch()
        env = dict(os.environ, PYTHONPATH=str(PLUGIN_ROOT), PYTHONIOENCODING="utf-8")
        subprocess.Popen([str(background_python()), "-m", "approver", "update", "--auto"], env=env, cwd=str(PLUGIN_ROOT),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **DETACHED)
    except (OSError, ValueError, AttributeError):
        pass


def log_called(event: str, args: list) -> None:
    """One line before anything else runs: the agent did call us (agent name, and the state for events)."""
    fields = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event,
              "agent": args[1] if len(args) > 1 else "claude", "pid": os.getpid()}
    if args[:1] == ["event"] and len(args) > 2:
        fields["state"] = args[2]
    try:
        with open(HOME / "hook.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(fields) + "\n")
    except OSError:
        pass


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["statusline"]:
        return statusline()
    # Capture mode (docs/AGENTS.md) records inputs even with the phone off, so it skips the fast paths.
    capturing = os.environ.get("HORNCALL_CAPTURE") == "1"
    if args[:1] == ["posttool"]:
        # Runs after EVERY tool call: only start Python's venv when a phone question is still open.
        pending = HOME / "pending"
        if not venv_python().exists() or not capturing and (
                not is_on() or not pending.is_dir() or not any(pending.iterdir())):
            return 0
        run_in_venv(args, agent_call=True)
        return 0
    if args[:1] in (["hook"], ["event"]):
        if args[:1] == ["hook"]:
            # First thing, before any check: shows whether the agent called us at all (a question once never
            # reached the phone and left no trace, 2026-11-09). Only the agent name — the input stays unread here.
            log_called("hook-called", args)
        if args[:1] == ["event"] and args[-1:] == ["idle"] and venv_python().exists():
            maybe_auto_update()
        if not venv_python().exists() or not capturing and not is_on():
            return 0  # fast path: let the agent show its normal dialog / carry on
        if args[:1] == ["event"] and not capturing:
            log_called("event-called", args)
            run_event_in_background(args)
            return 0
        run_in_venv(args, agent_call=True)
        return 0  # never fail the hook
    if args[:1] == ["setup"]:
        target = install_copy()
        if target is not None:
            print(f"Installing Horncall to {target} (you can delete the downloaded folder afterwards)…", file=sys.stderr)
            return subprocess.call([sys.executable, str(target / "plugin" / "hooks" / "approver_launcher.py"), *args])
        # Also picks up new dependencies after an update.
        code = setup()
        return code or run_in_venv(args)
    if not venv_python().exists():
        code = setup()
        if code:
            return code
    return run_in_venv(args or ["--help"])


if __name__ == "__main__":
    sys.exit(main())
