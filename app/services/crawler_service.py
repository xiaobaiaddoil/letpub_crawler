import json
import asyncio
import logging
from sqlalchemy.orm import Session
from app.services.task_manager import TaskManager, generate_worker_id
from app.models.task import TaskType, TaskStatus, CrawlTask
from app.models.category import Category
from app.models.journal import Journal
from app.models.comment import Comment
from app.database import SessionLocal
from app.config import config
from app.services.index_service import IndexService
from app.services.metric_service import MetricService
from app.services.detail_quality_service import DetailQualityService

logger = logging.getLogger(__name__)

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
        comment_n = max(0, config.COMMENT_PARALLEL_WORKERS)
        logger.info(
            f"爬虫服务启动 (worker: {self.worker_id})，详情/列表消费者协程数: {n}，"
            f"评论消费者协程数: {comment_n}"
        )

        consumer_tasks = [
            asyncio.create_task(self._consumer_loop(
                i,
                [TaskType.CATEGORY.value, TaskType.LIST.value, TaskType.DETAIL.value],
            ))
            for i in range(n)
        ]
        consumer_tasks.extend(
            asyncio.create_task(self._consumer_loop(
                i,
                [TaskType.COMMENT.value],
            ))
            for i in range(comment_n)
        )
        try:
            await asyncio.gather(*consumer_tasks)
        except asyncio.CancelledError:
            for t in consumer_tasks:
                t.cancel()

        logger.info(f"爬虫服务已停止 (worker: {self.worker_id})")

    # ------------------------------------------------------------------ #
    # 消费者主循环                                                          #
    # ------------------------------------------------------------------ #

    async def _consumer_loop(self, coroutine_id: int, task_types: list[str] | None = None):
        task_types = task_types or [TaskType.CATEGORY.value, TaskType.LIST.value, TaskType.DETAIL.value]
        logger.info(f"[消费者-{coroutine_id}] 启动")
        while self._running:
            if self._paused:
                await asyncio.sleep(1)
                continue

            task, task_type = await self._acquire_next_task(task_types)

            if task is None:
                logger.debug(f"[消费者-{coroutine_id}] 无任务，等待 {NO_TASK_SLEEP_SECONDS}s")
                await asyncio.sleep(NO_TASK_SLEEP_SECONDS)
                continue

            await self._execute_task(task, task_type, coroutine_id)

        logger.info(f"[消费者-{coroutine_id}] 退出")

    # ------------------------------------------------------------------ #
    # 任务获取                                                              #
    # ------------------------------------------------------------------ #

    async def _acquire_next_task(self, task_types: list[str] | None = None):
        """按优先级尝试获取一个任务。

        返回 (task, task_type) 或 (None, None)。
        """
        task_types = task_types or [TaskType.CATEGORY.value, TaskType.LIST.value, TaskType.DETAIL.value]
        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            for task_type in task_types:
                tasks = tm.acquire_tasks(task_type, limit=1)
                if tasks:
                    return tasks[0], task_type
            return None, None
        except Exception as e:
            logger.error(f"获取任务失败: {e}")
            db.rollback()
            return None, None
        finally:
            db.close()

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
        elif task_type == TaskType.COMMENT.value:
            await self._execute_comment_task(task, coroutine_id)

    async def _execute_category_task(self, task, coroutine_id: int):
        from app.crawler.category_crawler import CategoryCrawler

        tag = f"[消费者-{coroutine_id}][分类]"
        logger.info(f"{tag} 开始 task={task.id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            index_service = IndexService(db)
            async with CategoryCrawler() as crawler:
                tm.renew_task_lock(task)
                categories = await crawler.crawl()
                new_count = 0
                updated_count = 0
                scheduled_list_tasks = 0

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

                    else:
                        old_total = category.total_count or 0
                        category_changed = category.name != cat_data["name"] or old_total != new_total
                        if category_changed:
                            category.name = cat_data["name"]
                            category.total_count = new_total
                            db.commit()
                            updated_count += 1

                    state = index_service.update_category_state(category, new_total)
                    if state.status in ("changed", "missing_index"):
                        list_tasks = tm.create_list_tasks(field_tag, state.total_pages, refresh_completed=True)
                        scheduled_list_tasks += len(list_tasks)
                        logger.info(
                            f"{tag} 分类 {cat_data['name']} 索引状态={state.status} "
                            f"远端={state.remote_total_count} 本地索引={state.local_index_count} "
                            f"已安排 {len(list_tasks)} 个列表页扫描"
                        )

                logger.info(
                    f"{tag} 完成 task={task.id} 新增分类={new_count} 更新分类={updated_count} "
                    f"分类总数={len(categories)} 列表任务={scheduled_list_tasks}"
                )
                tm.complete_task(task)

        except Exception as e:
            db.rollback()
            logger.exception(f"{tag} 失败 task={task.id}")
            TaskManager(db, self.worker_id).fail_task(task, str(e))
        finally:
            db.close()

    async def _execute_list_task(self, task, coroutine_id: int):
        from app.crawler.list_crawler import ListCrawler

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
                new_count = 0
                updated_count = 0
                detail_task_count = 0
                new_index_count = 0

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
                        new_count += 1
                    else:
                        changed = False
                        if journal.name != j_data["name"]:
                            journal.name = j_data["name"]
                            changed = True
                        if category and journal.category_id is None:
                            journal.category_id = category.id
                            changed = True
                        if changed:
                            db.commit()
                            updated_count += 1

                    detail_task = tm.create_detail_task(
                        j_data["journal_id"],
                        category.id if category else journal.category_id,
                    )
                    if detail_task:
                        detail_task_count += 1

                if category:
                    # category_journal_index has an FK to journals.journal_id, so
                    # page indexes must be recorded after all page journals exist.
                    new_index_count = IndexService(db).record_list_page(category, page, journals)

            tm.complete_task(task)
            logger.info(
                f"{tag} 完成 task={task.id} field={field_tag} page={page} 期刊数={len(journals)} "
                f"新增={new_count} 更新={updated_count} 新索引={new_index_count} 详情任务={detail_task_count}"
            )

        except Exception as e:
            db.rollback()
            logger.exception(f"{tag} 失败 task={task.id}")
            TaskManager(db, self.worker_id).fail_task(task, str(e))
        finally:
            db.close()

    async def _execute_detail_task(self, task, coroutine_id: int):
        from app.crawler.detail_crawler import DetailCrawler, DataValidationError

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
                    fresh_basic_info = detail.get("basic_info", {})
                    quality_service = DetailQualityService(db)
                    quality = quality_service.audit_data(journal_id, fresh_basic_info)
                    if not quality.ok:
                        quality_service.record_result(journal_id, quality)
                        db.commit()
                        raise DataValidationError(
                            f"期刊 {journal_id} 详情质量检查未通过: {','.join(quality.hard_reasons)}",
                            missing_fields=quality.missing_required,
                            extracted_fields=quality.field_count,
                        )

                    basic_info = {
                        **(journal.detail_data or {}),
                        **fresh_basic_info,
                    }
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

                    quality_service.mark_detail_problems_resolved(journal_id)
                    MetricService(db).record_snapshot(journal, basic_info, task_id=task.id)
                    tm.create_comment_task(journal_id, journal.category_id)
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

    async def _execute_comment_task(self, task, coroutine_id: int):
        from app.crawler.detail_crawler import DetailCrawler

        journal_id = int(task.target_id)
        tag = f"[消费者-{coroutine_id}][评论]"
        logger.info(f"{tag} 开始 task={task.id} journal_id={journal_id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            tm.renew_task_lock(task)

            async with DetailCrawler() as crawler:
                comments, comment_info = await crawler.crawl_comments_only(journal_id)

            journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
            if journal:
                seen_comment_ids = set()
                comments_to_insert = []
                for c_data in comments:
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

                basic_info = dict(journal.detail_data or {})
                basic_info["comment_count"] = comment_info.get("total_count", 0)
                basic_info["comment_pages"] = comment_info.get("total_pages", 0)
                basic_info["crawled_comment_count"] = comment_info.get("crawled_count", 0)
                journal.detail_data = basic_info
                journal.comments_crawled = True
                MetricService(db).record_snapshot(journal, basic_info, task_id=task.id)
                db.commit()

            tm.complete_task(task)
            logger.info(f"{tag} 完成 task={task.id} journal_id={journal_id}")

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
