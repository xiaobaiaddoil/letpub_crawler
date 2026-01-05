import json
import logging
from datetime import datetime
from typing import List, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from app.models.task import CrawlTask, TaskType, TaskStatus
from app.models.category import Category
from app.models.journal import Journal
from app.config import config

logger = logging.getLogger(__name__)

class TaskManager:
    """任务管理器"""

    def __init__(self, db: Session):
        self.db = db

    def create_category_task(self) -> CrawlTask:
        """创建分类爬取任务"""
        # 检查是否已存在
        existing = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.CATEGORY.value,
            CrawlTask.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value])
        ).first()

        if existing:
            return existing

        task = CrawlTask(
            task_type=TaskType.CATEGORY.value,
            target_id="all",
            target_url=config.ENTRY_URL,
            status=TaskStatus.PENDING.value
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        logger.info(f"创建分类任务: {task.id}")
        return task

    def create_list_tasks(self, field_tag: str, total_pages: int) -> List[CrawlTask]:
        """创建列表页爬取任务"""
        tasks = []
        for page in range(1, total_pages + 1):
            target_id = f"{field_tag}:{page}"

            # 检查是否已存在
            existing = self.db.query(CrawlTask).filter(
                CrawlTask.task_type == TaskType.LIST.value,
                CrawlTask.target_id == target_id,
                CrawlTask.status == TaskStatus.COMPLETED.value
            ).first()

            if existing:
                continue

            # 检查是否有未完成的任务
            pending = self.db.query(CrawlTask).filter(
                CrawlTask.task_type == TaskType.LIST.value,
                CrawlTask.target_id == target_id,
                CrawlTask.status.in_([TaskStatus.PENDING.value, TaskStatus.RUNNING.value])
            ).first()

            if pending:
                tasks.append(pending)
                continue

            url = f"{config.BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag={field_tag}&firstletter=&currentpage={page}"
            task = CrawlTask(
                task_type=TaskType.LIST.value,
                target_id=target_id,
                target_url=url,
                status=TaskStatus.PENDING.value,
                extra_data=json.dumps({"field_tag": field_tag, "page": page})
            )
            self.db.add(task)
            tasks.append(task)

        self.db.commit()
        logger.info(f"创建 {len(tasks)} 个列表任务 (分类: {field_tag})")
        return tasks

    def create_detail_task(self, journal_id: int, category_id: int = None) -> Optional[CrawlTask]:
        """创建详情页爬取任务"""
        # 检查期刊是否已爬取详情
        journal = self.db.query(Journal).filter(
            Journal.journal_id == journal_id
        ).first()

        if journal and journal.detail_crawled:
            return None

        target_id = str(journal_id)

        # 检查是否有已完成或进行中的任务
        existing = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == target_id,
            CrawlTask.status.in_([
                TaskStatus.COMPLETED.value,
                TaskStatus.PENDING.value,
                TaskStatus.RUNNING.value
            ])
        ).first()

        if existing:
            return existing if existing.status != TaskStatus.COMPLETED.value else None

        url = f"{config.BASE_URL}/index.php?journalid={journal_id}&page=journalapp&view=detail"
        task = CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id=target_id,
            target_url=url,
            status=TaskStatus.PENDING.value,
            extra_data=json.dumps({"journal_id": journal_id, "category_id": category_id})
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def get_pending_tasks(self, task_type: str = None, limit: int = 10) -> List[CrawlTask]:
        """获取待处理任务"""
        query = self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.PENDING.value
        )

        if task_type:
            query = query.filter(CrawlTask.task_type == task_type)

        return query.order_by(CrawlTask.created_at).limit(limit).all()

    def get_failed_tasks(self, limit: int = 10) -> List[CrawlTask]:
        """获取失败任务（可重试）"""
        return self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.FAILED.value,
            CrawlTask.retry_count < CrawlTask.max_retry
        ).order_by(CrawlTask.created_at).limit(limit).all()

    def start_task(self, task: CrawlTask):
        """开始任务"""
        task.status = TaskStatus.RUNNING.value
        task.started_at = datetime.utcnow()
        self.db.commit()
        logger.info(f"开始任务: {task.id} ({task.task_type})")

    def complete_task(self, task: CrawlTask):
        """完成任务"""
        task.status = TaskStatus.COMPLETED.value
        task.completed_at = datetime.utcnow()
        self.db.commit()
        logger.info(f"完成任务: {task.id}")

    def fail_task(self, task: CrawlTask, error: str):
        """任务失败"""
        task.status = TaskStatus.FAILED.value
        task.retry_count += 1
        task.error_message = error
        task.completed_at = datetime.utcnow()
        self.db.commit()
        logger.error(f"任务失败: {task.id}, 错误: {error}")

    def retry_task(self, task: CrawlTask):
        """重试任务"""
        if task.retry_count < task.max_retry:
            task.status = TaskStatus.PENDING.value
            task.error_message = None
            self.db.commit()
            logger.info(f"重试任务: {task.id}")
            return True
        return False

    def get_stats(self) -> dict:
        """获取任务统计"""
        stats = {}
        for status in TaskStatus:
            count = self.db.query(CrawlTask).filter(
                CrawlTask.status == status.value
            ).count()
            stats[status.value] = count

        # 按类型统计
        type_stats = {}
        for task_type in TaskType:
            count = self.db.query(CrawlTask).filter(
                CrawlTask.task_type == task_type.value
            ).count()
            type_stats[task_type.value] = count

        return {
            "by_status": stats,
            "by_type": type_stats,
            "total": sum(stats.values())
        }

    def reset_running_tasks(self):
        """重置运行中的任务为待处理（用于程序重启后恢复）"""
        count = self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.RUNNING.value
        ).update({CrawlTask.status: TaskStatus.PENDING.value})
        self.db.commit()
        logger.info(f"重置 {count} 个运行中的任务")
        return count

    def reset_detail_task(self, journal_id: int) -> bool:
        """重置期刊详情任务状态，用于重新爬取"""
        target_id = str(journal_id)

        # 更新任务状态为pending
        task = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == target_id
        ).first()

        if task:
            task.status = TaskStatus.PENDING.value
            task.retry_count = 0
            task.error_message = None
            task.started_at = None
            task.completed_at = None

        # 同时重置期刊的爬取状态
        journal = self.db.query(Journal).filter(
            Journal.journal_id == journal_id
        ).first()

        if journal:
            journal.detail_crawled = False
            journal.comments_crawled = False

        self.db.commit()
        logger.info(f"重置期刊 {journal_id} 的详情任务")
        return True

    def reset_all_detail_tasks(self) -> int:
        """重置所有已完成的详情任务，用于全量重新爬取"""
        # 重置所有详情任务
        count = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.status == TaskStatus.COMPLETED.value
        ).update({
            CrawlTask.status: TaskStatus.PENDING.value,
            CrawlTask.retry_count: 0,
            CrawlTask.error_message: None,
            CrawlTask.started_at: None,
            CrawlTask.completed_at: None
        })

        # 重置所有期刊的爬取状态
        self.db.query(Journal).update({
            Journal.detail_crawled: False,
            Journal.comments_crawled: False
        })

        self.db.commit()
        logger.info(f"重置 {count} 个详情任务")
        return count
