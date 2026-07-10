import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.api.problems import retry_all_problems
from app.models.comment import Comment
from app.models.journal import Journal
from app.models.journal_metric import JournalMetricChange, JournalMetricSnapshot
from app.models.problem_task import ProblemTask, ProblemType
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.services.comment_refresh_service import CommentMismatchError, CommentRefreshService


@pytest.fixture
def comment_db():
    original_detail_type = Journal.__table__.c.detail_data.type
    original_metric_type = JournalMetricSnapshot.__table__.c.metrics.type
    Journal.__table__.c.detail_data.type = JSON()
    JournalMetricSnapshot.__table__.c.metrics.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Journal.__table__.create(engine)
        CrawlTask.__table__.create(engine)
        Comment.__table__.create(engine)
        ProblemTask.__table__.create(engine)
        JournalMetricSnapshot.__table__.create(engine)
        JournalMetricChange.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_detail_type
        JournalMetricSnapshot.__table__.c.metrics.type = original_metric_type


def _add_journal(db, journal_id=100):
    journal = Journal(
        journal_id=journal_id,
        name=f"Journal {journal_id}",
        comments_crawled=True,
        detail_data={"comment_count": 3, "crawled_comment_count": 1},
    )
    db.add(journal)
    db.add(Comment(journal_id=journal_id, comment_id=f"old-{journal_id}", content="old"))
    db.commit()
    db.refresh(journal)
    return journal


def test_replace_if_complete_deletes_old_comments_and_inserts_new_set(comment_db):
    journal = _add_journal(comment_db)
    comment_db.add(ProblemTask(
        journal_id=journal.journal_id,
        problem_type=ProblemType.WARNING.value,
        problem_code="comment_mismatch",
        resolved=0,
    ))
    comment_db.commit()

    result = CommentRefreshService(comment_db).replace_if_complete(
        journal,
        [
            {"comment_id": "new-1", "content": "one", "author": "a"},
            {"comment_id": "new-2", "content": "two", "author": "b"},
            {"comment_id": "new-2", "content": "duplicate", "author": "b"},
        ],
        {"total_count": 2, "total_pages": 1},
        task_id=None,
    )

    rows = comment_db.query(Comment).filter_by(journal_id=journal.journal_id).all()
    problem = comment_db.query(ProblemTask).filter_by(journal_id=journal.journal_id).one()
    comment_db.refresh(journal)

    assert result["inserted"] == 2
    assert sorted(row.comment_id for row in rows) == ["new-1", "new-2"]
    assert journal.comments_crawled is True
    assert journal.detail_data["comment_count"] == 2
    assert journal.detail_data["crawled_comment_count"] == 2
    assert problem.resolved == 1


def test_replace_if_incomplete_keeps_old_comments_and_records_problem(comment_db):
    journal = _add_journal(comment_db)

    with pytest.raises(CommentMismatchError):
        CommentRefreshService(comment_db).replace_if_complete(
            journal,
            [{"comment_id": "new-1", "content": "one", "author": "a"}],
            {"total_count": 3, "total_pages": 1},
            task_id=None,
        )

    rows = comment_db.query(Comment).filter_by(journal_id=journal.journal_id).all()
    problem = comment_db.query(ProblemTask).filter_by(journal_id=journal.journal_id).one()
    comment_db.refresh(journal)

    assert [row.comment_id for row in rows] == ["old-100"]
    assert journal.comments_crawled is False
    assert journal.detail_data["comment_count"] == 3
    assert journal.detail_data["crawled_comment_count"] == 1
    assert problem.problem_code == "comment_mismatch"
    assert problem.actual_value == 1


@pytest.mark.asyncio
async def test_retry_all_comment_mismatch_creates_comment_tasks_without_resolving_problem(comment_db):
    journal = _add_journal(comment_db, journal_id=200)
    comment_db.add(ProblemTask(
        journal_id=journal.journal_id,
        problem_type=ProblemType.WARNING.value,
        problem_code="comment_mismatch",
        resolved=0,
    ))
    comment_db.add(CrawlTask(
        task_type=TaskType.COMMENT.value,
        target_id=str(journal.journal_id),
        status=TaskStatus.COMPLETED.value,
        retry_count=2,
    ))
    comment_db.commit()

    result = await retry_all_problems(problem_code="comment_mismatch", db=comment_db)

    task = comment_db.query(CrawlTask).filter_by(
        task_type=TaskType.COMMENT.value,
        target_id=str(journal.journal_id),
    ).one()
    problem = comment_db.query(ProblemTask).filter_by(journal_id=journal.journal_id).one()
    comment_db.refresh(journal)

    assert result["action"] == "refresh_comments"
    assert task.status == TaskStatus.PENDING.value
    assert task.retry_count == 0
    assert journal.comments_crawled is False
    assert problem.resolved == 0
