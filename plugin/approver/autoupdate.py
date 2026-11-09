"""`approver autoupdate on|off`: new plugin versions install themselves.

- Installed from the Claude Code marketplace (GitHub): Claude Code's own background update does it. The
  marketplace can't switch that on itself (marketplace.json has no such field); the documented user switch is
  `extraKnownMarketplaces.horncall.autoUpdate` in ~/.claude/settings.json. Claude then says "Plugin updated:
  horncall · Run /reload-plugins to apply" (docs: plugins/loading, settings-reference).
- Unpacked ZIP: at most once a day, when a task ends, the launcher starts `approver update --auto` in the
  background — it installs only a release signed with the Horncall release key (update.py).
- git checkout / pip: not handled here (git pull, pipx upgrade).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import install, update
from .config import Config, home_dir

MARKETPLACE = "horncall"
CHECK_EVERY_S = 24 * 3600


def stamp_path() -> Path:
    """When the ZIP install last looked for an update (also read by the stdlib launcher)."""
    return home_dir() / "autoupdate-checked"


def _known_marketplace() -> dict:
    path = install.claude_settings().parent / "plugins" / "known_marketplaces.json"
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get(MARKETPLACE)
    except (OSError, ValueError, AttributeError):
        return {}
    return entry if isinstance(entry, dict) else {}


def github_source() -> dict | None:
    """The marketplace's GitHub source as Claude Code knows it, or None (local folder, not installed)."""
    source = _known_marketplace().get("source")
    if isinstance(source, dict) and source.get("source") == "github" and source.get("repo"):
        return {k: source[k] for k in ("source", "repo", "ref") if source.get(k)}
    return None


def kind() -> str:
    """'marketplace' (GitHub, Claude Code updates it), 'zip', or why neither applies: 'local', 'git', 'pip'."""
    how = update.how_installed()
    if how == "marketplace":
        return "marketplace" if github_source() else "local"
    return how


def is_on() -> bool:
    k = kind()
    if k == "marketplace":
        settings = install._load_json(install.claude_settings()) if install.claude_settings().exists() else {}
        entry = (settings.get("extraKnownMarketplaces") or {}).get(MARKETPLACE) or {}
        if "autoUpdate" in entry:  # the settings file comes first (docs: plugins/loading)
            return entry["autoUpdate"] is True
        return _known_marketplace().get("autoUpdate") is True
    if k == "zip":
        config = Config.load()
        return bool(config and config.auto_update)
    return False


def switch(on: bool) -> str:
    """Turns automatic updates on or off; returns what happened, for the user."""
    k = kind()
    if k == "marketplace":
        path = install.claude_settings()
        data = install._load_json(path)
        markets = data.setdefault("extraKnownMarketplaces", {})
        entry = markets.get(MARKETPLACE) if isinstance(markets.get(MARKETPLACE), dict) else {}
        entry.setdefault("source", github_source())
        entry["autoUpdate"] = on
        markets[MARKETPLACE] = entry
        install._save_json(path, data)
        return (f"Automatic updates ON — Claude Code installs new Horncall versions by itself ({path}). "
                "When it says \"Plugin updated\", type /reload-plugins or restart Claude Code."
                if on else f"Automatic updates OFF ({path}).")
    if k == "zip":
        config = Config.load()
        if config is None:
            return "Pair the phone first (setup or pair), then switch automatic updates on."
        config.auto_update = on
        config.save()
        stamp_path().unlink(missing_ok=True)  # look on the next task end, not in a day
        return ("Automatic updates ON — once a day, when a task ends, Horncall installs a new signed release "
                "in the background. Restart your AI tools afterwards." if on else "Automatic updates OFF.")
    return {
        "local": "Claude Code uses a local copy of the Horncall marketplace — it doesn't update by itself. "
                 "Update the folder (new ZIP / git pull), then: claude plugin marketplace update horncall",
        "git": "This is a git checkout (developer copy) — update it with git.",
        "pip": "Installed with pip/pipx — update with: pipx upgrade horncall  (or: uv tool upgrade horncall)",
    }[k]


def describe() -> str:
    """One line for `status`."""
    k = kind()
    if k not in ("marketplace", "zip"):
        return {"local": "manual (local marketplace copy)", "git": "manual (git checkout)",
                "pip": "manual (pipx upgrade horncall)"}[k]
    return "on" if is_on() else "off"
