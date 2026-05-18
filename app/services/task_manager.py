import json
import logging
import uuid
import socket
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from sqlalchemy.orm import Session
from app.models.task import CrawlTask, TaskType, TaskStatus
from app.models.journal import Journal
from app.config import config

logger = logging.getLogger(__name__)

# 任务锁定超时时间（秒）- 超过此时间的RUNNING任务会被释放
TASK_LOCK_TIMEOUT = config.TASK_LOCK_TIMEOUT


def get_utc_now() -> datetime:
    """获取当前UTC时间"""
    return datetime.now(timezone.utc)


def generate_worker_id() -> str:
    """生成唯一的worker标识"""
    # 如果配置了 WORKER_ID，使用配置的值
    if config.WORKER_ID:
        return config.WORKER_ID

    hostname = socket.gethostname()
    pid = os.getpid()
    unique_id = uuid.uuid4().hex[:8]
    return f"{hostname}-{pid}-{unique_id}"


class TaskManager:
    """任务管理器 - 支持分布式部署"""

    def __init__(self, db: Session, worker_id: str = None):
        self.db = db
        self.worker_id = worker_id or generate_worker_id()
        logger.info(f"TaskManager 初始化，worker_id: {self.worker_id}")

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
        """获取待处理任务（旧方法，保留兼容性）"""
        query = self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.PENDING.value
        )

        if task_type:
            query = query.filter(CrawlTask.task_type == task_type)

        return query.order_by(CrawlTask.created_at).limit(limit).all()

    def acquire_tasks(self, task_type: str = None, limit: int = 1) -> List[CrawlTask]:
        """原子性获取并锁定任务（分布式安全）

        使用 SELECT ... FOR UPDATE SKIP LOCKED 实现：
        - 原子性：获取和锁定在同一事务中完成
        - 无竞争：SKIP LOCKED 跳过已被其他worker锁定的行
        - 超时释放：同时释放超时的任务
        """
        now = get_utc_now()
        timeout_threshold = now - timedelta(seconds=TASK_LOCK_TIMEOUT)

        acquired_tasks = []

        try:
            # 先释放超时的任务（被其他节点锁定但超时的）
            self._release_timeout_tasks(timeout_threshold)

            # 构建查询：获取 PENDING 状态的任务
            query = self.db.query(CrawlTask).filter(
                CrawlTask.status == TaskStatus.PENDING.value
            )

            if task_type:
                query = query.filter(CrawlTask.task_type == task_type)

            # 使用 FOR UPDATE SKIP LOCKED 进行行级锁定
            # SKIP LOCKED: 跳过已被锁定的行，避免等待
            tasks = query.order_by(CrawlTask.created_at).limit(limit).with_for_update(skip_locked=True).all()

            for task in tasks:
                # 原子性地更新任务状态
                task.status = TaskStatus.RUNNING.value
                task.worker_id = self.worker_id
                task.locked_at = now
                task.started_at = now
                acquired_tasks.append(task)

            self.db.commit()

            if acquired_tasks:
                logger.info(f"Worker {self.worker_id} 获取 {len(acquired_tasks)} 个任务: {[t.id for t in acquired_tasks]}")

        except Exception as e:
            self.db.rollback()
            logger.error(f"获取任务失败: {e}")

        return acquired_tasks

    def _release_timeout_tasks(self, timeout_threshold: datetime):
        """释放超时的任务"""
        try:
            count = self.db.query(CrawlTask).filter(
                CrawlTask.status == TaskStatus.RUNNING.value,
                CrawlTask.locked_at < timeout_threshold
            ).update({
                CrawlTask.status: TaskStatus.PENDING.value,
                CrawlTask.worker_id: None,
                CrawlTask.locked_at: None,
                CrawlTask.started_at: None
            })

            if count > 0:
                self.db.commit()
                logger.warning(f"释放 {count} 个超时任务")
        except Exception as e:
            logger.error(f"释放超时任务失败: {e}")

    def get_failed_tasks(self, limit: int = 10) -> List[CrawlTask]:
        """获取失败任务（可重试）"""
        return self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.FAILED.value,
            CrawlTask.retry_count < CrawlTask.max_retry
        ).order_by(CrawlTask.created_at).limit(limit).all()

    def start_task(self, task: CrawlTask):
        """开始任务（旧方法，使用 acquire_tasks 更安全）"""
        now = get_utc_now()
        task.status = TaskStatus.RUNNING.value
        task.worker_id = self.worker_id
        task.locked_at = now
        task.started_at = now
        self.db.commit()
        logger.info(f"开始任务: {task.id} ({task.task_type}) by {self.worker_id}")

    def complete_task(self, task: CrawlTask):
        """完成任务"""
        now = get_utc_now()
        self.db.query(CrawlTask).filter(CrawlTask.id == task.id).update({
            CrawlTask.status: TaskStatus.COMPLETED.value,
            CrawlTask.completed_at: now,
            CrawlTask.locked_at: None,
        }, synchronize_session=False)
        self.db.commit()
        logger.info(f"完成任务: {task.id} by {self.worker_id}")

    def renew_task_lock(self, task: CrawlTask):
        """续期任务锁定时间（防止长时间任务被误判为超时）"""
        now = get_utc_now()
        self.db.query(CrawlTask).filter(CrawlTask.id == task.id).update({
            CrawlTask.locked_at: now,
        }, synchronize_session=False)
        self.db.commit()
        logger.debug(f"续期任务锁: {task.id}")

    def fail_task(self, task: CrawlTask, error: str):
        """任务失败"""
        now = get_utc_now()
        self.db.query(CrawlTask).filter(CrawlTask.id == task.id).update({
            CrawlTask.status: TaskStatus.FAILED.value,
            CrawlTask.retry_count: CrawlTask.retry_count + 1,
            CrawlTask.error_message: error,
            CrawlTask.completed_at: now,
            CrawlTask.locked_at: None,
        }, synchronize_session=False)
        self.db.commit()
        logger.error(f"任务失败: {task.id}, 错误: {error}, worker: {self.worker_id}")

    def retry_task(self, task: CrawlTask):
        """重试任务"""
        if task.retry_count < task.max_retry:
            self.db.query(CrawlTask).filter(CrawlTask.id == task.id).update({
                CrawlTask.status: TaskStatus.PENDING.value,
                CrawlTask.error_message: None,
                CrawlTask.worker_id: None,
                CrawlTask.locked_at: None,
            }, synchronize_session=False)
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

        # 按worker统计运行中的任务
        worker_stats = {}
        running_tasks = self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.RUNNING.value
        ).all()
        for task in running_tasks:
            worker = task.worker_id or "unknown"
            worker_stats[worker] = worker_stats.get(worker, 0) + 1

        return {
            "by_status": stats,
            "by_type": type_stats,
            "by_worker": worker_stats,
            "total": sum(stats.values())
        }

    def reset_running_tasks(self, only_current_worker: bool = False):
        """重置运行中的任务为待处理

        Args:
            only_current_worker: 如果为True，只重置当前worker的任务
        """
        query = self.db.query(CrawlTask).filter(
            CrawlTask.status == TaskStatus.RUNNING.value
        )

        if only_current_worker:
            query = query.filter(CrawlTask.worker_id == self.worker_id)

        count = query.update({
            CrawlTask.status: TaskStatus.PENDING.value,
            CrawlTask.worker_id: None,
            CrawlTask.locked_at: None,
            CrawlTask.started_at: None
        })
        self.db.commit()
        logger.info(f"重置 {count} 个运行中的任务 (only_current_worker={only_current_worker})")
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
