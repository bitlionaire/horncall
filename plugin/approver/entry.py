"""The `horncall` command of a pip/pipx install (the plugin folder uses hooks/approver_launcher.py).

Hooks run on every permission question, prompt and (posttool) every tool call: while Horncall is
off they must cost nothing, so this checks the mode before importing the rest (cryptography etc.).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .config import home_dir


def _is_on() -> bool:
    try:
        state = json.loads((home_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    until = state.get("on_until")
    return state.get("mode") == "on" and (until is None or time.time() < until)


def _event_in_background(args: list[str]) -> None:
    """`event …` only tells the phone: imports and the network send run in a detached process and the hook
    returns at once (same as hooks/approver_launcher.py run_event_in_background, which explains why)."""
    import os
    import subprocess
    t = time.time()
    try:
        data = sys.stdin.buffer.read()
        inbox = home_dir() / "events"
        inbox.mkdir(parents=True, exist_ok=True)
        path = inbox / f"{t:.6f}-{os.getpid()}.json"
        path.write_bytes(data)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", HORNCALL_CWD=os.getcwd(),
                   HORNCALL_EVENT_T=repr(t), HORNCALL_EVENT_INPUT=str(path))
        # No console window: pythonw where it exists, CREATE_NO_WINDOW otherwise (see the launcher's DETACHED).
        detached = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        python = Path(sys.executable)
        if os.name == "nt" and python.with_name("pythonw.exe").exists():
            python = python.with_name("pythonw.exe")
        # -I: the project's folder never lands on sys.path (a planted json.py would run inside the hook).
        subprocess.Popen([str(python), "-I", "-m", "approver", *args], env=env, cwd=str(home_dir()),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **detached)
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["statusline"]:
        from .statusline import main as statusline
        return statusline()
    if args[:1] in (["hook"], ["event"], ["posttool"]):
        if not _is_on():
            return 0  # let the agent show its normal dialog / carry on
        if args[:1] == ["event"] and argv is None:
            _event_in_background(args)
            return 0
        if args[:1] == ["posttool"]:
            pending = home_dir() / "pending"
            if not pending.is_dir() or not any(pending.iterdir()):
                return 0
    from .cli import main as cli
    cli(args or ["--help"], prog="horncall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
