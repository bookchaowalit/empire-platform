"""empire-recurring: HTTP behaviour against a fake Tasks API (no database)."""

from __future__ import annotations

import json
from datetime import date

import pytest

from empire_platform import recurring
from empire_platform.recurring import ApiError, preview_due_dates, recurrence_step, recurring_parents, run

TODAY = date(2026, 10, 11)


def task(number, **kw):
    base = {"id": f"id-{number}", "number": number, "title": f"t{number}", "status": "todo", "due_date": None,
            "completed_at": None, "recurrence_rule": None, "archived_at": None, "is_recurring_instance": False,
            "recurrence_occurrence": None, "parent_recurring_id": None}
    return {**base, **kw}


class FakeHttp:
    def __init__(self, tasks, fail=()):
        self.tasks, self.fail, self.calls = tasks, set(fail), []

    def call(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if method == "GET":
            after = (params or {}).get("after") or 0
            return {"items": [t for t in self.tasks if t["number"] > after][: (params or {}).get("limit", 200)]}
        task_id = path.split("/")[3]
        if task_id in self.fail:
            raise ApiError(500, "POST x")
        return {"created": [task(900, due_date="2026-10-11")], "existing": 0}


def posts(http):
    return [c for c in http.calls if c[0] == "POST"]


def test_zero_tasks_is_a_clean_noop():
    http = FakeHttp([])
    assert run(http, TODAY, dry_run=False) == {"parents": 0, "generated": 0, "unknown_rule": 0, "errors": 0}
    assert posts(http) == []


def test_parents_exclude_instances_archived_cancelled():
    tasks = [task(1, recurrence_rule="daily"), task(2, recurrence_rule="daily", is_recurring_instance=True),
             task(3, recurrence_rule="daily", archived_at="2026-01-01T00:00:00Z"),
             task(4, recurrence_rule="daily", status="cancelled"), task(5)]
    assert [t["number"] for t in recurring_parents(tasks)] == [1]


def test_apply_posts_generate_per_parent_with_until():
    http = FakeHttp([task(1, recurrence_rule="daily"), task(2, recurrence_rule="weekly")])
    counts = run(http, TODAY, dry_run=False)
    assert counts["parents"] == 2 and counts["generated"] == 2 and counts["errors"] == 0
    assert [(c[1], c[2]) for c in posts(http)] == [
        ("/v1/tasks/id-1/occurrences/generate", {"until": "2026-10-11"}),
        ("/v1/tasks/id-2/occurrences/generate", {"until": "2026-10-11"})]


def test_dry_run_writes_nothing_and_previews():
    http = FakeHttp([task(1, recurrence_rule="daily", due_date="2026-10-09")])
    counts = run(http, TODAY, dry_run=True)
    assert posts(http) == [] and counts["generated"] == 2  # 10-10 and 10-11


def test_preview_continues_after_existing_instances():
    parent = task(1, recurrence_rule="daily", due_date="2026-10-01")
    assert preview_due_dates(parent, [(1, "2026-10-09", None), (2, "2026-10-10", None)], TODAY) == [(3, TODAY)]


def test_unknown_rule_is_skipped_not_fatal():
    http = FakeHttp([task(1, recurrence_rule="whenever")])
    counts = run(http, TODAY, dry_run=False)
    assert counts["unknown_rule"] == 1 and counts["errors"] == 0 and posts(http) == []
    assert recurrence_step("every 3d").days == 3 and recurrence_step("every 2 weeks").days == 14


def test_one_failing_parent_does_not_block_the_rest():
    http = FakeHttp([task(1, recurrence_rule="daily"), task(2, recurrence_rule="daily")], fail={"id-1"})
    counts = run(http, TODAY, dry_run=False)
    assert counts["errors"] == 1 and counts["generated"] == 1 and len(posts(http)) == 2


def test_pagination_follows_cursor():
    many = [task(n) for n in range(1, 251)]
    assert len(recurring.list_tasks(FakeHttp(many))) == 250


def test_main_missing_config_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("EMPIRE_TASKS_API_URL", raising=False)
    monkeypatch.delenv("RECURRING_GEN_TOKEN", raising=False)
    assert recurring.main([]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["missing"] == ["EMPIRE_TASKS_API_URL", "RECURRING_GEN_TOKEN"]


def test_main_exit_codes_and_no_token_in_output(monkeypatch, capsys):
    monkeypatch.setenv("EMPIRE_TASKS_API_URL", "https://tasks.example")
    monkeypatch.setenv("RECURRING_GEN_TOKEN", "sekrit-token")
    monkeypatch.setattr(recurring, "Http", lambda base, token: FakeHttp([task(1, recurrence_rule="daily")]))
    assert recurring.main(["--until", "2026-10-11"]) == 0
    monkeypatch.setattr(recurring, "Http", lambda base, token: FakeHttp([task(1, recurrence_rule="daily")], fail={"id-1"}))
    assert recurring.main([]) == 1

    def down(base, token):
        raise_http = FakeHttp([])
        raise_http.call = lambda *a, **k: (_ for _ in ()).throw(ApiError(503, "GET /v1/tasks"))
        return raise_http
    monkeypatch.setattr(recurring, "Http", down)
    assert recurring.main([]) == 1
    captured = capsys.readouterr()
    assert "sekrit-token" not in captured.out + captured.err


def test_http_requires_https():
    with pytest.raises(SystemExit):
        recurring.Http("http://tasks.example", "t")
    recurring.Http("http://127.0.0.1:3601", "t")
