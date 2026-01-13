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
    BATCH_SIZE: 每次获取任务数量（默认5）
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
    """分布式爬虫Worker"""

    # 失败休眠配置
    FAILURE_WINDOW_SECONDS = 60  # 统计失败的时间窗口（秒）
    MAX_FAILURES_BEFORE_SLEEP = 5  # 触发休眠的失败次数
    FAILURE_SLEEP_SECONDS = 120  # 休眠时长（秒）

    def __init__(self, worker_id: str = None):
        self.worker_id = worker_id or generate_worker_id()
        self.hostname = socket.gethostname()
        self.ip_address = self._get_ip_address()
        self._running = False
        self._paused = False
        self._recent_failures = []  # 记录最近失败的时间戳

        # 确保失败HTML存储目录存在
        self.failed_html_dir = Path("logs/failed_html")
        self.failed_html_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"Worker初始化: {self.worker_id}")
        logger.info(f"主机: {self.hostname}, IP: {self.ip_address}")

    def _get_ip_address(self) -> str:
        """获取本机IP地址"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    async def _save_failed_html(self, crawler, task_id: str, error: str):
        """保存失败任务的HTML内容"""
        try:
            if crawler and crawler.page:
                html_content = await crawler.page.content()
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = self.failed_html_dir / f"task_{task_id}_{timestamp}.html"
                
                # 在HTML头部添加错误信息注释
                error_comment = f"<!-- \nTask ID: {task_id}\nError: {error}\nTime: {timestamp}\n-->\n"
                
                with open(filename, "w", encoding="utf-8") as f:
                    f.write(error_comment + html_content)
                
                logger.info(f"已保存失败页面HTML: {filename}")
        except Exception as e:
            logger.warning(f"保存失败HTML时出错: {e}")

    def _record_failure(self):
        """记录一次失败，返回是否需要休眠"""
        now = datetime.now().timestamp()
        
        # 清理过期的失败记录
        cutoff = now - self.FAILURE_WINDOW_SECONDS
        self._recent_failures = [t for t in self._recent_failures if t > cutoff]
        
        # 记录本次失败
        self._recent_failures.append(now)
        
        # 检查是否需要休眠
        if len(self._recent_failures) >= self.MAX_FAILURES_BEFORE_SLEEP:
            logger.warning(
                f"短时间内失败 {len(self._recent_failures)} 次，"
                f"将休眠 {self.FAILURE_SLEEP_SECONDS} 秒"
            )
            self._recent_failures.clear()  # 清空记录，休眠后重新计数
            return True
        return False

    async def _sleep_on_failures(self):
        """失败过多时休眠"""
        logger.info(f"开始休眠 {self.FAILURE_SLEEP_SECONDS} 秒...")
        await asyncio.sleep(self.FAILURE_SLEEP_SECONDS)
        logger.info("休眠结束，继续工作")

    async def register(self):
        """向数据库注册Worker"""
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(
                Worker.worker_id == self.worker_id
            ).first()

            now = datetime.now(timezone.utc)

            if worker:
                # 更新已存在的Worker
                worker.hostname = self.hostname
                worker.ip_address = self.ip_address
                worker.status = WorkerStatus.ONLINE
                worker.last_heartbeat = now
                worker.started_at = now
                worker.current_task_count = 0
            else:
                # 创建新Worker
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
        """发送心跳"""
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(
                Worker.worker_id == self.worker_id
            ).first()

            if worker:
                worker.last_heartbeat = datetime.now(timezone.utc)
                worker.status = WorkerStatus.ONLINE if worker.current_task_count == 0 else WorkerStatus.BUSY
                db.commit()
        except Exception as e:
            logger.warning(f"心跳发送失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def unregister(self):
        """注销Worker（下线）"""
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(
                Worker.worker_id == self.worker_id
            ).first()

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

    async def update_task_count(self, count: int):
        """更新当前任务数"""
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(
                Worker.worker_id == self.worker_id
            ).first()

            if worker:
                worker.current_task_count = count
                worker.status = WorkerStatus.BUSY if count > 0 else WorkerStatus.ONLINE
                db.commit()
        except Exception as e:
            logger.warning(f"更新任务数失败: {e}")
            db.rollback()
        finally:
            db.close()

    async def increment_stats(self, completed: int = 0, failed: int = 0):
        """增加统计数据"""
        db = SessionLocal()
        try:
            worker = db.query(Worker).filter(
                Worker.worker_id == self.worker_id
            ).first()

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
        """心跳循环"""
        while self._running:
            await self.heartbeat()
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)

    async def _process_category_tasks(self, db, task_manager: TaskManager):
        """处理分类任务"""
        import math
        from app.crawler.category_crawler import CategoryCrawler
        from app.models.category import Category

        JOURNALS_PER_PAGE = 10

        tasks = task_manager.acquire_tasks(TaskType.CATEGORY.value, limit=1)
        if not tasks:
            return

        task = tasks[0]
        await self.update_task_count(1)

        crawler = None
        try:
            crawler = CategoryCrawler()
            await crawler.init_browser()

            categories = await crawler.crawl()

            new_count = 0
            updated_count = 0

            for cat_data in categories:
                field_tag = cat_data["field_tag"]
                new_total = cat_data.get("total_count", 0)

                category = db.query(Category).filter(
                    Category.field_tag == field_tag
                ).first()

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
                        task_manager.create_list_tasks(field_tag, total_pages)
                        logger.info(f"新分类 {cat_data['name']}: {new_total} 期刊, {total_pages} 页")

                elif category.total_count != new_total:
                    old_total = category.total_count
                    category.name = cat_data["name"]
                    category.total_count = new_total
                    db.commit()
                    updated_count += 1

                    old_pages = math.ceil(old_total / JOURNALS_PER_PAGE) if old_total > 0 else 0
                    new_pages = math.ceil(new_total / JOURNALS_PER_PAGE) if new_total > 0 else 0

                    if new_pages > old_pages:
                        task_manager.create_list_tasks(field_tag, new_pages)
                        logger.info(f"分类更新 {cat_data['name']}: {old_total}->{new_total}")

            logger.info(f"分类任务完成: 新增 {new_count}, 更新 {updated_count}")
            task_manager.complete_task(task)
            # 报告 Cookie 使用成功
            await crawler.report_cookie_result(success=True)
            await self.increment_stats(completed=1)

        except Exception as e:
            db.rollback()
            error_msg = str(e)
            logger.exception("处理分类任务失败")
            
            # 保存失败页面的HTML
            await self._save_failed_html(crawler, "category", error_msg)
            
            task_manager.fail_task(task, error_msg)
            # 报告 Cookie 使用失败
            if crawler:
                await crawler.report_cookie_result(success=False)
            await self.increment_stats(failed=1)
            
            # 检查是否需要休眠
            if self._record_failure():
                await self._sleep_on_failures()
        finally:
            if crawler:
                await crawler.close()
            await self.update_task_count(0)

    async def _process_list_tasks(self, db, task_manager: TaskManager):
        """处理列表任务"""
        from app.crawler.list_crawler import ListCrawler
        from app.models.category import Category
        from app.models.journal import Journal

        tasks = task_manager.acquire_tasks(TaskType.LIST.value, limit=config.BATCH_SIZE)
        if not tasks:
            return

        await self.update_task_count(len(tasks))
        completed = 0
        failed = 0

        # 浏览器实例移到循环外部，批量任务复用
        crawler = None
        try:
            crawler = ListCrawler()
            await crawler.init_browser()

            for task in tasks:
                if not self._running or self._paused:
                    break

                try:
                    extra = json.loads(task.extra_data) if task.extra_data else {}
                    field_tag = extra.get("field_tag")
                    page = extra.get("page", 1)

                    # 续期任务锁定时间
                    task_manager.renew_task_lock(task)

                    category = db.query(Category).filter(
                        Category.field_tag == field_tag
                    ).first()

                    # 确保浏览器可用
                    await crawler.ensure_browser()

                    journals = await crawler.crawl(field_tag, page)

                    for j_data in journals:
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

                            task_manager.create_detail_task(
                                j_data["journal_id"],
                                category.id if category else None
                            )

                    task_manager.complete_task(task)
                    # 报告 Cookie 使用成功
                    await crawler.report_cookie_result(success=True)
                    completed += 1

                except Exception as e:
                    db.rollback()
                    error_msg = str(e)
                    logger.exception(f"处理列表任务失败: {task.target_id}")
                    
                    # 保存失败页面的HTML
                    await self._save_failed_html(crawler, task.target_id, error_msg)
                    
                    task_manager.fail_task(task, error_msg)
                    # 报告 Cookie 使用失败
                    if crawler:
                        await crawler.report_cookie_result(success=False)
                    failed += 1
                    
                    # 检查是否需要休眠
                    if self._record_failure():
                        await self._sleep_on_failures()

        finally:
            if crawler:
                await crawler.close()

        await self.increment_stats(completed=completed, failed=failed)
        await self.update_task_count(0)

    async def _process_detail_tasks(self, db, task_manager: TaskManager):
        """处理详情任务"""
        from app.crawler.detail_crawler import DetailCrawler
        from app.models.journal import Journal
        from app.models.comment import Comment

        tasks = task_manager.acquire_tasks(TaskType.DETAIL.value, limit=config.BATCH_SIZE)
        if not tasks:
            return

        await self.update_task_count(len(tasks))
        completed = 0
        failed = 0

        # 浏览器实例移到循环外部，批量任务复用
        crawler = None
        try:
            crawler = DetailCrawler()
            await crawler.init_browser()

            for task in tasks:
                if not self._running or self._paused:
                    break

                try:
                    journal_id = int(task.target_id)

                    # 续期任务锁定时间，防止长时间任务被误判为超时
                    task_manager.renew_task_lock(task)

                    # 确保浏览器可用
                    await crawler.ensure_browser()

                    detail = await crawler.crawl(journal_id)

                    journal = db.query(Journal).filter(
                        Journal.journal_id == journal_id
                    ).first()

                    if journal:
                        basic_info = detail.get("basic_info", {})
                        journal.issn = basic_info.get("issn", journal.issn)
                        journal.eissn = basic_info.get("E-ISSN")

                        # 数值字段需要清洗，防止无效值（如"-"）导致数据库错误
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
                        db.commit()  # 先提交期刊信息

                        # 单独处理评论，先去重避免同批次重复
                        seen_comment_ids = set()
                        for c_data in detail.get("comments", []):
                            comment_id = c_data.get("comment_id")
                            if not comment_id or comment_id in seen_comment_ids:
                                continue
                            seen_comment_ids.add(comment_id)

                            try:
                                # 使用 merge 避免重复插入问题
                                from sqlalchemy.dialects.postgresql import insert
                                stmt = insert(Comment).values(
                                    journal_id=journal.journal_id,
                                    comment_id=comment_id,
                                    content=c_data.get("content"),
                                    author=c_data.get("author"),
                                    rating=c_data.get("rating"),
                                    submit_experience=c_data.get("submit_experience"),
                                    comment_time=c_data.get("comment_time")
                                ).on_conflict_do_nothing(index_elements=['comment_id'])
                                db.execute(stmt)
                                db.commit()
                            except Exception:
                                db.rollback()

                        journal.comments_crawled = True
                        db.commit()

                    task_manager.complete_task(task)
                    # 报告 Cookie 使用成功
                    await crawler.report_cookie_result(success=True)
                    completed += 1

                except Exception as e:
                    db.rollback()
                    error_msg = str(e)
                    logger.exception(f"处理详情任务失败: {task.target_id}")
                    
                    # 保存失败页面的HTML
                    await self._save_failed_html(crawler, task.target_id, error_msg)
                    
                    task_manager.fail_task(task, error_msg)
                    # 报告 Cookie 使用失败
                    if crawler:
                        await crawler.report_cookie_result(success=False)
                    failed += 1
                    
                    # 检查是否需要休眠
                    if self._record_failure():
                        await self._sleep_on_failures()

        finally:
            if crawler:
                await crawler.close()

        await self.increment_stats(completed=completed, failed=failed)
        await self.update_task_count(0)

    async def _retry_failed_tasks(self, db, task_manager: TaskManager):
        """重试失败任务"""
        failed_tasks = task_manager.get_failed_tasks(limit=3)
        for task in failed_tasks:
            task_manager.retry_task(task)

    async def run(self):
        """运行Worker主循环"""
        self._running = True

        # 初始化数据库
        logger.info("初始化数据库连接...")
        try:
            init_db()
        except Exception as e:
            logger.error(f"数据库连接失败: {e}")
            return

        # 注册Worker
        await self.register()

        # 启动心跳任务
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        logger.info(f"Worker开始运行: {self.worker_id}")
        logger.info("等待任务...")

        try:
            while self._running:
                if self._paused:
                    await asyncio.sleep(1)
                    continue

                db = SessionLocal()
                try:
                    task_manager = TaskManager(db, self.worker_id)

                    # 按优先级处理任务
                    await self._process_category_tasks(db, task_manager)
                    await self._process_list_tasks(db, task_manager)
                    await self._process_detail_tasks(db, task_manager)
                    await self._retry_failed_tasks(db, task_manager)

                    # 检查是否有待处理任务
                    pending_count = len(task_manager.get_pending_tasks(limit=1))
                    if pending_count == 0:
                        logger.debug("无待处理任务，等待...")
                        await asyncio.sleep(10)
                    else:
                        await asyncio.sleep(1)

                except Exception as e:
                    logger.error(f"Worker循环错误: {e}")
                    await asyncio.sleep(5)
                finally:
                    db.close()

        except asyncio.CancelledError:
            logger.info("Worker被取消")
        finally:
            self._running = False
            heartbeat_task.cancel()
            await self.unregister()
            logger.info("Worker已停止")

    def stop(self):
        """停止Worker"""
        self._running = False
        logger.info("收到停止信号")

    def pause(self):
        """暂停Worker"""
        self._paused = True
        logger.info("Worker已暂停")

    def resume(self):
        """恢复Worker"""
        self._paused = False
        logger.info("Worker已恢复")


async def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="分布式爬虫Worker")
    parser.add_argument(
        "--worker-id",
        type=str,
        default=config.WORKER_ID,
        help="Worker唯一标识（默认自动生成）"
    )
    args = parser.parse_args()

    # 清理旧日志
    clean_old_logs(days=7)

    worker = DistributedWorker(worker_id=args.worker_id if args.worker_id else None)

    # 创建主任务
    main_task = asyncio.create_task(worker.run())

    # 处理信号
    import signal
    import sys

    def signal_handler(signum, frame):
        logger.info(f"收到信号 {signum}，正在停止...")
        worker.stop()
        # 给一些时间让当前操作完成
        # 如果再次收到信号则强制退出
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
