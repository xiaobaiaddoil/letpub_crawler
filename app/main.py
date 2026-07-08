import asyncio
import logging
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from pathlib import Path

from app.config import config
from app.database import init_db, get_db, SessionLocal
from app.api import tasks, data, workers, cookies, accounts, proxies, problems
from app.services.crawler_service import crawler_service
from app.services.task_manager import TaskManager
from app.services.worker_status import is_worker_online, to_db_naive
from app.logging_config import setup_app_logging, clean_old_logs

# 初始化日志系统
setup_app_logging(debug=config.DEBUG, console_level=config.CONSOLE_LOG_LEVEL)
logger = logging.getLogger(__name__)

# 清理超过7天的旧日志文件
clean_old_logs(days=7)

# 模板目录
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
static_dir = Path(__file__).parent / "static"


def utc_to_local(utc_dt):
    """模板时间显示：naive DB timestamp 按本地时间处理。"""
    if utc_dt is None:
        return None
    return to_db_naive(utc_dt)


templates.env.filters["localtime"] = utc_to_local

# 后台任务
crawler_task = None
proxy_refresh_task = None

# 是否自动启动爬虫（调试时设为false）
CRAWLER_AUTO_START = config.CRAWLER_AUTO_START

# 运行模式
RUN_MODE = config.RUN_MODE  # master / worker / standalone


async def proxy_auto_refresh_loop():
    """代理自动刷新后台任务 - 直接从 proxy.yaml 配置获取代理"""
    from app.services.proxy_service import ProxyService
    
    # 获取刷新间隔配置
    proxy_cfg = config.proxy_config or {}
    auto_refresh_cfg = proxy_cfg.get("auto_refresh", {}) or {}
    refresh_enabled = auto_refresh_cfg.get("enabled", False)
    refresh_interval = auto_refresh_cfg.get("interval", 300)  # 默认5分钟
    
    if not refresh_enabled:
        logger.info("[代理] 自动刷新未启用")
        # 仅首次初始化，不循环刷新
        if proxy_cfg:
            try:
                db = SessionLocal()
                try:
                    service = ProxyService(db)
                    result = await service.fetch_proxies_from_config()
                    if result["tunnel"] > 0 or result["private"] > 0:
                        logger.info(f"[代理] 初始化完成: 隧道 {result['tunnel']} 个, 私密 {result['private']} 个")
                finally:
                    db.close()
            except Exception as e:
                logger.error(f"[代理] 初始化失败: {e}")
        return
    
    logger.info(f"[代理] 自动刷新已启用，间隔 {refresh_interval} 秒")
    
    # 首次立即获取
    try:
        db = SessionLocal()
        try:
            service = ProxyService(db)
            result = await service.fetch_proxies_from_config()
            if result["tunnel"] > 0 or result["private"] > 0:
                logger.info(f"[代理] 初始化完成: 隧道 {result['tunnel']} 个, 私密 {result['private']} 个")
        finally:
            db.close()
    except Exception as e:
        logger.error(f"[代理] 初始化失败: {e}")
    
    # 定期刷新循环
    while True:
        try:
            await asyncio.sleep(refresh_interval)
            
            # 重新读取配置（支持热更新）
            config.reload()
            
            db = SessionLocal()
            try:
                service = ProxyService(db)
                result = await service.fetch_proxies_from_config()
                if result["tunnel"] > 0 or result["private"] > 0:
                    logger.info(f"[代理] 自动刷新: 隧道 {result['tunnel']} 个, 私密 {result['private']} 个")
            finally:
                db.close()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[代理] 自动刷新失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global crawler_task, proxy_refresh_task

    # 启动时初始化数据库
    logger.info("初始化数据库...")
    logger.info(f"运行模式: {RUN_MODE}")
    try:
        init_db()
        logger.info("数据库初始化成功")
    except Exception as e:
        logger.error(f"数据库初始化失败: {e}")
        logger.error("请检查 DATABASE_URL 配置和数据库连接")
        raise

    # 启动代理自动刷新任务（所有模式都启动）
    proxy_refresh_task = asyncio.create_task(proxy_auto_refresh_loop())
    logger.info("代理自动刷新任务已启动")

    # 根据运行模式决定是否启动爬虫
    if RUN_MODE == "master":
        # 主服务器模式：不运行爬虫，只提供管理界面
        logger.info("主服务器模式：爬虫由Worker节点执行")
        # 重置所有超时任务
        try:
            db = SessionLocal()
            task_manager = TaskManager(db)
            # 不重置所有任务，只释放超时任务
            # task_manager.reset_running_tasks()
            db.close()
        except Exception as e:
            logger.warning(f"任务检查失败: {e}")

    elif RUN_MODE == "worker":
        # Worker模式：作为Web服务运行的Worker（不推荐，建议使用worker.py）
        logger.warning("Worker模式请使用 worker.py 脚本运行")

    else:
        # 单机模式（standalone）：保持原有行为
        logger.info("单机模式：本地运行爬虫")
        try:
            db = SessionLocal()
            task_manager = TaskManager(db)
            task_manager.reset_running_tasks()
            db.close()
        except Exception as e:
            logger.warning(f"重置任务失败: {e}")

        if CRAWLER_AUTO_START:
            crawler_task = asyncio.create_task(crawler_service.run())
            logger.info("爬虫服务已启动")
        else:
            logger.info("调试模式：爬虫服务未自动启动，请通过API手动启动")

    yield

    # 关闭代理自动刷新任务
    if proxy_refresh_task:
        proxy_refresh_task.cancel()
        try:
            await proxy_refresh_task
        except asyncio.CancelledError:
            pass

    # 关闭时停止爬虫
    if RUN_MODE != "master":
        crawler_service.stop()
        if crawler_task:
            crawler_task.cancel()
            try:
                await crawler_task
            except asyncio.CancelledError:
                pass
    logger.info("应用已关闭")


app = FastAPI(
    title="LetPub期刊爬虫系统",
    description="分布式爬取LetPub网站的期刊数据",
    version="2.0.0",
    lifespan=lifespan
)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# 注册API路由
app.include_router(tasks.router)
app.include_router(data.router)
app.include_router(workers.router)
app.include_router(cookies.router)
app.include_router(accounts.router)
app.include_router(proxies.router)
app.include_router(problems.router)

# 爬虫控制API
@app.post("/api/crawler/start")
async def start_crawler():
    """启动/恢复已有任务消费，不创建新的爬取任务."""
    global crawler_task

    # 主服务器模式下不启动本地爬虫
    if RUN_MODE == "master":
        return {
            "message": "已通知Worker处理现有任务，请确保Worker节点在线",
            "mode": "master",
            "status": "waiting_workers"
        }

    # 单机模式下启动本地爬虫
    if not crawler_service.is_running:
        crawler_task = asyncio.create_task(crawler_service.run())
        logger.info("爬虫服务已启动")

    crawler_service.resume()
    return {"message": "已启动任务消费", "mode": RUN_MODE, "status": "running"}


@app.post("/api/crawler/pause")
def pause_crawler():
    """暂停爬虫（仅单机模式有效）"""
    if RUN_MODE == "master":
        return {"message": "主服务器模式下无法暂停，请在Worker节点操作", "status": "not_applicable"}
    crawler_service.pause()
    return {"message": "爬虫已暂停", "status": "paused"}


@app.post("/api/crawler/resume")
async def resume_crawler():
    """恢复爬虫（仅单机模式有效）"""
    global crawler_task

    if RUN_MODE == "master":
        return {"message": "主服务器模式下无法恢复，请在Worker节点操作", "status": "not_applicable"}

    # 如果爬虫服务未运行，先启动它
    if not crawler_service.is_running:
        crawler_task = asyncio.create_task(crawler_service.run())
        logger.info("爬虫服务已启动")
        return {"message": "爬虫服务已启动", "status": "running"}

    # 如果已运行但暂停，则恢复
    crawler_service.resume()
    return {"message": "爬虫已恢复", "status": "running"}


@app.get("/api/crawler/status")
def crawler_status(db: Session = Depends(get_db)):
    """获取爬虫状态"""
    from app.models.worker import Worker

    if RUN_MODE == "master":
        # 主服务器模式：返回Worker状态
        workers_list = db.query(Worker).all()
        online_workers = sum(1 for worker in workers_list if is_worker_online(worker))

        return {
            "mode": "master",
            "running": online_workers > 0,
            "online_workers": online_workers,
            "status": "workers_active" if online_workers > 0 else "no_workers"
        }

    return {
        "mode": RUN_MODE,
        "running": crawler_service.is_running,
        "paused": crawler_service.is_paused,
        "status": "paused" if crawler_service.is_paused else ("running" if crawler_service.is_running else "stopped")
    }

@app.post("/api/config/cookie")
async def update_cookie(request: Request):
    """更新Cookie配置"""
    body = await request.json()
    cookie = body.get("cookie", "")
    config.LETPUB_COOKIE = cookie
    return {"message": "Cookie已更新"}

# 前端页面路由
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db)):
    """控制面板页面"""
    from app.models.category import Category
    from app.models.journal import Journal
    from app.models.comment import Comment
    from app.models.worker import Worker

    task_manager = TaskManager(db)
    task_stats = task_manager.get_stats()

    data_stats = {
        "categories": db.query(Category).count(),
        "journals": db.query(Journal).count(),
        "journals_with_detail": db.query(Journal).filter(Journal.detail_crawled == True).count(),
        "comments": db.query(Comment).count()
    }

    workers_list = db.query(Worker).all()
    online_workers = sum(1 for worker in workers_list if is_worker_online(worker))
    total_workers = len(workers_list)

    worker_stats = {
        "online": online_workers,
        "total": total_workers
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "task_stats": task_stats,
        "data_stats": data_stats,
        "worker_stats": worker_stats,
        "run_mode": RUN_MODE,
        "crawler_status": {
            "running": crawler_service.is_running if RUN_MODE != "master" else online_workers > 0,
            "paused": crawler_service.is_paused if RUN_MODE != "master" else False
        }
    })

@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    """任务列表页面"""
    return templates.TemplateResponse("tasks.html", {
        "request": request
    })

@app.get("/journals", response_class=HTMLResponse)
async def journals_page(request: Request, db: Session = Depends(get_db)):
    """期刊列表页面"""
    from app.models.journal import Journal
    from app.models.category import Category

    categories = db.query(Category).order_by(Category.name).all()
    journals = db.query(Journal).order_by(Journal.impact_factor.desc().nullslast()).limit(50).all()

    return templates.TemplateResponse("journals.html", {
        "request": request,
        "categories": categories,
        "journals": journals
    })


@app.get("/workers", response_class=HTMLResponse)
async def workers_page(request: Request, db: Session = Depends(get_db)):
    """Worker管理页面"""
    from app.models.worker import Worker
    from app.models.task import CrawlTask, TaskStatus

    workers_list = db.query(Worker).order_by(Worker.last_heartbeat.desc().nullslast()).all()

    # 为每个worker添加在线状态
    workers_data = []
    for w in workers_list:
        online = is_worker_online(w)
        running_tasks = db.query(CrawlTask).filter(
            CrawlTask.worker_id == w.worker_id,
            CrawlTask.status == TaskStatus.RUNNING.value
        ).count()

        workers_data.append({
            "worker": w,
            "is_online": online,
            "running_tasks": running_tasks
        })

    return templates.TemplateResponse("workers.html", {
        "request": request,
        "workers": workers_data,
        "run_mode": RUN_MODE
    })


@app.get("/cookies", response_class=HTMLResponse)
async def cookies_page(request: Request, db: Session = Depends(get_db)):
    """Cookie池管理页面"""
    from app.models.cookie_pool import CookiePool
    from app.models.account import Account

    cookies_list = db.query(CookiePool).order_by(CookiePool.created_at.desc()).all()
    active_count = db.query(CookiePool).filter(CookiePool.is_active == True).count()

    # 获取账号列表
    accounts_list = db.query(Account).order_by(Account.created_at.desc()).all()

    return templates.TemplateResponse("cookies.html", {
        "request": request,
        "cookies": cookies_list,
        "total": len(cookies_list),
        "active_count": active_count,
        "accounts": accounts_list
    })


@app.get("/proxies", response_class=HTMLResponse)
async def proxies_page(request: Request):
    """代理池管理页面"""
    return templates.TemplateResponse("proxies.html", {"request": request})


@app.get("/journals/{journal_id}/comments", response_class=HTMLResponse)
async def journal_comments_page(request: Request, journal_id: int, db: Session = Depends(get_db)):
    """期刊评论页面"""
    from app.models.journal import Journal
    
    journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
    if not journal:
        return templates.TemplateResponse("comments.html", {
            "request": request,
            "journal_id": journal_id,
            "error": "期刊不存在"
        })
    
    return templates.TemplateResponse("comments.html", {
        "request": request,
        "journal_id": journal_id
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config.HOST, port=config.PORT)
