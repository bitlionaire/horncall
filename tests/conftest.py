import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugin"))


@pytest.fixture(autouse=True)
def approver_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HORNCALL_HOME", str(tmp_path / "home"))
    return tmp_path / "home"
