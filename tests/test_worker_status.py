from datetime import datetime, timedelta

from app.models.worker import Worker
from app.services import worker_status


def test_worker_online_uses_naive_db_local_time(monkeypatch):
    now = datetime(2026, 7, 8, 5, 30, 0)
    monkeypatch.setattr(worker_status, "db_now", lambda: now)
    monkeypatch.setenv("WORKER_TIMEOUT", "120")

    worker = Worker(worker_id="worker-a", last_heartbeat=now - timedelta(seconds=60))

    assert worker_status.is_worker_online(worker) is True


def test_worker_offline_after_timeout(monkeypatch):
    now = datetime(2026, 7, 8, 5, 30, 0)
    monkeypatch.setattr(worker_status, "db_now", lambda: now)
    monkeypatch.setenv("WORKER_TIMEOUT", "120")

    worker = Worker(worker_id="worker-a", last_heartbeat=now - timedelta(seconds=121))

    assert worker_status.is_worker_online(worker) is False
