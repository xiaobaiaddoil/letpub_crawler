import os
from pathlib import Path
from typing import Any
import yaml
from dotenv import load_dotenv

CONFIG_DIR = Path(__file__).parent.parent / "config"

load_dotenv(override=False)


def env_value(*names: str, default: Any = None) -> Any:
    """Return the first non-empty environment variable value."""
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return default


def coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def env_bool(name: str, default: Any = False) -> bool:
    return coerce_bool(env_value(name, default=default))


def env_int(name: str, default: Any = 0) -> int:
    return int(env_value(name, default=default))


def env_float(name: str, default: Any = 0.0) -> float:
    return float(env_value(name, default=default))


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
        self.LETPUB_COOKIE = str(env_value("LETPUB_COOKIE", default=self.LETPUB_COOKIE) or "")
    
    @property
    def DEBUG(self) -> bool:
        return env_bool("DEBUG", default=get_nested(self._app_config, "app", "debug", default=False))
    
    @property
    def CONSOLE_LOG_LEVEL(self) -> str:
        return str(env_value(
            "CONSOLE_LOG_LEVEL",
            default=get_nested(self._app_config, "app", "console_log_level", default="WARNING"),
        )).upper()
    
    @property
    def HOST(self) -> str:
        return str(env_value("HOST", default=get_nested(self._app_config, "app", "host", default="0.0.0.0")))
    
    @property
    def PORT(self) -> int:
        return env_int("PORT", default=get_nested(self._app_config, "app", "port", default=8000))
    
    @property
    def RUN_MODE(self) -> str:
        return str(env_value("RUN_MODE", default=get_nested(self._app_config, "run_mode", default="standalone"))).lower()
    
    @property
    def CRAWLER_AUTO_START(self) -> bool:
        return env_bool("CRAWLER_AUTO_START", default=get_nested(self._app_config, "crawler_auto_start", default=True))

    @property
    def DATABASE_URL(self) -> str:
        database_url = env_value("DATABASE_URL")
        if database_url:
            return str(database_url)

        db = self._app_config.get("database", {})
        host = env_value("DB_HOST", "POSTGRES_HOST", default=db.get("host", "localhost"))
        port = env_value("DB_PORT", "POSTGRES_PORT", default=db.get("port", 5432))
        name = env_value("DB_NAME", "POSTGRES_DB", default=db.get("name", "letpub_crawler"))
        user = env_value("DB_USER", "POSTGRES_USER", default=db.get("user", "postgres"))
        password = env_value("DB_PASSWORD", "POSTGRES_PASSWORD", default=db.get("password", ""))
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"
    
    @property
    def CRAWL_DELAY_MIN(self) -> int:
        return env_int("CRAWL_DELAY_MIN", default=get_nested(self._app_config, "crawler", "delay_min", default=1))
    
    @property
    def CRAWL_DELAY_MAX(self) -> int:
        return env_int("CRAWL_DELAY_MAX", default=get_nested(self._app_config, "crawler", "delay_max", default=3))

    @property
    def CRAWLER_FETCH_MODE(self) -> str:
        mode = str(env_value(
            "CRAWLER_FETCH_MODE",
            default=get_nested(self._app_config, "crawler", "fetch_mode", default="http"),
        ) or "http").lower()
        if mode not in {"http", "browser"}:
            return "http"
        return mode

    @property
    def CRAWLER_ALLOW_DIRECT_FALLBACK(self) -> bool:
        return env_bool(
            "CRAWLER_ALLOW_DIRECT_FALLBACK",
            default=get_nested(self._app_config, "crawler", "allow_direct_fallback", default=False),
        )

    @property
    def PROXY_UNAVAILABLE_SLEEP_SECONDS(self) -> int:
        return env_int(
            "PROXY_UNAVAILABLE_SLEEP_SECONDS",
            default=get_nested(self._app_config, "crawler", "proxy_unavailable_sleep_seconds", default=60),
        )
    
    @property
    def MAX_RETRY(self) -> int:
        return env_int("MAX_RETRY", default=get_nested(self._app_config, "crawler", "max_retry", default=3))
    
    @property
    def BATCH_SIZE(self) -> int:
        # Deprecated: consumer pool uses limit=1 per coroutine; kept for config compatibility
        return env_int("BATCH_SIZE", default=get_nested(self._app_config, "crawler", "batch_size", default=5))

    @property
    def PARALLEL_WORKERS(self) -> int:
        return env_int("PARALLEL_WORKERS", default=get_nested(self._app_config, "crawler", "parallel_workers", default=4))

    @property
    def COMMENT_PARALLEL_WORKERS(self) -> int:
        return env_int(
            "COMMENT_PARALLEL_WORKERS",
            default=get_nested(self._app_config, "crawler", "comment_parallel_workers", default=1),
        )

    @property
    def COMMENT_DELAY_MIN(self) -> float:
        return env_float(
            "COMMENT_DELAY_MIN",
            default=get_nested(self._app_config, "crawler", "comment_delay_min", default=0.5),
        )

    @property
    def COMMENT_DELAY_MAX(self) -> float:
        min_delay = self.COMMENT_DELAY_MIN
        configured = env_float(
            "COMMENT_DELAY_MAX",
            default=get_nested(self._app_config, "crawler", "comment_delay_max", default=1.5),
        )
        return max(min_delay, configured)
    
    @property
    def WORKER_ID(self) -> str:
        return str(env_value("WORKER_ID", default=get_nested(self._app_config, "distributed", "worker_id", default="")) or "")
    
    @property
    def TASK_LOCK_TIMEOUT(self) -> int:
        return env_int("TASK_LOCK_TIMEOUT", default=get_nested(self._app_config, "distributed", "task_lock_timeout", default=600))
    
    @property
    def HEARTBEAT_INTERVAL(self) -> int:
        return env_int("HEARTBEAT_INTERVAL", default=get_nested(self._app_config, "distributed", "heartbeat_interval", default=30))
    
    @property
    def WORKER_TIMEOUT(self) -> int:
        return env_int("WORKER_TIMEOUT", default=get_nested(self._app_config, "distributed", "worker_timeout", default=120))
    
    @property
    def MASTER_URL(self) -> str:
        return str(env_value("MASTER_URL", default=get_nested(self._app_config, "distributed", "master_url", default="")) or "")
    
    @property
    def ENCRYPTION_KEY(self) -> str:
        return str(env_value("ENCRYPTION_KEY", default=get_nested(self._app_config, "encryption_key", default="")) or "")

    @property
    def CLASH_ENABLED(self) -> bool:
        return env_bool("CLASH_ENABLED", default=get_nested(self._app_config, "clash", "enabled", default=False))

    @property
    def CLASH_PROFILE_DIR(self) -> str:
        return str(env_value("CLASH_PROFILE_DIR", default=get_nested(
            self._app_config, "clash", "profile_dir",
            default="",
        )) or "")

    @property
    def CLASH_CONTROLLER(self) -> str:
        return str(env_value("CLASH_CONTROLLER", default=get_nested(
            self._app_config, "clash", "controller",
            default="http://127.0.0.1:9097",
        )))

    @property
    def CLASH_SECRET(self) -> str:
        return str(env_value("CLASH_SECRET", default=get_nested(
            self._app_config, "clash", "secret",
            default="",
        )) or "")

    @property
    def CLASH_LISTENER_PORT(self) -> int:
        return env_int("CLASH_LISTENER_PORT", default=get_nested(
            self._app_config, "clash", "listener_port",
            default=60000,
        ))

    @property
    def CLASH_GROUP_NAME(self) -> str:
        return str(env_value("CLASH_GROUP_NAME", default=get_nested(
            self._app_config, "clash", "group_name",
            default="crawler-pool",
        )))

    @property
    def HOST_PROXY_HOST(self) -> str:
        return str(env_value("HOST_PROXY_HOST", "DOCKER_HOST_PROXY_HOST", default=get_nested(
            self._app_config, "proxy", "host_proxy_host",
            default="",
        )) or "")

    @property
    def PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH(self) -> str:
        return str(env_value("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH", default="") or "")

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
