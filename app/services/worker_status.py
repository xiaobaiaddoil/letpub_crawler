"""Worker status helpers for timestamp-without-time-zone columns."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import config
from app.models.worker import Worker


def _local_timezone():
    timezone_name = os.getenv("TZ") or os.getenv("LOG_TIMEZONE") or "Asia/Shanghai"
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def db_now() -> datetime:
    """Return naive local time comparable with PostgreSQL timestamp columns."""
    return datetime.now(_local_timezone()).replace(tzinfo=None)


def to_db_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(_local_timezone()).replace(tzinfo=None)


def worker_timeout_threshold() -> datetime:
    return db_now() - timedelta(seconds=config.WORKER_TIMEOUT)


def is_worker_online(worker: Worker) -> bool:
    last_heartbeat = to_db_naive(worker.last_heartbeat)
    return bool(last_heartbeat and last_heartbeat >= worker_timeout_threshold())
