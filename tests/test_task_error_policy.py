import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.problems import retry_all_problems
from app.crawler.base import ProxyUnavailableError
from app.crawler.detail_crawler import DataValidationError
from app.models.problem_task import ProblemTask, ProblemType
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.services.journal_id_resolver_service import JournalIdResolverService
from app.services.task_error_policy import TaskErrorCode, TaskErrorPolicyService
from app.services.task_manager import TaskManager


@pytest.fixture
def task_policy_db():
    engine = create_engine("sqlite:///:memory:")
    CrawlTask.__table__.create(engine)
    ProblemTask.__table__.create(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _running_task(db, task_type=TaskType.DETAIL.value, target_id="3653", retry_count=0):
    task = CrawlTask(
        task_type=task_type,
        target_id=target_id,
        target_url="old",
        status=TaskStatus.RUNNING.value,
        retry_count=retry_count,
        max_retry=3,
        worker_id="worker-a",
        extra_data=json.dumps({"journal_id": int(target_id) if target_id.isdigit() else target_id}),
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


@pytest.mark.asyncio
async def test_detail_quality_error_freezes_task_and_calls_resolver(task_policy_db, monkeypatch):
    task = _running_task(task_policy_db)
    called = {}

    async def fake_resolve_one(self, old_journal_id, dry_run=True):
        called["journal_id"] = old_journal_id
        called["dry_run"] = dry_run
        return {"journal_id": old_journal_id, "status": "updated", "new_journal_id": 10279}

    monkeypatch.setattr(JournalIdResolverService, "resolve_one", fake_resolve_one)

    result = await TaskErrorPolicyService(task_policy_db).handle_exception(
        task=task,
        task_type=TaskType.DETAIL.value,
        exc=DataValidationError(
            "期刊 3653 详情质量检查未通过 (fetch_journal_id=3653): detail_missing_identity",
            missing_fields=["issn"],
            extracted_fields=32,
        ),
        task_manager=TaskManager(task_policy_db, worker_id="worker-a"),
        metadata={"fetch_journal_id": 3653},
    )

    task_policy_db.refresh(task)
    extra_data = json.loads(task.extra_data)

    assert result.code == TaskErrorCode.DETAIL_QUALITY_FAILED
    assert result.action.value == "resolve_detail_id"
    assert result.count_as_failed is False
    assert called == {"journal_id": 3653, "dry_run": False}
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == task.max_retry
    assert extra_data["last_error"]["code"] == "detail_quality_failed"
    assert extra_data["last_error"]["action"] == "resolve_detail_id"


@pytest.mark.asyncio
async def test_detail_quality_resolver_failure_does_not_escape(task_policy_db, monkeypatch):
    task = _running_task(task_policy_db)

    async def fail_resolve_one(self, old_journal_id, dry_run=True):
        raise RuntimeError("search unavailable")

    monkeypatch.setattr(JournalIdResolverService, "resolve_one", fail_resolve_one)

    result = await TaskErrorPolicyService(task_policy_db).handle_exception(
        task=task,
        task_type=TaskType.DETAIL.value,
        exc=DataValidationError(
            "期刊 3653 详情质量检查未通过: detail_missing_identity",
            missing_fields=["issn"],
        ),
        task_manager=TaskManager(task_policy_db, worker_id="worker-a"),
    )

    task_policy_db.refresh(task)
    assert result.count_as_failed is True
    assert result.resolution == {
        "journal_id": 3653,
        "status": "resolver_failed",
        "message": "search unavailable",
    }
    assert task.status == TaskStatus.FAILED.value
    assert task.retry_count == task.max_retry


@pytest.mark.asyncio
async def test_unresolved_detail_id_counts_as_failed(task_policy_db, monkeypatch):
    task = _running_task(task_policy_db)

    async def unresolved(self, old_journal_id, dry_run=True):
        return {"journal_id": old_journal_id, "status": "unresolved"}

    monkeypatch.setattr(JournalIdResolverService, "resolve_one", unresolved)

    result = await TaskErrorPolicyService(task_policy_db).handle_exception(
        task=task,
        task_type=TaskType.DETAIL.value,
        exc=DataValidationError(
            "期刊 3653 详情质量检查未通过: detail_missing_identity",
            missing_fields=["issn"],
        ),
        task_manager=TaskManager(task_policy_db, worker_id="worker-a"),
    )

    assert result.count_as_failed is True
    assert result.resolution == {"journal_id": 3653, "status": "unresolved"}


@pytest.mark.asyncio
async def test_proxy_unavailable_releases_without_retry_increment(task_policy_db):
    task = _running_task(task_policy_db, task_type=TaskType.LIST.value, target_id="4", retry_count=2)

    result = await TaskErrorPolicyService(task_policy_db).handle_exception(
        task=task,
        task_type=TaskType.LIST.value,
        exc=ProxyUnavailableError("no proxy"),
        task_manager=TaskManager(task_policy_db, worker_id="worker-a"),
    )

    task_policy_db.refresh(task)
    extra_data = json.loads(task.extra_data)

    assert result.code == TaskErrorCode.PROXY_UNAVAILABLE
    assert result.count_as_failed is False
    assert task.status == TaskStatus.PENDING.value
    assert task.retry_count == 2
    assert extra_data["last_error"]["code"] == "proxy_unavailable"


@pytest.mark.asyncio
async def test_problem_retry_all_uses_resolver_for_detail_quality_code(task_policy_db, monkeypatch):
    task_policy_db.add(
        ProblemTask(
            journal_id=3653,
            problem_type=ProblemType.ERROR.value,
            problem_code="detail_missing_identity",
            message="详情质量检查失败",
            resolved=0,
        )
    )
    task_policy_db.commit()

    async def fake_resolve_many(self, journal_ids, dry_run=True):
        return {
            "dry_run": dry_run,
            "requested": len(journal_ids),
            "updated": 1,
            "unresolved": 0,
            "items": [{"journal_id": journal_ids[0], "status": "updated", "new_journal_id": 10279}],
        }

    monkeypatch.setattr(JournalIdResolverService, "resolve_many", fake_resolve_many)

    result = await retry_all_problems(problem_code="detail_missing_identity", db=task_policy_db)

    problem = task_policy_db.query(ProblemTask).filter_by(journal_id=3653).one()
    assert result["action"] == "resolve_detail_id"
    assert result["resolution"]["updated"] == 1
    assert problem.resolved == 1
