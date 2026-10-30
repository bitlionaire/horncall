"""Which AI CLI versions Horncall supports (compat.json) and a warning when an installed one is older.

`support.floor` per agent is the oldest version we answer for; older ones are not worked around, the user
is only told to update. Never blocks anything: the hook itself stays fail-open (docs/AGENTS.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

COMPAT = Path(__file__).with_name("compat.json")


def load() -> dict:
    return json.loads(COMPAT.read_text(encoding="utf-8"))["agents"]


def version_key(version: str | None) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", version or ""))[:3]


def find_version(text: str) -> str | None:
    found = re.search(r"\d+\.\d+\.\d+", text)
    return found.group(0) if found else None


def installed_version(spec: dict) -> str | None:
    try:
        # 15 s: `gemini --version` alone takes ~6 s on Windows (Node start-up). Called by setup/status and by hooks
        # only in opt-in capture mode (HORNCALL_CAPTURE=1), never on a normal permission request.
        out = subprocess.run(spec["version_command"], capture_output=True, text=True, timeout=15,
                             shell=sys.platform == "win32")
    except (OSError, subprocess.TimeoutExpired, KeyError):
        return None
    return find_version(out.stdout) if out.returncode == 0 else None


def floor_warning(spec: dict, installed: str | None) -> str | None:
    floor = (spec.get("support") or {}).get("floor")
    if not floor or not installed or version_key(installed) >= version_key(floor):
        return None
    return f"{spec['title']} {installed} is older than {floor}, which Horncall needs — update it (older versions are not supported)"


def floor_warnings(names: list[str] | None = None) -> list[str]:
    """One line per listed agent (default: all) whose installed version is below its floor. Missing agents are skipped."""
    try:
        agents = load()
    except (OSError, ValueError):
        return []
    found = []
    for name in names if names is not None else agents:
        spec = agents.get(name)
        if spec and (line := floor_warning(spec, installed_version(spec))):
            found.append(line)
    return found
