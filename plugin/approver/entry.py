"""The `horncall` command of a pip/pipx install (the plugin folder uses hooks/approver_launcher.py).

Hooks run on every permission question, prompt and (posttool) every tool call: while Horncall is
off they must cost nothing, so this checks the mode before importing the rest (cryptography etc.).
"""

from __future__ import annotations

import json
import sys
import time

from .config import home_dir


def _is_on() -> bool:
    try:
        state = json.loads((home_dir() / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    until = state.get("on_until")
    return state.get("mode") == "on" and (until is None or time.time() < until)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["statusline"]:
        from .statusline import main as statusline
        return statusline()
    if args[:1] in (["hook"], ["event"], ["posttool"]):
        if not _is_on():
            return 0  # let the agent show its normal dialog / carry on
        if args[:1] == ["posttool"]:
            pending = home_dir() / "pending"
            if not pending.is_dir() or not any(pending.iterdir()):
                return 0
    from .cli import main as cli
    cli(args or ["--help"], prog="horncall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
