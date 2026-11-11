"""hook.log never grows without end (it reached 1.4 MB in a few weeks): over LOG_MAX_BYTES it becomes hook.log.1."""

import json

from approver import hook
from approver.config import log_path


def test_log_rotates_and_keeps_one_old_file(monkeypatch):
    monkeypatch.setattr(hook, "LOG_MAX_BYTES", 200)
    for n in range(30):
        hook.log("tick", n=n)
    current, old = log_path(), log_path().with_name(log_path().name + ".1")
    assert old.exists() and current.exists()
    assert current.stat().st_size <= 200 + 100 and old.stat().st_size <= 200 + 100
    last = json.loads(current.read_text(encoding="utf-8").splitlines()[-1])
    assert last["n"] == 29  # the newest line is never lost
    assert not old.with_name(old.name + ".1").exists()  # only one old file
