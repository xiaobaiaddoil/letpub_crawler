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
from app.models.journal_index import CategoryIndexState
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

    @staticmethod
    def _reset_task_to_pending(task: CrawlTask):
        task.status = TaskStatus.PENDING.value
        task.retry_count = 0
        task.error_message = None
        task.worker_id = None
        task.locked_at = None
        task.started_at = None
        task.completed_at = None

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

    def create_index_check_task(self) -> CrawlTask:
        """创建新增期刊索引检测任务."""
        return self.create_category_task()

    def create_list_tasks(
        self,
        field_tag: str,
        total_pages: int,
        refresh_completed: bool = False,
    ) -> List[CrawlTask]:
        """创建列表页爬取任务.

        Args:
            field_tag: LetPub 分类 fieldtag。
            total_pages: 当前分类页数。
            refresh_completed: 为 True 时，已完成的列表页也会重新置为 pending。
                这用于增量扫描：重新读取列表页，但列表执行阶段只会为新增期刊创建详情任务。
        """
        tasks = []
        for page in range(1, total_pages + 1):
            target_id = f"{field_tag}:{page}"
            url = f"{config.BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag={field_tag}&firstletter=&currentpage={page}"
            extra_data = json.dumps({"field_tag": field_tag, "page": page})

            existing = self.db.query(CrawlTask).filter(
                CrawlTask.task_type == TaskType.LIST.value,
                CrawlTask.target_id == target_id
            ).order_by(CrawlTask.id.desc()).first()

            if existing:
                existing.target_url = url
                existing.extra_data = extra_data
                if existing.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
                    tasks.append(existing)
                elif refresh_completed or existing.status == TaskStatus.FAILED.value:
                    self._reset_task_to_pending(existing)
                    tasks.append(existing)
                continue

            task = CrawlTask(
                task_type=TaskType.LIST.value,
                target_id=target_id,
                target_url=url,
                status=TaskStatus.PENDING.value,
                extra_data=extra_data
            )
            self.db.add(task)
            tasks.append(task)

        self.db.commit()
        logger.info(f"创建/刷新 {len(tasks)} 个列表任务 (分类: {field_tag}, refresh_completed={refresh_completed})")
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

        existing = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == target_id
        ).order_by(CrawlTask.id.desc()).first()

        if existing:
            if existing.status in (TaskStatus.PENDING.value, TaskStatus.RUNNING.value):
                return existing
            self._reset_task_to_pending(existing)
            existing.target_url = f"{config.BASE_URL}/index.php?journalid={journal_id}&page=journalapp&view=detail"
            existing.extra_data = json.dumps({"journal_id": journal_id, "category_id": category_id})
            self.db.commit()
            self.db.refresh(existing)
            return existing

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

    def reset_or_create_detail_task(self, journal_id: int, category_id: int = None) -> CrawlTask:
        """重置或创建详情任务，用于增量更新和手动重爬.

        与 create_detail_task 不同，本方法不会因为 Journal.detail_crawled=True 而跳过。
        如果任务正在运行，则只返回现有任务，避免把正在执行的任务重置回 pending。
        """
        target_id = str(journal_id)
        url = f"{config.BASE_URL}/index.php?journalid={journal_id}&page=journalapp&view=detail"
        extra_data = json.dumps({"journal_id": journal_id, "category_id": category_id})

        task = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == target_id
        ).order_by(CrawlTask.id.desc()).first()

        if task:
            task.target_url = url
            task.extra_data = extra_data
            if task.status != TaskStatus.RUNNING.value:
                self._reset_task_to_pending(task)
            self.db.commit()
            self.db.refresh(task)
            return task

        task = CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id=target_id,
            target_url=url,
            status=TaskStatus.PENDING.value,
            extra_data=extra_data
        )
        self.db.add(task)
        self.db.commit()
        self.db.refresh(task)
        return task

    def create_index_scan_tasks(self, statuses: tuple[str, ...] = ("changed", "missing_index")) -> int:
        """为索引检测发现差异的分类创建列表扫描任务."""
        states = self.db.query(CategoryIndexState).filter(
            CategoryIndexState.status.in_(statuses),
            CategoryIndexState.total_pages > 0,
        ).all()

        created = 0
        for state in states:
            created += len(self.create_list_tasks(
                state.field_tag,
                state.total_pages,
                refresh_completed=True,
            ))
        return created

    def create_full_detail_refresh_tasks(self, limit: int | None = None) -> int:
        """为所有已知期刊创建详情刷新任务，用于全量指标更新."""
        query = self.db.query(Journal).order_by(Journal.updated_at.asc().nullsfirst())
        if limit:
            query = query.limit(limit)

        created = 0
        for journal in query.all():
            task = self.reset_or_create_detail_task(journal.journal_id, journal.category_id)
            if task.status != TaskStatus.RUNNING.value:
                created += 1
        return created

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
        journal = self.db.query(Journal).filter(
            Journal.journal_id == journal_id
        ).first()

        if journal:
            journal.detail_crawled = False
            journal.comments_crawled = False
            self.reset_or_create_detail_task(journal_id, journal.category_id)
            logger.info(f"重置期刊 {journal_id} 的详情任务")
            return True

        task = self.db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == str(journal_id)
        ).first()
        if task:
            self._reset_task_to_pending(task)
            self.db.commit()
            logger.info(f"重置孤立详情任务: journal_id={journal_id}")
            return True

        logger.warning(f"重置详情任务失败，期刊不存在且无任务: journal_id={journal_id}")
        return False

    def reset_all_detail_tasks(self) -> int:
        """重置所有已完成的详情任务，用于全量重新爬取"""
        journals = self.db.query(Journal).all()
        count = 0
        for journal in journals:
            journal.detail_crawled = False
            journal.comments_crawled = False
            self.reset_or_create_detail_task(journal.journal_id, journal.category_id)
            count += 1

        logger.info(f"重置/创建 {count} 个详情任务")
        return count
