"""Paths, configuration (pairing) and runtime state (on/off mode)."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Team relay (own ntfy on Oracle Cloud, no 250 msg/day limit). The old address
# https://132-145-60-229.sslip.io keeps working for existing pairings. Public fallback: https://ntfy.sh
DEFAULT_SERVER = "https://relay.bitlionaire.me"


def home_dir() -> Path:
    """~/.horncall (HORNCALL_HOME overrides). Data from before the rename (~/.zug-zug) is moved over
    on first use; if that fails (files in use), the old folder keeps being used. Keep in sync with
    plugin/hooks/approver_launcher.py, which must stay stdlib-only."""
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
    """Where setup installs the program itself (the add-on code), per OS conventions — so nobody has to
    pick a folder. Data and pairing stay in home_dir(). Keep in sync with plugin/hooks/approver_launcher.py."""
    if os.environ.get("HORNCALL_APP_DIR"):
        return Path(os.environ["HORNCALL_APP_DIR"])
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Horncall" / "app"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Horncall" / "app"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "horncall" / "app"


def config_path() -> Path:
    return home_dir() / "config.json"


def state_path() -> Path:
    return home_dir() / "state.json"


def log_path() -> Path:
    return home_dir() / "hook.log"


def venv_dir() -> Path:
    return home_dir() / "venv"


@dataclass
class Config:
    key: str  # base64url, 32 bytes
    server: str = DEFAULT_SERVER
    device_name: str = field(default_factory=platform.node)
    # How long a question waits on the phone. It's open on the PC meanwhile (answering there drops the
    # phone card), so it can wait long: you may be far from the desk.
    timeout_s: int = 1800
    on_timeout: str = "dialog"  # "dialog" | "deny"
    max_preview_chars: int = 1500
    danger_patterns: list[str] | None = None  # None = built-in defaults
    labels: dict[str, str] = field(default_factory=dict)  # project folder (path_key) -> label
    # Phones whose signed answers are trusted: [{"pub": base64url DER, "added": unix time}].
    phones: list[dict] = field(default_factory=list)
    # Until this unix time a not-yet-known phone may join (set by `pair`).
    pairing_until: float | None = None
    # Created by a plugin that knows phone signatures: unsigned answers are never accepted (no
    # unsigned grace period as for pairings from before 0.5.5). New Config() objects get True;
    # configs saved by older plugins load without the key, so the default for loading is False.
    signed_only: bool = True

    @classmethod
    def load(cls) -> "Config | None":
        try:
            data = json.loads(config_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if "key" not in known:
            return None
        if known.get("timeout_s") == 120:
            known["timeout_s"] = 1800  # the default before 0.5.7, saved into every config
        known.setdefault("signed_only", False)  # saved by a plugin without signatures: keep its grace period
        return cls(**known)

    def save(self) -> None:
        _write_private(config_path(), json.dumps(asdict(self), indent=2))
        _restrict_acl(config_path())


@dataclass
class State:
    mode: str = "off"  # "on" = forward requests to the phone
    on_until: float | None = None  # unix time after which mode falls back to "off"
    on_since: float | None = None  # when the mode was switched on

    @classmethod
    def load(cls) -> "State":
        try:
            data = json.loads(state_path().read_text(encoding="utf-8"))
            return cls(mode=data.get("mode", "off"), on_until=data.get("on_until"), on_since=data.get("on_since"))
        except (OSError, ValueError):
            return cls()

    def save(self) -> None:
        _write_private(state_path(), json.dumps(asdict(self)))

    def is_on(self, now: float | None = None) -> bool:
        if self.mode != "on":
            return False
        return self.on_until is None or (now or time.time()) < self.on_until


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def _restrict_acl(path: Path) -> None:
    """Windows ignores chmod: give the file to the current user only (no inherited access).

    The account is named DOMAIN\\user — a bare user name can match the computer instead. If the file
    can't be read afterwards, the default (inherited) access is put back.
    """
    user, domain = os.environ.get("USERNAME"), os.environ.get("USERDOMAIN")
    if os.name != "nt" or not user or not domain:
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{domain}\\{user}:F"],
                       capture_output=True, timeout=10, creationflags=flags)
        try:
            path.read_bytes()
        except OSError:
            subprocess.run(["icacls", str(path), "/reset"], capture_output=True, timeout=10, creationflags=flags)
    except (OSError, subprocess.SubprocessError):
        pass
