from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models.comment import Comment
from app.models.journal import Journal
from app.services.metric_service import MetricService
from app.services.problem_service import ProblemService


class CommentMismatchError(RuntimeError):
    """Raised when a comment refresh result is incomplete and must not replace data."""

    def __init__(self, journal_id: int, expected: int, actual: int):
        self.journal_id = journal_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"期刊 {journal_id} 评论数量不匹配: expected={expected}, actual={actual}"
        )


class CommentRefreshService:
    """Replace a journal's comment set only after a complete comment crawl."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        try:
            if value is None:
                return default
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _comment_rows(journal_id: int, comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_comment_ids: set[str] = set()
        for c_data in comments:
            comment_id = c_data.get("comment_id")
            if not comment_id or comment_id in seen_comment_ids:
                continue
            seen_comment_ids.add(comment_id)
            rows.append({
                "journal_id": journal_id,
                "comment_id": comment_id,
                "content": c_data.get("content"),
                "author": c_data.get("author"),
                "rating": c_data.get("rating"),
                "submit_experience": c_data.get("submit_experience"),
                "comment_time": c_data.get("comment_time"),
            })
        return rows

    @staticmethod
    def _update_comment_metrics(
        journal: Journal,
        total_count: int,
        total_pages: int,
        crawled_count: int,
        complete: bool,
    ) -> dict[str, Any]:
        basic_info = dict(journal.detail_data or {})
        basic_info["comment_count"] = total_count
        basic_info["comment_pages"] = total_pages
        basic_info["crawled_comment_count"] = crawled_count
        journal.detail_data = basic_info
        journal.comments_crawled = complete
        return basic_info

    def replace_if_complete(
        self,
        journal: Journal,
        comments: list[dict[str, Any]],
        comment_info: dict[str, Any],
        task_id: int | None = None,
    ) -> dict[str, Any]:
        total_count = self._to_int(comment_info.get("total_count"))
        total_pages = self._to_int(comment_info.get("total_pages"))
        comment_rows = self._comment_rows(journal.journal_id, comments)
        crawled_count = len(comment_rows)
        complete = total_count == 0 or crawled_count >= total_count

        basic_info = self._update_comment_metrics(
            journal,
            total_count=total_count,
            total_pages=total_pages,
            crawled_count=crawled_count,
            complete=complete,
        )

        if not complete:
            ProblemService(self.db).record_problem(
                journal_id=journal.journal_id,
                problem_type="warning",
                problem_code=ProblemService.CODE_COMMENT_MISMATCH,
                message=(
                    f"评论集合不完整，保留旧评论数据: expected={total_count}, "
                    f"actual={crawled_count}, pages={total_pages}"
                ),
                expected_value=total_count,
                actual_value=crawled_count,
            )
            raise CommentMismatchError(journal.journal_id, total_count, crawled_count)

        self.db.query(Comment).filter(
            Comment.journal_id == journal.journal_id
        ).delete(synchronize_session=False)

        if comment_rows:
            self.db.add_all([Comment(**row) for row in comment_rows])

        MetricService(self.db).record_snapshot(journal, basic_info, task_id=task_id)
        self.db.commit()
        ProblemService(self.db).mark_resolved_by_journal(
            journal.journal_id,
            ProblemService.CODE_COMMENT_MISMATCH,
        )

        return {
            "journal_id": journal.journal_id,
            "expected": total_count,
            "actual": crawled_count,
            "replaced": True,
            "deleted_old": True,
            "inserted": len(comment_rows),
        }
