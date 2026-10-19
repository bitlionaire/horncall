"""The add-on has one version number, written in two places: approver/__init__.py (pip/PyPI, the ZIP,
`approver update`) and plugin.json (Claude Code's marketplace). They must never drift apart."""

import json
from pathlib import Path

import approver

ROOT = Path(__file__).resolve().parents[1]


def test_plugin_manifest_has_the_package_version():
    manifest = json.loads((ROOT / "plugin" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == approver.__version__


def test_version_is_plain_major_minor_patch():
    assert [int(part) for part in approver.__version__.split(".")] and approver.__version__.count(".") == 2
