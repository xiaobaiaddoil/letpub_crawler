import json

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.models.journal import Journal
from app.models.problem_task import ProblemTask
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.services.detail_quality_service import DetailQualityService


@pytest.fixture
def quality_db():
    original_type = Journal.__table__.c.detail_data.type
    Journal.__table__.c.detail_data.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Journal.__table__.create(engine)
        CrawlTask.__table__.create(engine)
        ProblemTask.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_type


def _detail_data(field_count: int = 50, **overrides):
    data = {f"field_{index}": f"value_{index}" for index in range(field_count)}
    data.update({
        "issn": "1234-5678",
        "impact_factor": "3.2",
        "jcr_partition": "Q1",
        "cas_partition": "2区",
        "citescore": "4.5",
    })
    data.update(overrides)
    return data


def _add_journal(db, journal_id: int, detail_data, detail_crawled: bool = True):
    journal = Journal(
        journal_id=journal_id,
        name=f"Journal {journal_id}",
        detail_data=detail_data,
        detail_crawled=detail_crawled,
    )
    db.add(journal)
    db.commit()
    db.refresh(journal)
    return journal


def test_audit_data_flags_empty_detail(quality_db):
    result = DetailQualityService(quality_db).audit_data(100, {})

    assert result.status == "bad"
    assert "detail_empty" in result.hard_reasons


def test_audit_data_flags_login_placeholder(quality_db):
    data = _detail_data(50, hidden="注册 或 登录 后，查看")

    result = DetailQualityService(quality_db).audit_data(100, data)

    assert result.status == "bad"
    assert "detail_login_placeholder" in result.hard_reasons


def test_audit_data_warns_for_missing_optional_metrics(quality_db):
    data = _detail_data(50)
    data.pop("impact_factor")

    result = DetailQualityService(quality_db).audit_data(100, data)

    assert result.ok is True
    assert result.status == "warning"
    assert "detail_missing_important" in result.warnings


def test_audit_completed_records_problem_for_bad_journal(quality_db):
    _add_journal(quality_db, 100, {"issn": "1234-5678"})

    result = DetailQualityService(quality_db).audit_completed(record=True)

    assert result["stats"]["bad"] == 1
    problem = quality_db.query(ProblemTask).filter_by(journal_id=100).one()
    assert problem.problem_code == "detail_low_field_count"


def test_rebuild_by_journal_ids_resets_detail_task(quality_db):
    _add_journal(quality_db, 100, _detail_data(50))

    result = DetailQualityService(quality_db).rebuild_by_journal_ids([100], dry_run=False)

    assert result["reset_count"] == 1
    journal = quality_db.query(Journal).filter_by(journal_id=100).one()
    task = quality_db.query(CrawlTask).filter_by(
        task_type=TaskType.DETAIL.value,
        target_id="100",
    ).one()
    assert journal.detail_crawled is False
    assert task.status == TaskStatus.PENDING.value
    assert json.loads(task.extra_data)["rebuild_reason"] == "detail_quality"


def test_rebuild_failed_audit_returns_only_bad_audit_items(quality_db):
    _add_journal(quality_db, 100, {"issn": "1234-5678"})
    warning_data = _detail_data(50)
    warning_data.pop("impact_factor")
    _add_journal(quality_db, 200, warning_data)

    result = DetailQualityService(quality_db).rebuild_failed_audit(dry_run=True)

    assert result["audit"]["stats"]["bad"] == 1
    assert result["audit"]["stats"]["warning"] == 1
    assert [item["journal_id"] for item in result["audit"]["items"]] == [100]
    assert result["rebuild"]["requested"] == 1


def test_rebuild_failed_audit_records_only_bad_problems(quality_db):
    _add_journal(quality_db, 100, {"issn": "1234-5678"})
    warning_data = _detail_data(50)
    warning_data.pop("impact_factor")
    _add_journal(quality_db, 200, warning_data)

    DetailQualityService(quality_db).rebuild_failed_audit(dry_run=False)

    problems = quality_db.query(ProblemTask).all()
    assert len(problems) == 1
    assert problems[0].journal_id == 100
    assert problems[0].problem_code == "detail_low_field_count"
