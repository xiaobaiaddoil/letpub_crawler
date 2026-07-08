#!/usr/bin/env python
"""
分布式爬虫Worker节点

用于在从服务器上运行，监听并执行爬虫任务。

使用方法:
    python worker.py [--worker-id WORKER_ID]

环境变量:
    DATABASE_URL: 数据库连接字符串（指向主数据库）
    WORKER_ID: Worker唯一标识（可选，默认自动生成）
    HEARTBEAT_INTERVAL: 心跳间隔（秒，默认30）
    PARALLEL_WORKERS: 消费者协程数量（默认3）
"""
import sys
import os
import argparse
import asyncio
import logging
import socket
import json
from datetime import datetime, timezone
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from app.config import config
from app.database import init_db, SessionLocal
from app.logging_config import setup_app_logging, clean_old_logs
from app.models.worker import Worker, WorkerStatus
from app.models.task import CrawlTask, TaskType, TaskStatus
from app.services.task_manager import TaskManager, generate_worker_id
from app.services.problem_service import ProblemService
from app.services.metric_service import MetricService
from app.services.index_service import IndexService
from app.crawler.category_crawler import CategoryCrawler
from app.crawler.list_crawler import ListCrawler
from app.crawler.detail_crawler import DetailCrawler

_CRAWLER_CLASS = {
    TaskType.CATEGORY.value: CategoryCrawler,
    TaskType.LIST.value: ListCrawler,
    TaskType.DETAIL.value: DetailCrawler,
}

# 初始化日志
setup_app_logging(debug=config.DEBUG, console_level=config.CONSOLE_LOG_LEVEL)
logger = logging.getLogger("worker")


def clean_numeric_value(value):
    """清洗数值字段，将无效值转换为 None

    处理常见的无效值如: "-", "N/A", "", None, 非数字字符串
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        value = value.strip()
        # 无效值列表
        if value in ["-", "N/A", "n/a", "", "--", "无", "暂无"]:
            return None
        # 尝试提取数字
        import re
        match = re.search(r'[\d.]+', value)
        if match:
            try:
                return float(match.group())
            except ValueError:
                return None
        return None
    return None


class DistributedWorker:
    """分布式爬虫Worker — 消费者协程池模式

    启动 PARALLEL_WORKERS 个持久协程，各自独立从任务池拉取任务，
    完成即拉下一个，无批次间隙。任务去重由 PostgreSQL FOR UPDATE SKIP LOCKED 保证。
    """

    DIRECT_FAIL_SLEEP_SECONDS = 20
    NO_TASK_SLEEP_SECONDS = 5

    def __init__(self, worker_id: str = None):
        self.worker_id = worker_id or generate_worker_id()
        self.hostname = socket.gethostname()
        self.ip_address = self._get_ip_address()
        self._running = False
        self._paused = False

        self.failed_html_dir = Path("logs/failed_html")
        self.failed_html_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Worker初始化: {self.worker_id}")
        logger.info(f"主机: {self.hostname}, IP: {self.ip_address}")

    def _get_ip_address(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _setup_problem_recorder(self):
        from app.crawler.detail_crawler import set_problem_recorder

        def recorder(journal_id, problem_type, problem_code, message, expected, actual):
            db = SessionLocal()
            try:
                service = ProblemService(db)
                service.record_problem(
                    journal_id=journal_id,
                    problem_type=problem_type,
                    problem_code=problem_code,
                    message=message,
                    expected_value=expected,
                    actual_value=actual
                )
            except Exception as e:
                logger.warning(f"记录问题失败: {e}")
            finally:
                db.close()

        set_problem_recorder(recorder)
        logger.info("问题记录器已设置")

    async def _save_failed_html(self, crawler, task_id: str, error: str):
        try:
            if crawler and crawler.page:
                html_content = await crawler.page.content()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = self.failed_html_dir / f"task_{task_id}_{timestamp}.html"
                error_comment = f"<!-- \nTask ID: {task_id}\nError: {error}\nTime: {timestamp}\n-->\n"
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(error_comment + html_content)
                logger.info(f"已保存失败页面HTML: {filename}")
        except Exception as e:
            logger.warning(f"保存失败HTML时出错: {e}")

    async def _handle_request_failure(self, crawler):
        if crawler and crawler.is_using_direct():
            logger.warning(f"[直连] 请求失败，休眠 {self.DIRECT_FAIL_SLEEP_SECONDS} 秒...")
            await asyncio.sleep(self.DIRECT_FAIL_SLEEP_SECONDS)
            logger.info("[直连] 休眠结束，继续工作")

    async def register(self):
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(Worker.worker_id == self.worker_id).first()
            now = datetime.now(timezone.utc)
            if worker:
                worker.hostname = self.hostname
                worker.ip_address = self.ip_address
                worker.status = WorkerStatus.ONLINE
                worker.last_heartbeat = now
                worker.started_at = now
                worker.current_task_count = 0
            else:
                worker = Worker(
                    worker_id=self.worker_id,
                    hostname=self.hostname,
                    ip_address=self.ip_address,
                    status=WorkerStatus.ONLINE,
                    registered_at=now,
                    last_heartbeat=now,
                    started_at=now,
                    version="1.0.0"
                )
                db.add(worker)
            db.commit()
            logger.info(f"Worker已注册: {self.worker_id}")
        except Exception as e:
            logger.error(f"Worker注册失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def heartbeat(self):
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(Worker.worker_id == self.worker_id).first()
            if worker:
                running_count = db.query(CrawlTask).filter(
                    CrawlTask.worker_id == self.worker_id,
                    CrawlTask.status == TaskStatus.RUNNING.value
                ).count()
                worker.last_heartbeat = datetime.now(timezone.utc)
                worker.current_task_count = running_count
                worker.status = WorkerStatus.BUSY if running_count > 0 else WorkerStatus.ONLINE
                db.commit()
        except Exception as e:
            logger.warning(f"心跳发送失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def unregister(self):
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(Worker.worker_id == self.worker_id).first()
            if worker:
                worker.status = WorkerStatus.OFFLINE
                worker.current_task_count = 0
                db.commit()
                logger.info(f"Worker已下线: {self.worker_id}")
        except Exception as e:
            logger.warning(f"Worker注销失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def increment_stats(self, completed: int = 0, failed: int = 0):
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(Worker.worker_id == self.worker_id).first()
            if worker:
                worker.total_completed += completed
                worker.total_failed += failed
                db.commit()
        except Exception as e:
            logger.warning(f"更新统计失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def _heartbeat_loop(self):
        while self._running:
            await self.heartbeat()
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)

    # ------------------------------------------------------------------ #
    # 任务获取                                                              #
    # ------------------------------------------------------------------ #

    async def _acquire_next_task(self):
        """按优先级尝试获取一个任务：CATEGORY → LIST → DETAIL。

        返回 (task, task_type) 或 (None, None)。
        使用独立 DB session，获取后立即关闭，执行阶段用新 session。
        """
        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            for task_type in [TaskType.CATEGORY.value, TaskType.LIST.value, TaskType.DETAIL.value]:
                tasks = tm.acquire_tasks(task_type, limit=1)
                if tasks:
                    return tasks[0], task_type
            # 无任务时顺便重置失败任务
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

    async def _fail_acquired_task(self, task, coroutine_id: int, error: Exception):
        """Mark an already-acquired task as failed when setup fails before task execution."""
        db = SessionLocal()
        try:
            error_msg = str(error) or type(error).__name__
            TaskManager(db, self.worker_id).fail_task(task, error_msg)
            await self.increment_stats(failed=1)
            logger.error(
                f"[消费者-{coroutine_id}] 任务准备失败 task={task.id}",
                exc_info=(type(error), error, error.__traceback__),
            )
        except Exception as fail_error:
            db.rollback()
            logger.error(f"[消费者-{coroutine_id}] 标记任务失败也失败 task={task.id}: {fail_error}")
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # 任务执行                                                              #
    # ------------------------------------------------------------------ #

    async def _execute_task(self, task, task_type: str, coroutine_id: int, crawler) -> bool:
        if task_type == TaskType.CATEGORY.value:
            return await self._execute_category_task(task, coroutine_id, crawler)
        elif task_type == TaskType.LIST.value:
            return await self._execute_list_task(task, coroutine_id, crawler)
        elif task_type == TaskType.DETAIL.value:
            return await self._execute_detail_task(task, coroutine_id, crawler)
        return False

    async def _execute_category_task(self, task, coroutine_id: int, crawler):
        from app.models.category import Category

        tag = f"[消费者-{coroutine_id}][分类]"
        logger.info(f"{tag} 开始 task={task.id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            index_service = IndexService(db)
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
            await crawler.report_cookie_result(success=True)
            await crawler.report_proxy_result(success=True)
            await self.increment_stats(completed=1)
            return True

        except Exception as e:
            db.rollback()
            error_msg = str(e)
            logger.exception(f"{tag} 失败 task={task.id}")
            await self._save_failed_html(crawler, "category", error_msg)
            TaskManager(db, self.worker_id).fail_task(task, error_msg)
            await crawler.report_cookie_result(success=False)
            await crawler.report_proxy_result(success=False)
            await self.increment_stats(failed=1)
            await self._handle_request_failure(crawler)
            return False
        finally:
            db.close()

    async def _execute_list_task(self, task, coroutine_id: int, crawler):
        from app.models.category import Category
        from app.models.journal import Journal

        extra = json.loads(task.extra_data) if task.extra_data else {}
        field_tag = extra.get("field_tag")
        page = extra.get("page", 1)
        tag = f"[消费者-{coroutine_id}][列表]"
        logger.info(f"{tag} 开始 task={task.id} field={field_tag} page={page}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            tm.renew_task_lock(task)

            category = db.query(Category).filter(Category.field_tag == field_tag).first()
            journals = await crawler.crawl(field_tag, page)
            new_count = 0
            updated_count = 0
            detail_task_count = 0
            new_index_count = 0

            if category:
                new_index_count = IndexService(db).record_list_page(category, page, journals)

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

            tm.complete_task(task)
            await crawler.report_cookie_result(success=True)
            await crawler.report_proxy_result(success=True)
            await self.increment_stats(completed=1)
            logger.info(
                f"{tag} 完成 task={task.id} field={field_tag} page={page} 期刊数={len(journals)} "
                f"新增={new_count} 更新={updated_count} 新索引={new_index_count} 详情任务={detail_task_count}"
            )
            return True

        except Exception as e:
            db.rollback()
            error_msg = str(e)
            logger.exception(f"{tag} 失败 task={task.id}")
            await self._save_failed_html(crawler, task.target_id, error_msg)
            TaskManager(db, self.worker_id).fail_task(task, error_msg)
            await crawler.report_cookie_result(success=False)
            await crawler.report_proxy_result(success=False)
            await self.increment_stats(failed=1)
            await self._handle_request_failure(crawler)
            return False
        finally:
            db.close()

    async def _execute_detail_task(self, task, coroutine_id: int, crawler):
        from app.models.journal import Journal
        from app.models.comment import Comment

        journal_id = int(task.target_id)
        tag = f"[消费者-{coroutine_id}][详情]"
        logger.info(f"{tag} 开始 task={task.id} journal_id={journal_id}")

        db = SessionLocal()
        try:
            tm = TaskManager(db, self.worker_id)
            tm.renew_task_lock(task)

            detail = await crawler.crawl(journal_id)

            journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
            if journal:
                basic_info = detail.get("basic_info", {})
                journal.issn = basic_info.get("issn", journal.issn)
                journal.eissn = basic_info.get("E-ISSN")
                journal.impact_factor = clean_numeric_value(basic_info.get("impact_factor")) or journal.impact_factor
                journal.impact_factor_realtime = clean_numeric_value(basic_info.get("impact_factor_realtime"))
                journal.self_citation_rate = basic_info.get("self_citation_rate")
                journal.jcr_partition = basic_info.get("jcr_partition")
                journal.cas_partition = basic_info.get("cas_partition")
                journal.cas_warning = basic_info.get("cas_warning")
                journal.citescore = basic_info.get("citescore")
                journal.review_speed = basic_info.get("review_speed")
                journal.acceptance_rate = basic_info.get("acceptance_rate")
                journal.detail_data = basic_info
                journal.detail_crawled = True
                db.commit()

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
                    db.commit()

                journal.comments_crawled = True
                MetricService(db).record_snapshot(journal, basic_info, task_id=task.id)
                db.commit()

            tm.complete_task(task)
            await crawler.report_cookie_result(success=True)
            await crawler.report_proxy_result(success=True)
            await self.increment_stats(completed=1)
            logger.info(f"{tag} 完成 task={task.id} journal_id={journal_id}")
            return True

        except Exception as e:
            db.rollback()
            error_msg = str(e)
            logger.exception(f"{tag} 失败 task={task.id} journal_id={journal_id}")
            await self._save_failed_html(crawler, task.target_id, error_msg)
            TaskManager(db, self.worker_id).fail_task(task, error_msg)
            await crawler.report_cookie_result(success=False)
            await crawler.report_proxy_result(success=False)
            await self.increment_stats(failed=1)
            await self._handle_request_failure(crawler)
            return False
        finally:
            db.close()

    # ------------------------------------------------------------------ #
    # 消费者主循环                                                          #
    # ------------------------------------------------------------------ #

    async def _consumer_loop(self, coroutine_id: int):
        """持久消费者协程：独立拉取任务，完成即拉下一个。

        HTTP 模式复用 httpx client/session；browser 模式复用 browser，只换 context。
        """
        logger.info(f"[消费者-{coroutine_id}] 启动")
        crawler = None
        current_crawler_type = None
        fetch_mode = config.CRAWLER_FETCH_MODE

        while self._running:
            if self._paused:
                await asyncio.sleep(1)
                continue

            task, task_type = await self._acquire_next_task()

            if task is None:
                logger.debug(f"[消费者-{coroutine_id}] 无任务，等待 {self.NO_TASK_SLEEP_SECONDS}s")
                await asyncio.sleep(self.NO_TASK_SLEEP_SECONDS)
                continue

            use_cookie = (task_type == TaskType.DETAIL.value)

            try:
                CrawlerClass = _CRAWLER_CLASS[task_type]
                if crawler is not None and current_crawler_type != task_type:
                    await crawler.close()
                    crawler = None

                if crawler is None:
                    crawler = CrawlerClass()
                    if fetch_mode == "browser":
                        await crawler.init_browser(use_proxy=True, use_cookie=use_cookie)
                        logger.info(f"[消费者-{coroutine_id}] 新建 browser type={task_type}")
                    else:
                        await crawler.init_http(use_proxy=True)
                        logger.info(f"[消费者-{coroutine_id}] 新建 http client type={task_type}")
                    current_crawler_type = task_type
                else:
                    if fetch_mode == "browser":
                        try:
                            await crawler.reset_context(use_cookie=use_cookie)
                            logger.debug(f"[消费者-{coroutine_id}] 复用 browser，重置 context")
                        except Exception as e:
                            logger.warning(f"[消费者-{coroutine_id}] reset_context 失败({e})，重建 browser")
                            await crawler.close()
                            crawler = CrawlerClass()
                            await crawler.init_browser(use_proxy=True, use_cookie=use_cookie)
                            current_crawler_type = task_type
                    else:
                        logger.debug(f"[消费者-{coroutine_id}] 复用 http client")
            except Exception as e:
                await self._fail_acquired_task(task, coroutine_id, e)
                if crawler:
                    await crawler.close()
                    crawler = None
                current_crawler_type = None
                continue

            task_success = await self._execute_task(task, task_type, coroutine_id, crawler)
            if not task_success or (fetch_mode == "browser" and crawler.is_using_direct()):
                await crawler.close()
                crawler = None
                current_crawler_type = None

        if crawler:
            await crawler.close()
        logger.info(f"[消费者-{coroutine_id}] 退出")

    # ------------------------------------------------------------------ #
    # 主入口                                                               #
    # ------------------------------------------------------------------ #

    async def run(self):
        self._running = True

        logger.info("初始化数据库连接...")
        try:
            init_db()
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            return

        self._setup_problem_recorder()
        await self.register()

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        n = config.PARALLEL_WORKERS
        logger.info(f"Worker开始运行: {self.worker_id}，消费者协程数: {n}")

        consumer_tasks = [asyncio.create_task(self._consumer_loop(i)) for i in range(n)]

        try:
            await asyncio.gather(*consumer_tasks)
        except asyncio.CancelledError:
            logger.info("Worker被取消")
            for t in consumer_tasks:
                t.cancel()
        finally:
            self._running = False
            heartbeat_task.cancel()
            await self.unregister()
            logger.info("Worker已停止")

    def stop(self):
        self._running = False
        logger.info("收到停止信号")

    def pause(self):
        self._paused = True
        logger.info("Worker已暂停")

    def resume(self):
        self._paused = False
        logger.info("Worker已恢复")


async def main():
    parser = argparse.ArgumentParser(description="分布式爬虫Worker")
    parser.add_argument(
        "--worker-id",
        type=str,
        default=config.WORKER_ID,
        help="Worker唯一标识（默认自动生成）"
    )
    args = parser.parse_args()

    clean_old_logs(days=7)

    worker = DistributedWorker(worker_id=args.worker_id if args.worker_id else None)

    main_task = asyncio.create_task(worker.run())

    import signal
    import sys

    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，正在停止...")
        worker.stop()
        signal.signal(signal.SIGINT, lambda _, __: sys.exit(1))
        signal.signal(signal.SIGTERM, lambda _, __: sys.exit(1))

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await main_task
    except asyncio.CancelledError:
        logger.info("主任务被取消")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序被用户中断")
    except SystemExit:
        pass
