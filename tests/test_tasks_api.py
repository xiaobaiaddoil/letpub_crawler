from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.tasks import list_tasks
from app.models.task import CrawlTask


def _session():
    engine = create_engine("sqlite:///:memory:")
    CrawlTask.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def _add_task(db, **kwargs):
    defaults = {
        "task_type": "detail",
        "target_id": "100",
        "target_url": "https://example.test/detail/100",
        "status": "pending",
        "retry_count": 0,
        "max_retry": 3,
        "created_at": datetime(2026, 1, 1, 0, 0, 0),
    }
    defaults.update(kwargs)
    task = CrawlTask(**defaults)
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def test_list_tasks_filters_and_searches_across_all_rows():
    db = _session()
    try:
        _add_task(db, target_id="100", status="completed", error_message=None)
        _add_task(db, target_id="200", status="failed", error_message="proxy connection failed")
        _add_task(db, target_id="300", status="failed", worker_id="worker-b")

        result = list_tasks(status="failed", search="proxy", page=1, size=20, db=db)

        assert result["total"] == 1
        assert result["items"][0].target_id == "200"
        assert result["items"][0].max_retry == 3
    finally:
        db.close()


def test_list_tasks_returns_total_and_page_items():
    db = _session()
    try:
        for index in range(3):
            _add_task(db, target_id=str(index), created_at=datetime(2026, 1, 1, 0, index, 0))

        result = list_tasks(search=None, page=2, size=2, db=db)

        assert result["total"] == 3
        assert result["page"] == 2
        assert result["size"] == 2
        assert len(result["items"]) == 1
    finally:
        db.close()
