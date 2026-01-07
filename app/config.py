import os
from pathlib import Path
from dotenv import load_dotenv

# 加载.env文件
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

class Config:
    # 应用配置
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"
    CONSOLE_LOG_LEVEL: str = os.getenv("CONSOLE_LOG_LEVEL", "WARNING").upper()

    # 运行模式: master(主服务器-管理界面) / worker(从服务器-爬虫) / standalone(单机模式)
    RUN_MODE: str = os.getenv("RUN_MODE", "standalone").lower()

    # 数据库配置
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql://myuser:123456@192.168.0.110:5432/letpub_crawler"
    )

    # 爬虫配置
    CRAWL_DELAY_MIN: int = int(os.getenv("CRAWL_DELAY_MIN", "3"))
    CRAWL_DELAY_MAX: int = int(os.getenv("CRAWL_DELAY_MAX", "8"))
    MAX_RETRY: int = int(os.getenv("MAX_RETRY", "3"))
    BATCH_SIZE: int = int(os.getenv("BATCH_SIZE", "5"))  # 每次获取任务数量

    # 分布式配置
    WORKER_ID: str = os.getenv("WORKER_ID", "")  # 可手动指定worker标识，为空则自动生成
    TASK_LOCK_TIMEOUT: int = int(os.getenv("TASK_LOCK_TIMEOUT", "300"))  # 任务锁定超时时间（秒）
    HEARTBEAT_INTERVAL: int = int(os.getenv("HEARTBEAT_INTERVAL", "30"))  # 心跳间隔（秒）
    WORKER_TIMEOUT: int = int(os.getenv("WORKER_TIMEOUT", "120"))  # Worker超时时间（秒）

    # Cookie配置
    LETPUB_COOKIE: str = os.getenv("LETPUB_COOKIE", "")

    # 服务器配置
    HOST: str = os.getenv("HOST", "0.0.0.0")
    PORT: int = int(os.getenv("PORT", "8000"))

    # 目标网站URL
    BASE_URL: str = "https://letpub.com.cn"
    ENTRY_URL: str = f"{BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag=all&firstletter="

    # User-Agent列表
    USER_AGENTS: list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    ]

config = Config()
