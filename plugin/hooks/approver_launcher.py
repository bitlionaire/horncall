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


def run_in_venv(args: list) -> int:
    # Run from the plugin folder, never the project: `python -m` puts the working directory first on
    # sys.path, so a json.py / hmac.py planted in the project would otherwise run inside the hook.
    # The caller's folder is passed on for `approver name`.
    env = dict(os.environ, PYTHONPATH=str(PLUGIN_ROOT), PYTHONIOENCODING="utf-8", HORNCALL_CWD=os.getcwd())
    return subprocess.call([str(venv_python()), "-m", "approver", *args], env=env, cwd=str(PLUGIN_ROOT))


def statusline() -> int:
    """Claude Code status line: the stdlib-only code lives in the package (shared with pip installs)."""
    sys.path.insert(0, str(PLUGIN_ROOT))
    from approver.statusline import main as run
    return run()


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["statusline"]:
        return statusline()
    if args[:1] == ["posttool"]:
        # Runs after EVERY tool call: only start Python's venv when a phone question is still open.
        pending = HOME / "pending"
        if not is_on() or not venv_python().exists() or not pending.is_dir() or not any(pending.iterdir()):
            return 0
        run_in_venv(args)
        return 0
    if args[:1] in (["hook"], ["event"]):
        if not is_on() or not venv_python().exists():
            return 0  # fast path: let the agent show its normal dialog / carry on
        run_in_venv(args)
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
