"""Incremental task creation behavior."""
import json

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.models.category import Category
from app.models.journal import Journal
from app.models.journal_index import CategoryIndexState
from app.models.journal_metric import JournalMetricChange, JournalMetricSnapshot
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.services.metric_service import MetricService
from app.services.task_manager import TaskManager


@pytest.fixture
def task_db():
    """SQLite DB with JSONB patched to generic JSON for task-manager tests."""
    original_type = Journal.__table__.c.detail_data.type
    Journal.__table__.c.detail_data.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Category.__table__.create(engine)
        Journal.__table__.create(engine)
        CrawlTask.__table__.create(engine)
        CategoryIndexState.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_type


def test_refresh_completed_list_tasks_for_incremental_scan(task_db):
    task = CrawlTask(
        task_type=TaskType.LIST.value,
        target_id="10:1",
        target_url="old",
        status=TaskStatus.COMPLETED.value,
        extra_data=json.dumps({"field_tag": "10", "page": 1}),
    )
    task_db.add(task)
    task_db.commit()

    tasks = TaskManager(task_db).create_list_tasks("10", 2, refresh_completed=True)

    assert [task.target_id for task in tasks] == ["10:1", "10:2"]
    refreshed = task_db.query(CrawlTask).filter_by(target_id="10:1").one()
    created = task_db.query(CrawlTask).filter_by(target_id="10:2").one()
    assert refreshed.status == TaskStatus.PENDING.value
    assert refreshed.retry_count == 0
    assert created.status == TaskStatus.PENDING.value


def test_create_list_tasks_does_not_refresh_completed_by_default(task_db):
    task_db.add(CrawlTask(
        task_type=TaskType.LIST.value,
        target_id="10:1",
        status=TaskStatus.COMPLETED.value,
    ))
    task_db.commit()

    tasks = TaskManager(task_db).create_list_tasks("10", 1)

    assert tasks == []
    existing = task_db.query(CrawlTask).filter_by(target_id="10:1").one()
    assert existing.status == TaskStatus.COMPLETED.value


def test_acquire_tasks_schedules_retryable_failed_when_no_pending(task_db):
    task_db.add(CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="1",
        status=TaskStatus.FAILED.value,
        retry_count=1,
        max_retry=3,
        error_message="old error",
    ))
    task_db.commit()

    tasks = TaskManager(task_db, worker_id="worker-a").acquire_tasks(TaskType.DETAIL.value, limit=1)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == TaskStatus.RUNNING.value
    assert task.worker_id == "worker-a"
    assert task.retry_count == 1
    assert task.error_message is None
    assert task.locked_at is not None
    assert task.started_at is not None


def test_acquire_tasks_prefers_pending_over_retryable_failed(task_db):
    failed = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="failed",
        status=TaskStatus.FAILED.value,
        retry_count=1,
        max_retry=3,
    )
    pending = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="pending",
        status=TaskStatus.PENDING.value,
    )
    task_db.add_all([failed, pending])
    task_db.commit()

    tasks = TaskManager(task_db, worker_id="worker-a").acquire_tasks(TaskType.DETAIL.value, limit=1)

    assert len(tasks) == 1
    assert tasks[0].target_id == "pending"

    task_db.refresh(failed)
    assert failed.status == TaskStatus.FAILED.value


def test_acquire_tasks_fills_limit_with_retryable_failed(task_db):
    task_db.add_all([
        CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id="pending",
            status=TaskStatus.PENDING.value,
        ),
        CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id="failed",
            status=TaskStatus.FAILED.value,
            retry_count=1,
            max_retry=3,
        ),
        CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id="exhausted",
            status=TaskStatus.FAILED.value,
            retry_count=3,
            max_retry=3,
        ),
    ])
    task_db.commit()

    tasks = TaskManager(task_db, worker_id="worker-a").acquire_tasks(TaskType.DETAIL.value, limit=2)

    assert [task.target_id for task in tasks] == ["pending", "failed"]
    assert all(task.status == TaskStatus.RUNNING.value for task in tasks)

    exhausted = task_db.query(CrawlTask).filter_by(target_id="exhausted").one()
    assert exhausted.status == TaskStatus.FAILED.value


def test_get_failed_tasks_can_filter_by_task_type(task_db):
    task_db.add_all([
        CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id="1",
            status=TaskStatus.FAILED.value,
            retry_count=1,
            max_retry=3,
        ),
        CrawlTask(
            task_type=TaskType.COMMENT.value,
            target_id="1",
            status=TaskStatus.FAILED.value,
            retry_count=1,
            max_retry=3,
        ),
    ])
    task_db.commit()

    tasks = TaskManager(task_db).get_failed_tasks(task_type=TaskType.COMMENT.value)

    assert len(tasks) == 1
    assert tasks[0].task_type == TaskType.COMMENT.value


def test_reset_or_create_detail_task_ignores_detail_crawled_skip(task_db):
    category = Category(field_tag="10", name="A", total_count=1)
    journal = Journal(
        journal_id=123,
        name="Journal",
        category=category,
        detail_crawled=True,
        comments_crawled=True,
    )
    task_db.add_all([category, journal])
    task_db.commit()

    task = TaskManager(task_db).reset_or_create_detail_task(123, category.id)

    assert task.task_type == TaskType.DETAIL.value
    assert task.target_id == "123"
    assert task.status == TaskStatus.PENDING.value


def test_create_detail_task_keeps_incremental_skip_for_already_crawled_journal(task_db):
    task_db.add(Journal(journal_id=123, name="Journal", detail_crawled=True))
    task_db.commit()

    task = TaskManager(task_db).create_detail_task(123)

    assert task is None
    assert task_db.query(CrawlTask).count() == 0


def test_create_comment_task_skips_already_crawled_journal(task_db):
    task_db.add(Journal(journal_id=123, name="Journal", comments_crawled=True))
    task_db.commit()

    task = TaskManager(task_db).create_comment_task(123)

    assert task is None
    assert task_db.query(CrawlTask).count() == 0


def test_create_comment_task_creates_pending_comment_task(task_db):
    task_db.add(Journal(journal_id=123, name="Journal", comments_crawled=False))
    task_db.commit()

    task = TaskManager(task_db).create_comment_task(123, category_id=7)

    assert task.task_type == TaskType.COMMENT.value
    assert task.target_id == "123"
    assert task.status == TaskStatus.PENDING.value
    assert json.loads(task.extra_data) == {"journal_id": 123, "category_id": 7}


def test_create_comment_task_refreshes_completed_when_requested(task_db):
    task_db.add_all([
        Journal(journal_id=123, name="Journal", comments_crawled=True),
        CrawlTask(
            task_type=TaskType.COMMENT.value,
            target_id="123",
            status=TaskStatus.COMPLETED.value,
            retry_count=2,
            error_message="old",
        ),
    ])
    task_db.commit()

    task = TaskManager(task_db).create_comment_task(123, refresh_completed=True)

    assert task.status == TaskStatus.PENDING.value
    assert task.retry_count == 0
    assert task.error_message is None


def test_create_comment_task_refreshes_completed_when_journal_not_crawled(task_db):
    task_db.add_all([
        Journal(journal_id=123, name="Journal", comments_crawled=False),
        CrawlTask(
            task_type=TaskType.COMMENT.value,
            target_id="123",
            status=TaskStatus.COMPLETED.value,
            retry_count=2,
            error_message="old",
        ),
    ])
    task_db.commit()

    task = TaskManager(task_db).create_comment_task(123)

    assert task.status == TaskStatus.PENDING.value
    assert task.retry_count == 0
    assert task.error_message is None


def test_reset_detail_task_creates_missing_task_for_existing_journal(task_db):
    journal = Journal(journal_id=456, name="Journal", detail_crawled=True, comments_crawled=True)
    task_db.add(journal)
    task_db.commit()

    result = TaskManager(task_db).reset_detail_task(456)

    tasks = task_db.query(CrawlTask).filter_by(target_id="456").all()
    task_by_type = {task.task_type: task for task in tasks}
    task_db.refresh(journal)
    assert result is True
    assert task_by_type[TaskType.DETAIL.value].status == TaskStatus.PENDING.value
    assert task_by_type[TaskType.COMMENT.value].status == TaskStatus.PENDING.value
    assert journal.detail_crawled is False
    assert journal.comments_crawled is False


def test_reset_all_detail_tasks_creates_tasks_for_all_journals(task_db):
    task_db.add_all([
        Journal(journal_id=1, name="A", detail_crawled=True, comments_crawled=True),
        Journal(journal_id=2, name="B", detail_crawled=True, comments_crawled=True),
    ])
    task_db.commit()

    count = TaskManager(task_db).reset_all_detail_tasks()

    tasks = task_db.query(CrawlTask).order_by(CrawlTask.target_id, CrawlTask.task_type).all()
    assert count == 2
    assert [(task.task_type, task.target_id) for task in tasks] == [
        (TaskType.COMMENT.value, "1"),
        (TaskType.DETAIL.value, "1"),
        (TaskType.COMMENT.value, "2"),
        (TaskType.DETAIL.value, "2"),
    ]
    assert all(task.status == TaskStatus.PENDING.value for task in tasks)


def test_create_index_scan_tasks_refreshes_changed_category_pages(task_db):
    category = Category(field_tag="10", name="A", total_count=20)
    task_db.add(category)
    task_db.commit()

    task_db.add_all([
        CategoryIndexState(
            category_id=category.id,
            field_tag=category.field_tag,
            remote_total_count=20,
            local_index_count=10,
            total_pages=2,
            status="changed",
        ),
        CrawlTask(
            task_type=TaskType.LIST.value,
            target_id="10:1",
            status=TaskStatus.COMPLETED.value,
        ),
    ])
    task_db.commit()

    count = TaskManager(task_db).create_index_scan_tasks()

    tasks = task_db.query(CrawlTask).filter(
        CrawlTask.task_type == TaskType.LIST.value
    ).order_by(CrawlTask.target_id).all()
    assert count == 2
    assert [task.target_id for task in tasks] == ["10:1", "10:2"]
    assert all(task.status == TaskStatus.PENDING.value for task in tasks)


def test_create_full_detail_refresh_tasks_refreshes_crawled_journals(task_db):
    task_db.add_all([
        Journal(journal_id=1, name="A", detail_crawled=True),
        Journal(journal_id=2, name="B", detail_crawled=True),
    ])
    task_db.commit()

    count = TaskManager(task_db).create_full_detail_refresh_tasks()

    tasks = task_db.query(CrawlTask).filter(
        CrawlTask.task_type == TaskType.DETAIL.value
    ).order_by(CrawlTask.target_id).all()
    assert count == 2
    assert [task.target_id for task in tasks] == ["1", "2"]
    assert all(task.status == TaskStatus.PENDING.value for task in tasks)


def test_running_task_mutations_require_current_worker(task_db):
    task = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="123",
        status=TaskStatus.RUNNING.value,
        worker_id="worker-a",
    )
    task_db.add(task)
    task_db.commit()
    task_db.refresh(task)

    other_manager = TaskManager(task_db, worker_id="worker-b")

    assert other_manager.renew_task_lock(task) is False
    assert other_manager.complete_task(task) is False
    assert other_manager.fail_task(task, "late failure") is False
    assert other_manager.release_task(task, "late release") is False

    task_db.refresh(task)
    assert task.status == TaskStatus.RUNNING.value
    assert task.worker_id == "worker-a"
    assert task.retry_count == 0
    assert task.error_message is None

    owner_manager = TaskManager(task_db, worker_id="worker-a")

    assert owner_manager.renew_task_lock(task) is True
    assert owner_manager.complete_task(task) is True

    task_db.refresh(task)
    assert task.status == TaskStatus.COMPLETED.value
    assert task.worker_id == "worker-a"
    assert task.locked_at is None


@pytest.fixture
def metric_db():
    """SQLite DB with JSONB fields patched to generic JSON for metric tests."""
    original_journal_detail_type = Journal.__table__.c.detail_data.type
    original_snapshot_metrics_type = JournalMetricSnapshot.__table__.c.metrics.type
    Journal.__table__.c.detail_data.type = JSON()
    JournalMetricSnapshot.__table__.c.metrics.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Category.__table__.create(engine)
        Journal.__table__.create(engine)
        CrawlTask.__table__.create(engine)
        JournalMetricSnapshot.__table__.create(engine)
        JournalMetricChange.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_journal_detail_type
        JournalMetricSnapshot.__table__.c.metrics.type = original_snapshot_metrics_type


def test_metric_snapshot_records_only_changed_fields(metric_db):
    journal = Journal(
        journal_id=123,
        name="Journal",
        issn="1111-1111",
        impact_factor=1.23,
    )
    metric_db.add(journal)
    metric_db.commit()

    service = MetricService(metric_db)
    first_change_count = service.record_snapshot(
        journal,
        {"comment_count": 10, "crawled_comment_count": 10},
    )
    metric_db.commit()

    assert first_change_count == 0
    assert metric_db.query(JournalMetricSnapshot).count() == 1
    assert metric_db.query(JournalMetricChange).count() == 0

    journal.impact_factor = 2.34
    second_change_count = service.record_snapshot(
        journal,
        {"comment_count": 12, "crawled_comment_count": 12},
    )
    metric_db.commit()

    changed_fields = {
        change.field_name
        for change in metric_db.query(JournalMetricChange).all()
    }
    assert second_change_count == 3
    assert metric_db.query(JournalMetricSnapshot).count() == 2
    assert changed_fields == {"impact_factor", "comment_count", "crawled_comment_count"}
