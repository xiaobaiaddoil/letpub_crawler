from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.models.journal import Journal
from app.models.problem_task import ProblemType
from app.models.task import TaskStatus
from app.services.problem_service import ProblemService
from app.services.task_manager import TaskManager


LOGIN_PLACEHOLDER_MARKERS = (
    "注册 或 登录 后",
    "注册或登录后",
    "登录 后，查看",
)

DEFAULT_MIN_FIELD_COUNT = 30
DEFAULT_WARN_FIELD_COUNT = 45

IDENTITY_FIELD_GROUPS = {
    "issn": ("issn", "期刊ISSN"),
}

IMPORTANT_FIELD_GROUPS = {
    "impact_factor": ("impact_factor", "最新影响因子"),
    "jcr_partition": ("jcr_partition", "JCR分区"),
    "cas_partition": (
        "cas_partition",
        "中国科学院期刊分区",
        "期刊分区表_（_2025年3月升级版_）",
        "《新锐期刊分区表》_（_2026年3月发布_）",
    ),
    "citescore": ("citescore", "CiteScore"),
}


@dataclass
class DetailQualityResult:
    journal_id: int
    status: str
    field_count: int = 0
    hard_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    missing_important: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard_reasons

    @property
    def reason_codes(self) -> list[str]:
        return [*self.hard_reasons, *self.warnings]

    def to_dict(self) -> dict[str, Any]:
        return {
            "journal_id": self.journal_id,
            "status": self.status,
            "field_count": self.field_count,
            "hard_reasons": self.hard_reasons,
            "warnings": self.warnings,
            "missing_required": self.missing_required,
            "missing_important": self.missing_important,
            "reason_codes": self.reason_codes,
        }


class DetailQualityService:
    """Audit detail_data and create targeted detail rebuild tasks."""

    CODE_EMPTY = "detail_empty"
    CODE_NOT_OBJECT = "detail_not_object"
    CODE_LOGIN_PLACEHOLDER = "detail_login_placeholder"
    CODE_LOW_FIELD_COUNT = "detail_low_field_count"
    CODE_MISSING_IDENTITY = "detail_missing_identity"
    CODE_WARN_FIELD_COUNT = "detail_warn_field_count"
    CODE_MISSING_IMPORTANT = "detail_missing_important"

    DETAIL_PROBLEM_CODES = (
        CODE_EMPTY,
        CODE_NOT_OBJECT,
        CODE_LOGIN_PLACEHOLDER,
        CODE_LOW_FIELD_COUNT,
        CODE_MISSING_IDENTITY,
        CODE_WARN_FIELD_COUNT,
        CODE_MISSING_IMPORTANT,
    )

    def __init__(
        self,
        db: Session,
        min_field_count: int = DEFAULT_MIN_FIELD_COUNT,
        warn_field_count: int = DEFAULT_WARN_FIELD_COUNT,
    ):
        self.db = db
        self.min_field_count = min_field_count
        self.warn_field_count = max(warn_field_count, min_field_count)

    @staticmethod
    def _has_any_key(data: dict[str, Any], names: Iterable[str]) -> bool:
        lowered = {str(key).lower() for key in data.keys()}
        for name in names:
            if name in data or str(name).lower() in lowered:
                return True
        return False

    @staticmethod
    def _contains_login_placeholder(value: Any) -> bool:
        if isinstance(value, str):
            return any(marker in value for marker in LOGIN_PLACEHOLDER_MARKERS)
        if isinstance(value, dict):
            return any(DetailQualityService._contains_login_placeholder(v) for v in value.values())
        if isinstance(value, list):
            return any(DetailQualityService._contains_login_placeholder(v) for v in value)
        return False

    def audit_data(self, journal_id: int, detail_data: Any) -> DetailQualityResult:
        result = DetailQualityResult(journal_id=journal_id, status="ok")

        if not detail_data:
            result.hard_reasons.append(self.CODE_EMPTY)
            result.status = "bad"
            result.missing_required.append("detail_data")
            return result

        if not isinstance(detail_data, dict):
            result.hard_reasons.append(self.CODE_NOT_OBJECT)
            result.status = "bad"
            return result

        result.field_count = len(detail_data)

        if self._contains_login_placeholder(detail_data):
            result.hard_reasons.append(self.CODE_LOGIN_PLACEHOLDER)

        if result.field_count < self.min_field_count:
            result.hard_reasons.append(self.CODE_LOW_FIELD_COUNT)

        for group_name, keys in IDENTITY_FIELD_GROUPS.items():
            if not self._has_any_key(detail_data, keys):
                result.hard_reasons.append(self.CODE_MISSING_IDENTITY)
                result.missing_required.append(group_name)

        if not result.hard_reasons and result.field_count < self.warn_field_count:
            result.warnings.append(self.CODE_WARN_FIELD_COUNT)

        for group_name, keys in IMPORTANT_FIELD_GROUPS.items():
            if not self._has_any_key(detail_data, keys):
                result.missing_important.append(group_name)

        if result.missing_important:
            result.warnings.append(self.CODE_MISSING_IMPORTANT)

        result.status = "bad" if result.hard_reasons else ("warning" if result.warnings else "ok")
        return result

    def audit_journal(self, journal: Journal) -> DetailQualityResult:
        return self.audit_data(journal.journal_id, journal.detail_data)

    def mark_detail_problems_resolved(self, journal_id: int) -> None:
        problem_service = ProblemService(self.db)
        for code in self.DETAIL_PROBLEM_CODES:
            problem_service.mark_resolved_by_journal(journal_id, code)

    def record_result(self, journal_id: int, result: DetailQualityResult) -> None:
        problem_service = ProblemService(self.db)
        if result.status == "ok":
            self.mark_detail_problems_resolved(journal_id)
            return

        message = (
            f"详情质量检查: status={result.status}, field_count={result.field_count}, "
            f"reasons={','.join(result.reason_codes)}"
        )
        primary_code = result.hard_reasons[0] if result.hard_reasons else result.warnings[0]
        problem_type = ProblemType.ERROR.value if result.hard_reasons else ProblemType.WARNING.value
        problem_service.record_problem(
            journal_id=journal_id,
            problem_type=problem_type,
            problem_code=primary_code,
            message=message,
            expected_value=self.min_field_count if primary_code == self.CODE_LOW_FIELD_COUNT else None,
            actual_value=result.field_count,
        )

    def audit_completed(
        self,
        limit: int | None = None,
        include_ok: bool = False,
        record: bool = False,
        record_warnings: bool = True,
    ) -> dict[str, Any]:
        query = self.db.query(Journal).filter(Journal.detail_crawled == True)
        query = query.order_by(Journal.updated_at.asc(), Journal.id.asc())
        if limit:
            query = query.limit(limit)

        items: list[dict[str, Any]] = []
        stats = {"ok": 0, "warning": 0, "bad": 0}

        for journal in query.all():
            result = self.audit_journal(journal)
            stats[result.status] += 1
            if record and (result.status == "bad" or record_warnings) and result.status != "ok":
                self.record_result(journal.journal_id, result)
            if include_ok or result.status != "ok":
                item = result.to_dict()
                item["name"] = journal.name
                item["updated_at"] = journal.updated_at
                items.append(item)

        if record:
            self.db.commit()

        return {
            "stats": stats,
            "total_checked": sum(stats.values()),
            "items": items,
        }

    def rebuild_by_journal_ids(
        self,
        journal_ids: list[int],
        dry_run: bool = True,
        include_comments: bool = False,
    ) -> dict[str, Any]:
        task_manager = TaskManager(self.db)
        items: list[dict[str, Any]] = []
        reset_count = 0
        skipped_running = 0
        not_found: list[int] = []

        for journal_id in journal_ids:
            journal = self.db.query(Journal).filter(Journal.journal_id == journal_id).first()
            if not journal:
                not_found.append(journal_id)
                continue

            result = self.audit_journal(journal)
            item = {
                "journal_id": journal.journal_id,
                "name": journal.name,
                "quality": result.to_dict(),
                "dry_run": dry_run,
            }

            if dry_run:
                items.append(item)
                continue

            task = task_manager.reset_or_create_detail_task(journal.journal_id, journal.category_id)
            item["task_id"] = task.id
            if task.status == TaskStatus.RUNNING.value:
                skipped_running += 1
                item["action"] = "skipped_running"
            else:
                journal.detail_crawled = False
                if include_comments:
                    journal.comments_crawled = False
                    comment_task = task_manager.create_comment_task(
                        journal.journal_id,
                        journal.category_id,
                        refresh_completed=True,
                    )
                    item["comment_task_id"] = comment_task.id if comment_task else None
                extra_data = json.loads(task.extra_data or "{}")
                extra_data["rebuild_reason"] = "detail_quality"
                extra_data["quality_reasons"] = result.reason_codes
                task.extra_data = json.dumps(extra_data, ensure_ascii=False)
                reset_count += 1
                item["action"] = "reset"
            items.append(item)

        if not dry_run:
            self.db.commit()

        return {
            "dry_run": dry_run,
            "requested": len(journal_ids),
            "reset_count": reset_count,
            "skipped_running": skipped_running,
            "not_found": not_found,
            "items": items,
        }

    def rebuild_failed_audit(
        self,
        dry_run: bool = True,
        limit: int | None = None,
        include_comments: bool = False,
    ) -> dict[str, Any]:
        audit = self.audit_completed(
            limit=limit,
            include_ok=False,
            record=not dry_run,
            record_warnings=False,
        )
        bad_items = [item for item in audit["items"] if item["status"] == "bad"]
        journal_ids = [item["journal_id"] for item in bad_items]
        rebuild = self.rebuild_by_journal_ids(
            journal_ids,
            dry_run=dry_run,
            include_comments=include_comments,
        )
        return {
            "audit": {
                "stats": audit["stats"],
                "total_checked": audit["total_checked"],
                "items": bad_items,
            },
            "rebuild": rebuild,
        }
