import os
from pathlib import Path
from typing import Any
import yaml

CONFIG_DIR = Path(__file__).parent.parent / "config"


def load_yaml_config(filename: str) -> dict:
    """加载 YAML 配置文件"""
    config_path = CONFIG_DIR / filename
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


def get_nested(data: dict, *keys, default: Any = None) -> Any:
    """获取嵌套字典的值"""
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key, default)
        else:
            return default
    return data if data is not None else default


class Config:
    def __init__(self):
        self._app_config = load_yaml_config("app.yaml")
        self._proxy_config = load_yaml_config("proxy.yaml")
    
    @property
    def DEBUG(self) -> bool:
        return get_nested(self._app_config, "app", "debug", default=False)
    
    @property
    def CONSOLE_LOG_LEVEL(self) -> str:
        return str(get_nested(self._app_config, "app", "console_log_level", default="WARNING")).upper()
    
    @property
    def HOST(self) -> str:
        return str(get_nested(self._app_config, "app", "host", default="0.0.0.0"))
    
    @property
    def PORT(self) -> int:
        return int(get_nested(self._app_config, "app", "port", default=8000))
    
    @property
    def RUN_MODE(self) -> str:
        return str(get_nested(self._app_config, "run_mode", default="standalone")).lower()
    
    @property
    def CRAWLER_AUTO_START(self) -> bool:
        return get_nested(self._app_config, "crawler_auto_start", default=True)

    @property
    def DATABASE_URL(self) -> str:
        db = self._app_config.get("database", {})
        host = db.get("host", "localhost")
        port = db.get("port", 5432)
        name = db.get("name", "letpub_crawler")
        user = db.get("user", "postgres")
        password = db.get("password", "")
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    
    @property
    def CRAWL_DELAY_MIN(self) -> int:
        return int(get_nested(self._app_config, "crawler", "delay_min", default=3))
    
    @property
    def CRAWL_DELAY_MAX(self) -> int:
        return int(get_nested(self._app_config, "crawler", "delay_max", default=8))
    
    @property
    def MAX_RETRY(self) -> int:
        return int(get_nested(self._app_config, "crawler", "max_retry", default=3))
    
    @property
    def BATCH_SIZE(self) -> int:
        return int(get_nested(self._app_config, "crawler", "batch_size", default=5))

    @property
    def PARALLEL_WORKERS(self) -> int:
        return int(get_nested(self._app_config, "crawler", "parallel_workers", default=3))
    
    @property
    def WORKER_ID(self) -> str:
        return str(get_nested(self._app_config, "distributed", "worker_id", default="") or "")
    
    @property
    def TASK_LOCK_TIMEOUT(self) -> int:
        return int(get_nested(self._app_config, "distributed", "task_lock_timeout", default=600))
    
    @property
    def HEARTBEAT_INTERVAL(self) -> int:
        return int(get_nested(self._app_config, "distributed", "heartbeat_interval", default=30))
    
    @property
    def WORKER_TIMEOUT(self) -> int:
        return int(get_nested(self._app_config, "distributed", "worker_timeout", default=120))
    
    @property
    def MASTER_URL(self) -> str:
        return str(get_nested(self._app_config, "distributed", "master_url", default="") or "")
    
    @property
    def ENCRYPTION_KEY(self) -> str:
        return str(get_nested(self._app_config, "encryption_key", default="") or "")

    @property
    def CLASH_ENABLED(self) -> bool:
        return bool(get_nested(self._app_config, "clash", "enabled", default=False))

    @property
    def CLASH_PROFILE_DIR(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "profile_dir",
            default="",
        ) or "")

    @property
    def CLASH_CONTROLLER(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "controller",
            default="http://127.0.0.1:9097",
        ))

    @property
    def CLASH_SECRET(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "secret",
            default="",
        ) or "")

    @property
    def CLASH_LISTENER_PORT(self) -> int:
        return int(get_nested(
            self._app_config, "clash", "listener_port",
            default=30000,
        ))

    @property
    def CLASH_GROUP_NAME(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "group_name",
            default="crawler-pool",
        ))

    # Cookie 配置（运行时可修改）
    LETPUB_COOKIE: str = ""
    
    @property
    def proxy_config(self) -> dict:
        return self._proxy_config
    
    # 目标网站
    BASE_URL: str = "https://www.letpub.com.cn"
    
    @property
    def ENTRY_URL(self) -> str:
        return f"{self.BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag=all&firstletter="
    
    USER_AGENTS: list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    ]
    
    def reload(self):
        """重新加载配置文件"""
        self._app_config = load_yaml_config("app.yaml")
        self._proxy_config = load_yaml_config("proxy.yaml")


config = Config()
