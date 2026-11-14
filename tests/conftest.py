import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))


@pytest.fixture(autouse=True)
def approver_home(tmp_path, monkeypatch):
    from approver import compat
    monkeypatch.setenv("HORNCALL_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(compat, "installed_version", lambda spec: None)  # no real CLIs in tests
    monkeypatch.setenv("HORNCALL_NO_PC_DIALOG", "1")  # no real windows; test_pcdialog.py fakes one
    monkeypatch.delenv("HORNCALL_CAPTURE", raising=False)  # a developer's capture mode must not change the tests
    monkeypatch.delenv("HORNCALL_EVENT_T", raising=False)
    monkeypatch.delenv("HORNCALL_EVENT_INPUT", raising=False)
    return tmp_path / "home"
