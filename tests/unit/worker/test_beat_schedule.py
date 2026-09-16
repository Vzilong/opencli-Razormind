"""Unit tests for backend/worker/beat_schedule.py — the surviving
parse_cron_expression helper (its former build_beat_schedule() was dead code,
removed in runtime-hardening phase PR-C, superseded by worker/redbeat_sync.py)."""

import pytest
from celery.schedules import crontab

from backend.worker import tasks as worker_tasks
from backend.worker.beat_schedule import parse_cron_expression
from backend.worker.celery_app import celery_app


def test_parse_cron_expression_valid():
    result = parse_cron_expression("*/5 9 * * 1-5")
    assert isinstance(result, crontab)


def test_parse_cron_expression_wrong_field_count():
    with pytest.raises(ValueError, match="need 5 fields"):
        parse_cron_expression("* * * *")


def test_automation_scheduler_tick_is_wired_into_celery_beat():
    entry = celery_app.conf.beat_schedule["scheduled-automation-tick"]
    assert entry["task"] == "run_automation_scheduler_tick"
    assert entry["schedule"] == 60.0


def test_automation_tick_enqueues_every_queued_scheduled_run(monkeypatch):
    monkeypatch.setattr(
        worker_tasks,
        "post_control_plane",
        lambda _path, _payload: {
            "success": True,
            "data": {"queued_run_ids": ["run-1", "run-2"]},
        },
    )
    enqueued: list[str] = []
    monkeypatch.setattr(
        worker_tasks.dispatch_scheduled_operations_agent_run,
        "delay",
        enqueued.append,
    )

    result = worker_tasks.run_automation_scheduler_tick.run()

    assert enqueued == ["run-1", "run-2"]
    assert result["dispatch_enqueued_run_ids"] == ["run-1", "run-2"]
