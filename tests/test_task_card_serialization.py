"""A UI click must not race scheduled intake or the legacy API alias."""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from dashboard import plugin_api as api


def test_task_and_bug_aliases_share_one_source_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "_project", lambda _: SimpleNamespace(root=tmp_path))
    entered = threading.Event()
    release = threading.Event()
    other_started = threading.Event()
    other_entered = threading.Event()

    @api._task_card_serialized
    def canonical(project_id, task_id, body):
        entered.set()
        assert release.wait(5)
        return "canonical"

    @api._task_card_serialized
    def legacy(project_id, bug_id, body):
        other_entered.set()
        return "legacy"

    def call_legacy():
        other_started.set()
        return legacy("p_test", "bug-20261023-example", None)

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(canonical, "p_test", "bug-20261023-example", None)
        assert entered.wait(5)
        second = pool.submit(call_legacy)
        assert other_started.wait(5)
        try:
            assert not other_entered.wait(0.1)
        finally:
            release.set()
        assert first.result(5) == "canonical"
        assert second.result(5) == "legacy"
