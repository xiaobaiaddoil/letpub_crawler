"""Incremental task creation behavior."""
import json

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.models.category import Category
from app.models.journal import Journal
from app.models.task import CrawlTask, TaskStatus, TaskType
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


def test_reset_detail_task_creates_missing_task_for_existing_journal(task_db):
    journal = Journal(journal_id=456, name="Journal", detail_crawled=True, comments_crawled=True)
    task_db.add(journal)
    task_db.commit()

    result = TaskManager(task_db).reset_detail_task(456)

    task = task_db.query(CrawlTask).filter_by(target_id="456").one()
    task_db.refresh(journal)
    assert result is True
    assert task.status == TaskStatus.PENDING.value
    assert journal.detail_crawled is False
    assert journal.comments_crawled is False


def test_reset_all_detail_tasks_creates_tasks_for_all_journals(task_db):
    task_db.add_all([
        Journal(journal_id=1, name="A", detail_crawled=True, comments_crawled=True),
        Journal(journal_id=2, name="B", detail_crawled=True, comments_crawled=True),
    ])
    task_db.commit()

    count = TaskManager(task_db).reset_all_detail_tasks()

    tasks = task_db.query(CrawlTask).order_by(CrawlTask.target_id).all()
    assert count == 2
    assert [task.target_id for task in tasks] == ["1", "2"]
    assert all(task.status == TaskStatus.PENDING.value for task in tasks)
