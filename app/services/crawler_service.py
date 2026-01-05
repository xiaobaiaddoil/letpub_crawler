import json
import asyncio
import logging
import math
from typing import Optional
from sqlalchemy.orm import Session
from app.services.task_manager import TaskManager
from app.models.task import TaskType, TaskStatus, CrawlTask
from app.models.category import Category
from app.models.journal import Journal
from app.models.comment import Comment
from app.crawler.category_crawler import CategoryCrawler
from app.crawler.list_crawler import ListCrawler
from app.crawler.detail_crawler import DetailCrawler
from app.database import SessionLocal

logger = logging.getLogger(__name__)

# 每页期刊数量（根据网站实际情况调整）
JOURNALS_PER_PAGE = 10

class CrawlerService:
    """爬虫服务 - 协调各爬虫执行"""

    def __init__(self):
        self._running = False
        self._paused = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return self._paused

    def pause(self):
        """暂停爬虫"""
        self._paused = True
        logger.info("爬虫已暂停")

    def resume(self):
        """恢复爬虫"""
        self._paused = False
        logger.info("爬虫已恢复")

    def stop(self):
        """停止爬虫"""
        self._running = False
        logger.info("爬虫已停止")

    async def run(self):
        """运行爬虫主循环"""
        self._running = True
        logger.info("爬虫服务启动")

        while self._running:
            if self._paused:
                await asyncio.sleep(1)
                continue

            db = SessionLocal()
            try:
                task_manager = TaskManager(db)

                # 重置之前运行中的任务
                task_manager.reset_running_tasks()

                # 首先处理分类任务
                await self._process_category_tasks(db, task_manager)

                # 处理列表任务
                await self._process_list_tasks(db, task_manager)

                # 处理详情任务
                await self._process_detail_tasks(db, task_manager)

                # 重试失败任务
                await self._retry_failed_tasks(db, task_manager)

                # 检查是否还有任务
                pending_count = len(task_manager.get_pending_tasks(limit=1))
                if pending_count == 0:
                    logger.info("所有任务完成，等待新任务...")
                    await asyncio.sleep(10)

            except Exception as e:
                logger.error(f"爬虫循环错误: {e}")
                await asyncio.sleep(5)
            finally:
                db.close()

        logger.info("爬虫服务已停止")

    async def _process_category_tasks(self, db: Session, task_manager: TaskManager):
        """处理分类任务 - 增量爬取，只处理有变化的分类"""
        tasks = task_manager.get_pending_tasks(TaskType.CATEGORY.value, limit=1)
        if not tasks:
            return

        task = tasks[0]
        task_manager.start_task(task)

        try:
            async with CategoryCrawler() as crawler:
                categories = await crawler.crawl()

                new_count = 0
                updated_count = 0

                for cat_data in categories:
                    field_tag = cat_data["field_tag"]
                    new_total = cat_data.get("total_count", 0)

                    # 查询已有分类
                    category = db.query(Category).filter(
                        Category.field_tag == field_tag
                    ).first()

                    if not category:
                        # 新分类
                        category = Category(
                            field_tag=field_tag,
                            name=cat_data["name"],
                            total_count=new_total
                        )
                        db.add(category)
                        db.commit()
                        new_count += 1

                        # 创建列表任务
                        if new_total > 0:
                            total_pages = math.ceil(new_total / JOURNALS_PER_PAGE)
                            task_manager.create_list_tasks(field_tag, total_pages)
                            logger.info(f"新分类 {cat_data['name']}: {new_total} 期刊, {total_pages} 页")

                    elif category.total_count != new_total:
                        # 期刊数量有变化，需要更新
                        old_total = category.total_count
                        category.name = cat_data["name"]
                        category.total_count = new_total
                        db.commit()
                        updated_count += 1

                        # 只为新增的页创建任务
                        old_pages = math.ceil(old_total / JOURNALS_PER_PAGE) if old_total > 0 else 0
                        new_pages = math.ceil(new_total / JOURNALS_PER_PAGE) if new_total > 0 else 0

                        if new_pages > old_pages:
                            # 创建新增页面的任务
                            task_manager.create_list_tasks(field_tag, new_pages)
                            logger.info(f"分类更新 {cat_data['name']}: {old_total}->{new_total} 期刊, 新增 {new_pages - old_pages} 页")
                    # 如果分类已存在且数量没变化，跳过

                logger.info(f"分类任务完成: 新增 {new_count}, 更新 {updated_count}, 共 {len(categories)} 个分类")
                task_manager.complete_task(task)

        except Exception as e:
            task_manager.fail_task(task, str(e))

    async def _process_list_tasks(self, db: Session, task_manager: TaskManager):
        """处理列表任务"""
        tasks = task_manager.get_pending_tasks(TaskType.LIST.value, limit=5)

        for task in tasks:
            if not self._running or self._paused:
                break

            task_manager.start_task(task)

            try:
                extra = json.loads(task.extra_data) if task.extra_data else {}
                field_tag = extra.get("field_tag")
                page = extra.get("page", 1)

                # 获取分类ID
                category = db.query(Category).filter(
                    Category.field_tag == field_tag
                ).first()

                async with ListCrawler() as crawler:
                    journals = await crawler.crawl(field_tag, page)

                    for j_data in journals:
                        # 保存期刊（如果不存在）
                        journal = db.query(Journal).filter(
                            Journal.journal_id == j_data["journal_id"]
                        ).first()

                        if not journal:
                            journal = Journal(
                                journal_id=j_data["journal_id"],
                                name=j_data["name"],
                                category_id=category.id if category else None
                            )
                            db.add(journal)
                            db.commit()
                            db.refresh(journal)

                            # 创建详情任务
                            task_manager.create_detail_task(
                                j_data["journal_id"],
                                category.id if category else None
                            )

                task_manager.complete_task(task)

            except Exception as e:
                task_manager.fail_task(task, str(e))

    async def _process_detail_tasks(self, db: Session, task_manager: TaskManager):
        """处理详情任务"""
        tasks = task_manager.get_pending_tasks(TaskType.DETAIL.value, limit=3)

        for task in tasks:
            if not self._running or self._paused:
                break

            task_manager.start_task(task)

            try:
                journal_id = int(task.target_id)

                async with DetailCrawler() as crawler:
                    detail = await crawler.crawl(journal_id)

                    # 更新期刊信息
                    journal = db.query(Journal).filter(
                        Journal.journal_id == journal_id
                    ).first()

                    if journal:
                        basic_info = detail.get("basic_info", {})
                        journal.name = basic_info.get("name", journal.name)
                        journal.issn = basic_info.get("issn", journal.issn)
                        journal.eissn = basic_info.get("eissn")
                        journal.impact_factor = basic_info.get("impact_factor", journal.impact_factor)
                        journal.jcr_partition = basic_info.get("jcr_partition")
                        journal.cas_partition = basic_info.get("cas_partition")
                        journal.review_speed = basic_info.get("review_speed")
                        journal.acceptance_rate = basic_info.get("acceptance_rate")
                        journal.detail_data = basic_info
                        journal.detail_crawled = True

                        # 保存评论
                        for c_data in detail.get("comments", []):
                            comment_id = c_data.get("comment_id")
                            if not comment_id:
                                continue

                            existing = db.query(Comment).filter(
                                Comment.comment_id == comment_id
                            ).first()

                            if not existing:
                                comment = Comment(
                                    journal_id=journal.id,
                                    comment_id=comment_id,
                                    content=c_data.get("content"),
                                    author=c_data.get("author"),
                                    rating=c_data.get("rating")
                                )
                                db.add(comment)

                        journal.comments_crawled = True
                        db.commit()

                task_manager.complete_task(task)

            except Exception as e:
                task_manager.fail_task(task, str(e))

    async def _retry_failed_tasks(self, db: Session, task_manager: TaskManager):
        """重试失败任务"""
        failed_tasks = task_manager.get_failed_tasks(limit=3)
        for task in failed_tasks:
            task_manager.retry_task(task)

    async def start_full_crawl(self):
        """启动完整爬取"""
        db = SessionLocal()
        try:
            task_manager = TaskManager(db)
            task_manager.create_category_task()
            logger.info("已创建分类爬取任务，爬虫将自动开始")
        finally:
            db.close()


# 全局爬虫服务实例
crawler_service = CrawlerService()
