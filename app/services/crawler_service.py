import json
import asyncio
import logging
import math
from sqlalchemy.orm import Session
from app.services.task_manager import TaskManager, generate_worker_id
from app.models.task import TaskType, TaskStatus, CrawlTask
from app.models.category import Category
from app.models.journal import Journal
from app.models.comment import Comment
from app.crawler.category_crawler import CategoryCrawler
from app.crawler.list_crawler import ListCrawler
from app.crawler.detail_crawler import DetailCrawler, DataValidationError
from app.database import SessionLocal
from app.config import config

logger = logging.getLogger(__name__)

JOURNALS_PER_PAGE = 10
NO_TASK_SLEEP_SECONDS = 5


class CrawlerService:
    """爬虫服务 — 消费者协程池模式（standalone 模式）

    启动 PARALLEL_WORKERS 个持久协程，各自独立从任务池拉取任务，
    完成即拉下一个，无批次间隙。
    """

    def __init__(self, worker_id: str = None):
        self._running = False
        self._paused = False
        self.worker_id = worker_id or generate_worker_id()
        logger.info(f"CrawlerService 初始化，worker_id: {self.worker_id}")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self):
        self._paused = True
        logger.info(f"爬虫已暂停 (worker: {self.worker_id})")

    def resume(self):
        self._paused = False
        logger.info(f"爬虫已恢复 (worker: {self.worker_id})")

    def stop(self):
        self._running = False
        logger.info(f"爬虫已停止 (worker: {self.worker_id})")

    # ------------------------------------------------------------------ #
    # 主入口                                                               #
    # ------------------------------------------------------------------ #

    async def run(self):
        self._running = True
        n = config.PARALLEL_WORKERS
        logger.info(f"爬虫服务启动 (worker: {self.worker_id})，消费者协程数: {n}")

        consumer_tasks = [asyncio.create_task(self._consumer_loop(i)) for i in range(n)]
        try:
            await asyncio.gather(*consumer_tasks)
        except asyncio.CancelledError:
            for t in consumer_tasks:
                t.cancel()

        logger.info(f"爬虫服务已停止 (worker: {self.worker_id})")

    # ------------------------------------------------------------------ #
    # 消费者主循环                                                          #
    # ------------------------------------------------------------------ #

    async def _consumer_loop(self, coroutine_id: int):
        logger.info(f"[消费者-{coroutine_id}] 启动")
        while self._running:
            if self._paused:
                await asyncio.sleep(1)
                continue

            task, task_type = await self._acquire_next_task()

            if task is None:
                logger.debug(f"[消费者-{coroutine_id}] 无任务，等待 {NO_TASK_SLEEP_SECONDS}s")
                await asyncio.sleep(NO_TASK_SLEEP_SECONDS)
                continue

            await self._execute_task(task, task_type, coroutine_id)

        logger.info(f"[消费者-{coroutine_id}] 退出")

    # ------------------------------------------------------------------ #
    # 任务获取                                                              #
    # ------------------------------------------------------------------ #

    async def _acquire_next_task(self):
        """按优先级尝试获取一个任务：CATEGORY → LIST → DETAIL。

        返回 (task, task_type) 或 (None, None)。
        """
        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            for task_type in [TaskType.CATEGORY.value, TaskType.LIST.value, TaskType.DETAIL.value]:
                tasks = tm.acquire_tasks(task_type, limit=1)
                if tasks:
                    return tasks[0], task_type
            self._maybe_retry_failed(db, tm)
            return None, None
        except Exception as e:
            logger.error(f"获取任务失败: {e}")
            db.rollback()
            return None, None
        finally:
            db.close()

    def _maybe_retry_failed(self, db, tm: TaskManager):
        try:
            failed_tasks = tm.get_failed_tasks(limit=3)
            for task in failed_tasks:
                tm.retry_task(task)
        except Exception as e:
            logger.warning(f"重置失败任务出错: {e}")

    # ------------------------------------------------------------------ #
    # 任务执行                                                              #
    # ------------------------------------------------------------------ #

    async def _execute_task(self, task, task_type: str, coroutine_id: int):
        if task_type == TaskType.CATEGORY.value:
            await self._execute_category_task(task, coroutine_id)
        elif task_type == TaskType.LIST.value:
            await self._execute_list_task(task, coroutine_id)
        elif task_type == TaskType.DETAIL.value:
            await self._execute_detail_task(task, coroutine_id)

    async def _execute_category_task(self, task, coroutine_id: int):
        tag = f"[消费者-{coroutine_id}][分类]"
        logger.info(f"{tag} 开始 task={task.id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            async with CategoryCrawler() as crawler:
                tm.renew_task_lock(task)
                categories = await crawler.crawl()
                new_count = 0
                updated_count = 0

                for cat_data in categories:
                    field_tag = cat_data["field_tag"]
                    new_total = cat_data.get("total_count", 0)
                    category = db.query(Category).filter(Category.field_tag == field_tag).first()

                    if not category:
                        category = Category(
                            field_tag=field_tag,
                            name=cat_data["name"],
                            total_count=new_total
                        )
                        db.add(category)
                        db.commit()
                        new_count += 1
                        if new_total > 0:
                            total_pages = math.ceil(new_total / JOURNALS_PER_PAGE)
                            tm.create_list_tasks(field_tag, total_pages)
                            logger.info(f"{tag} 新分类 {cat_data['name']}: {new_total} 期刊, {total_pages} 页")

                    elif category.total_count != new_total:
                        old_total = category.total_count
                        category.name = cat_data["name"]
                        category.total_count = new_total
                        db.commit()
                        updated_count += 1
                        old_pages = math.ceil(old_total / JOURNALS_PER_PAGE) if old_total > 0 else 0
                        new_pages = math.ceil(new_total / JOURNALS_PER_PAGE) if new_total > 0 else 0
                        if new_pages > old_pages:
                            tm.create_list_tasks(field_tag, new_pages)
                            logger.info(f"{tag} 分类更新 {cat_data['name']}: {old_total}->{new_total} 期刊, 新增 {new_pages - old_pages} 页")

                logger.info(f"{tag} 完成 task={task.id} 新增={new_count} 更新={updated_count} 共={len(categories)}")
                tm.complete_task(task)

        except Exception as e:
            db.rollback()
            logger.exception(f"{tag} 失败 task={task.id}")
            TaskManager(db, self.worker_id).fail_task(task, str(e))
        finally:
            db.close()

    async def _execute_list_task(self, task, coroutine_id: int):
        extra = json.loads(task.extra_data) if task.extra_data else {}
        field_tag = extra.get("field_tag")
        page = extra.get("page", 1)
        tag = f"[消费者-{coroutine_id}][列表]"
        logger.info(f"{tag} 开始 task={task.id} field={field_tag} page={page}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            category = db.query(Category).filter(Category.field_tag == field_tag).first()

            async with ListCrawler() as crawler:
                tm.renew_task_lock(task)
                journals = await crawler.crawl(field_tag, page)

                for j_data in journals:
                    journal = db.query(Journal).filter(Journal.journal_id == j_data["journal_id"]).first()
                    if not journal:
                        journal = Journal(
                            journal_id=j_data["journal_id"],
                            name=j_data["name"],
                            category_id=category.id if category else None
                        )
                        db.add(journal)
                        db.commit()
                        db.refresh(journal)
                        tm.create_detail_task(j_data["journal_id"], category.id if category else None)

            tm.complete_task(task)
            logger.info(f"{tag} 完成 task={task.id} field={field_tag} page={page} 期刊数={len(journals)}")

        except Exception as e:
            db.rollback()
            logger.exception(f"{tag} 失败 task={task.id}")
            TaskManager(db, self.worker_id).fail_task(task, str(e))
        finally:
            db.close()

    async def _execute_detail_task(self, task, coroutine_id: int):
        journal_id = int(task.target_id)
        tag = f"[消费者-{coroutine_id}][详情]"
        logger.info(f"{tag} 开始 task={task.id} journal_id={journal_id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)

            async with DetailCrawler() as crawler:
                tm.renew_task_lock(task)
                detail = await crawler.crawl(journal_id)

                journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
                if journal:
                    basic_info = detail.get("basic_info", {})
                    journal.issn = basic_info.get("issn", journal.issn)
                    journal.eissn = basic_info.get("E-ISSN")
                    journal.impact_factor = basic_info.get("impact_factor", journal.impact_factor)
                    journal.impact_factor_realtime = basic_info.get("impact_factor_realtime")
                    journal.self_citation_rate = basic_info.get("self_citation_rate")
                    journal.jcr_partition = basic_info.get("jcr_partition")
                    journal.cas_partition = basic_info.get("cas_partition")
                    journal.cas_warning = basic_info.get("cas_warning")
                    journal.citescore = basic_info.get("citescore")
                    journal.review_speed = basic_info.get("review_speed")
                    journal.acceptance_rate = basic_info.get("acceptance_rate")
                    journal.detail_data = basic_info
                    journal.detail_crawled = True

                    seen_comment_ids = set()
                    comments_to_insert = []
                    for c_data in detail.get("comments", []):
                        comment_id = c_data.get("comment_id")
                        if not comment_id or comment_id in seen_comment_ids:
                            continue
                        seen_comment_ids.add(comment_id)
                        comments_to_insert.append({
                            "journal_id": journal.journal_id,
                            "comment_id": comment_id,
                            "content": c_data.get("content"),
                            "author": c_data.get("author"),
                            "rating": c_data.get("rating"),
                            "submit_experience": c_data.get("submit_experience"),
                            "comment_time": c_data.get("comment_time")
                        })

                    if comments_to_insert:
                        from sqlalchemy.dialects.postgresql import insert
                        stmt = insert(Comment).values(comments_to_insert)
                        stmt = stmt.on_conflict_do_nothing(index_elements=['comment_id'])
                        db.execute(stmt)

                    journal.comments_crawled = True
                    db.commit()

            tm.complete_task(task)
            logger.info(f"{tag} 完成 task={task.id} journal_id={journal_id}")

        except DataValidationError as e:
            db.rollback()
            error_msg = f"数据校验失败: {str(e)}, 提取字段数: {e.extracted_fields}, 缺失字段: {e.missing_fields}"
            logger.warning(f"{tag} 校验失败 task={task.id}: {error_msg}")
            TaskManager(db, self.worker_id).fail_task(task, error_msg)

        except Exception as e:
            db.rollback()
            logger.exception(f"{tag} 失败 task={task.id} journal_id={journal_id}")
            TaskManager(db, self.worker_id).fail_task(task, str(e))
        finally:
            db.close()

    async def start_full_crawl(self):
        db = SessionLocal()
        try:
            task_manager = TaskManager(db, self.worker_id)
            task_manager.create_category_task()
            logger.info("已创建分类爬取任务，爬虫将自动开始")
        finally:
            db.close()


# 全局爬虫服务实例
crawler_service = CrawlerService()
