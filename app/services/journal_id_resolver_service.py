import json
import unicodedata
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.config import config
from app.crawler.list_crawler import ListCrawler
from app.models.category import Category
from app.models.journal import Journal
from app.models.task import CrawlTask, TaskStatus, TaskType


class JournalIdResolverService:
    """Resolve stale LetPub journal IDs through name search and update detail tasks."""

    DETAIL_QUALITY_ERROR_MARKERS = (
        "详情质量检查未通过",
        "detail_missing_identity",
        "detail_low_field_count",
        "detail_empty",
        "detail_not_object",
        "detail_login_placeholder",
        "数据校验失败",
    )

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def normalize_name(name: str | None) -> str:
        text = unicodedata.normalize("NFKC", (name or "").strip()).casefold()
        return "".join(character for character in text if character.isalnum())

    @staticmethod
    def _load_extra_data(extra_data: str | None) -> dict[str, Any]:
        if not extra_data:
            return {}
        try:
            data = json.loads(extra_data)
            return data if isinstance(data, dict) else {}
        except (TypeError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _detail_url(journal_id: int) -> str:
        return f"{config.BASE_URL}/index.php?journalid={journal_id}&page=journalapp&view=detail"

    @staticmethod
    def _task_summary(task: CrawlTask) -> dict[str, Any]:
        return {
            "id": task.id,
            "target_id": task.target_id,
            "target_url": task.target_url,
            "status": task.status,
            "retry_count": task.retry_count,
            "max_retry": task.max_retry,
            "error_message": task.error_message,
            "extra_data": JournalIdResolverService._load_extra_data(task.extra_data),
            "created_at": task.created_at,
            "completed_at": task.completed_at,
        }

    @classmethod
    def is_detail_quality_failure_task(cls, task: CrawlTask) -> bool:
        """Only stale-ID repair tasks that failed detail quality checks."""
        if task.task_type != TaskType.DETAIL.value or task.status != TaskStatus.FAILED.value:
            return False

        extra_data = cls._load_extra_data(task.extra_data)
        quality_reasons = extra_data.get("quality_reasons")
        if extra_data.get("rebuild_reason") == "detail_quality":
            return True
        if isinstance(quality_reasons, list) and any(str(reason).startswith("detail_") for reason in quality_reasons):
            return True

        error_message = task.error_message or ""
        return any(marker in error_message for marker in cls.DETAIL_QUALITY_ERROR_MARKERS)

    @classmethod
    def choose_candidate(
        cls,
        old_journal_id: int,
        journal_name: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        normalized_target = cls.normalize_name(journal_name)
        if not normalized_target:
            return None
        usable = [
            candidate
            for candidate in candidates
            if candidate.get("journal_id") and int(candidate["journal_id"]) != int(old_journal_id)
        ]
        if not usable:
            return None

        for candidate in usable:
            if cls.normalize_name(candidate.get("name")) == normalized_target:
                return candidate
        return None

    async def search_by_journal_name(self, journal_name: str) -> list[dict[str, Any]]:
        async with ListCrawler() as crawler:
            return await crawler.search_by_name(journal_name)

    def _detail_tasks_for_journal(self, journal_id: int) -> list[CrawlTask]:
        return (
            self.db.query(CrawlTask)
            .filter(
                CrawlTask.task_type == TaskType.DETAIL.value,
                CrawlTask.target_id == str(journal_id),
            )
            .order_by(CrawlTask.id.desc())
            .all()
        )

    def _detail_quality_failure_tasks_for_journal(self, journal_id: int) -> list[CrawlTask]:
        return [
            task
            for task in self._detail_tasks_for_journal(journal_id)
            if self.is_detail_quality_failure_task(task)
        ]

    async def crawl_category_journals(self, category: Category) -> list[dict[str, Any]]:
        journals: list[dict[str, Any]] = []
        async with ListCrawler() as crawler:
            total_pages = await crawler.get_total_pages(category.field_tag)
            for page in range(1, total_pages + 1):
                page_journals = await crawler.crawl(category.field_tag, page)
                for position_no, item in enumerate(page_journals, start=1):
                    if not item.get("journal_id"):
                        continue
                    journals.append({
                        **item,
                        "field_tag": category.field_tag,
                        "page": page,
                        "position_no": position_no,
                    })
        return journals

    def _annotate_existing_journals(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidate_ids = [int(item["journal_id"]) for item in candidates if item.get("journal_id")]
        existing_ids = set()
        if candidate_ids:
            existing_ids = {
                row[0]
                for row in self.db.query(Journal.journal_id)
                .filter(Journal.journal_id.in_(candidate_ids))
                .all()
            }
        return [
            {
                **item,
                "exists_in_db": int(item["journal_id"]) in existing_ids,
            }
            for item in candidates
        ]

    @classmethod
    def choose_category_candidate(
        cls,
        old_journal_id: int,
        journal_name: str,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        normalized_target = cls.normalize_name(journal_name)
        if not normalized_target:
            return None
        usable = [
            candidate
            for candidate in candidates
            if candidate.get("journal_id") and int(candidate["journal_id"]) != int(old_journal_id)
        ]
        if not usable:
            return None

        for candidate in usable:
            if cls.normalize_name(candidate.get("name")) == normalized_target:
                return candidate
        return None

    def apply_resolution(
        self,
        old_journal_id: int,
        new_journal_id: int,
        journal_name: str,
        candidates: list[dict[str, Any]],
        dry_run: bool = True,
        method: str = "letpub_name_search",
        tasks: list[CrawlTask] | None = None,
    ) -> dict[str, Any]:
        journal = self.db.query(Journal).filter(Journal.journal_id == old_journal_id).first()
        if not journal:
            return {
                "journal_id": old_journal_id,
                "status": "not_found",
                "message": "旧期刊ID不存在",
            }

        tasks = tasks if tasks is not None else self._detail_quality_failure_tasks_for_journal(old_journal_id)
        if not tasks:
            return {
                "journal_id": old_journal_id,
                "journal_name": journal.name,
                "dry_run": dry_run,
                "status": "not_detail_quality_failure",
                "message": "未找到详情质量失败的 detail 任务，跳过ID修复",
            }

        updated_tasks = []
        skipped_running = []
        now = datetime.now(timezone.utc)
        for task in tasks:
            if task.status == TaskStatus.RUNNING.value:
                skipped_running.append(self._task_summary(task))
                continue

            before = self._task_summary(task)
            extra_data = self._load_extra_data(task.extra_data)
            extra_data["journal_id"] = old_journal_id
            extra_data["category_id"] = journal.category_id
            extra_data["fetch_journal_id"] = new_journal_id
            extra_data["resolved_journal_id"] = new_journal_id
            extra_data["journal_id_resolution"] = {
                "old_journal_id": old_journal_id,
                "new_journal_id": new_journal_id,
                "journal_name": journal_name,
                "method": method,
                "resolved_at": now.isoformat(),
                "candidates": candidates[:5],
            }

            if not dry_run:
                task.target_url = self._detail_url(new_journal_id)
                task.extra_data = json.dumps(extra_data, ensure_ascii=False)
                task.status = TaskStatus.PENDING.value
                task.retry_count = 0
                task.error_message = None
                task.worker_id = None
                task.locked_at = None
                task.started_at = None
                task.completed_at = None

            after = dict(before)
            after.update({
                "target_url": self._detail_url(new_journal_id),
                "status": TaskStatus.PENDING.value,
                "retry_count": 0,
                "error_message": None,
                "extra_data": extra_data,
            })
            updated_tasks.append({
                "before": before,
                "after": after,
            })

        if not dry_run:
            journal.detail_crawled = False
            self.db.commit()

        return {
            "journal_id": old_journal_id,
            "journal_name": journal.name,
            "new_journal_id": new_journal_id,
            "dry_run": dry_run,
            "status": "updated" if updated_tasks else "skipped",
            "updated_tasks": updated_tasks,
            "skipped_running": skipped_running,
        }

    def keep_unresolvable_detail_tasks(
        self,
        old_journal_id: int,
        tasks: list[CrawlTask],
        search_candidates: list[dict[str, Any]],
        category_candidates: list[dict[str, Any]],
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return {
            "journal_id": old_journal_id,
            "dry_run": dry_run,
            "status": "unresolved",
            "message": "名称搜索和分类全页对比均未找到可确认的新ID，保留详情质量失败任务",
            "failed_tasks": [self._task_summary(task) for task in tasks],
            "search_candidates": search_candidates,
            "category_candidates_sample": category_candidates[:20],
            "category_candidate_count": len(category_candidates),
        }

    async def resolve_one(self, old_journal_id: int, dry_run: bool = True) -> dict[str, Any]:
        journal = self.db.query(Journal).filter(Journal.journal_id == old_journal_id).first()
        if not journal:
            return {
                "journal_id": old_journal_id,
                "status": "not_found",
                "message": "旧期刊ID不存在",
            }

        quality_failure_tasks = self._detail_quality_failure_tasks_for_journal(old_journal_id)
        if not quality_failure_tasks:
            return {
                "journal_id": old_journal_id,
                "journal_name": journal.name,
                "dry_run": dry_run,
                "status": "not_detail_quality_failure",
                "message": "未找到详情质量失败的 detail 任务，跳过ID修复",
            }

        candidates = await self.search_by_journal_name(journal.name)
        selected = self.choose_candidate(old_journal_id, journal.name, candidates)
        method = "letpub_name_search"
        category_candidates: list[dict[str, Any]] = []

        if not selected:
            category = None
            if journal.category_id:
                category = self.db.query(Category).filter(Category.id == journal.category_id).first()

            if category:
                category_candidates = self._annotate_existing_journals(
                    await self.crawl_category_journals(category)
                )
                selected = self.choose_category_candidate(
                    old_journal_id,
                    journal.name,
                    category_candidates,
                )
                method = "category_full_scan"

        if not selected:
            result = self.keep_unresolvable_detail_tasks(
                old_journal_id,
                quality_failure_tasks,
                candidates,
                category_candidates,
                dry_run=dry_run,
            )
            result["journal_name"] = journal.name
            return result

        result = self.apply_resolution(
            old_journal_id,
            int(selected["journal_id"]),
            journal.name,
            category_candidates or candidates,
            dry_run=dry_run,
            method=method,
            tasks=quality_failure_tasks,
        )
        result["selected_candidate"] = selected
        result["candidates"] = candidates
        result["category_candidates_sample"] = category_candidates[:20]
        result["category_candidate_count"] = len(category_candidates)
        return result

    async def resolve_many(self, journal_ids: list[int], dry_run: bool = True) -> dict[str, Any]:
        items = []
        for journal_id in journal_ids:
            items.append(await self.resolve_one(journal_id, dry_run=dry_run))
        return {
            "dry_run": dry_run,
            "requested": len(journal_ids),
            "updated": sum(1 for item in items if item.get("status") == "updated"),
            "unresolved": sum(1 for item in items if item.get("status") == "unresolved"),
            "items": items,
        }
