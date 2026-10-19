"""`approver update`: replaces this plugin folder with the newest signed release from the relay.

The ZIP's Ed25519 signature (made by tools/make_dist.py with the release key kept off-repo) must match
the public key below — otherwise nothing is touched. ~/.horncall (pairing, labels, phones) stays as is.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import __version__, crypto
from .transport import USER_AGENT, Ntfy

RELEASE_PUBLIC_KEY = "zArkNlta1KuzmYQZQLXkhmGh22jJc6jnLM5Agyb1Njg"
INSTALL_ROOT = Path(__file__).resolve().parent.parent.parent  # unpacked ZIP: .claude-plugin/, plugin/, setup.cmd


class UpdateError(Exception):
    pass


def newer(candidate: str, current: str) -> bool:
    try:
        return tuple(map(int, candidate.split("."))) > tuple(map(int, current.split(".")))
    except ValueError:
        return False


def verify(data: bytes, signature: str, public_key: str | None = None) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(crypto.b64u_decode(public_key or RELEASE_PUBLIC_KEY))
        key.verify(crypto.b64u_decode(signature), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def install_zip(data: bytes, root: Path) -> None:
    """Unpacks a verified release over `root`; refuses odd paths and anything that isn't a plugin ZIP."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = zf.namelist()
        if "plugin/approver/__init__.py" not in names or ".claude-plugin/marketplace.json" not in names:
            raise UpdateError("not a Horncall plugin package")
        for name in names:
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or ":" in name:
                raise UpdateError(f"unsafe path in package: {name}")
        with tempfile.TemporaryDirectory() as tmp:
            zf.extractall(tmp)
            # The code folder is replaced whole (removed files must go): the slow copy goes next to
            # it first, then two renames swap them, so an interruption never leaves half a package.
            old_code = root / "plugin" / "approver"
            staged, backup = root / "plugin" / ".approver-new", root / "plugin" / ".approver-old"
            for leftover in (staged, backup):
                shutil.rmtree(leftover, ignore_errors=True)
            shutil.copytree(Path(tmp) / "plugin" / "approver", staged)
            if old_code.exists():
                old_code.rename(backup)
            try:
                staged.rename(old_code)
            except OSError:
                if backup.exists():
                    backup.rename(old_code)
                raise
            # The rest (hooks.json, skills, setup.cmd…) file by file; each copy is small.
            for src in Path(tmp).rglob("*"):
                rel = src.relative_to(tmp)
                if src.is_file() and rel.parts[:2] != ("plugin", "approver"):
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, root / rel)
            shutil.rmtree(backup, ignore_errors=True)


def how_installed(root: Path = INSTALL_ROOT) -> str:
    """'git', 'pip' (pip/pipx package), 'marketplace' (Claude Code's own copy from GitHub) or 'zip'."""
    if (root / ".git").exists():
        return "git"
    if not (root / "plugin" / "hooks" / "approver_launcher.py").exists():
        return "pip"
    plugins = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "plugins"
    try:
        root.resolve().relative_to(plugins.resolve())
        return "marketplace"
    except ValueError:
        return "zip"


def run(server: str, root: Path = INSTALL_ROOT, fetch=None, say=print) -> int:
    kind = how_installed(root)
    if kind == "git":
        say("This is a git checkout (developer copy) — update it with git, not with approver update.")
        return 1
    if kind == "pip":
        say("Installed with pip/pipx — update with: pipx upgrade horncall  (or: uv tool upgrade horncall)")
        return 1
    if kind == "marketplace":
        say("Installed from the Claude Code marketplace — in Claude type: /plugin marketplace update horncall")
        return 1
    latest = Ntfy(server).latest_versions() or {}
    plugin = latest.get("plugin") or {}
    version = plugin.get("version", "")
    if not newer(version, __version__):
        say(f"Horncall plugin {__version__} is up to date.")
        return 0
    if not plugin.get("sig"):
        say(f"Version {version} is available but not signed — download it from {server}/app/ by hand.")
        return 1
    fetch = fetch or _download
    data = fetch(f"{server}/app/{plugin.get('zip', 'horncall-plugin.zip')}")
    if not verify(data, plugin["sig"]):
        say("The downloaded package is NOT signed by the Horncall release key — nothing was changed.")
        return 1
    install_zip(data, root)
    say(f"Updated Horncall plugin {__version__} → {version}. Restart Claude Code (and other AI tools).")
    say("If the release notes mention new hooks, run setup.cmd once more.")
    return 0


def _download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()
