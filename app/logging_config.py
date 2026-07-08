"""
日志配置模块
支持：
- 从配置文件加载（logging.conf）或代码配置
- 分级别文件存储（INFO、WARNING、ERROR）
- 控制台彩色输出
- 日志轮转
- 统一格式化
"""
import os
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def _get_log_dir() -> Path:
    log_dir = Path(os.getenv("LOG_DIR", "logs") or "logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _get_log_timezone():
    timezone_name = (os.getenv("LOG_TIMEZONE") or os.getenv("TZ") or "Asia/Shanghai").strip()
    if not timezone_name:
        timezone_name = "Asia/Shanghai"
    try:
        return timezone_name, ZoneInfo(timezone_name)
    except Exception:
        if timezone_name in {"Asia/Shanghai", "CST", "UTC+8"}:
            return timezone_name, timezone(timedelta(hours=8))
        return timezone_name, datetime.now().astimezone().tzinfo


class TimezoneFormatter(logging.Formatter):
    """使用指定时区格式化日志时间。"""

    def __init__(self, *args, tzinfo=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tzinfo = tzinfo

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, self.tzinfo)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(sep=" ", timespec="milliseconds")


class ColoredFormatter(TimezoneFormatter):
    """彩色日志格式化器（用于控制台）"""

    # ANSI 颜色代码
    COLORS = {
        'DEBUG': '\033[36m',      # Cyan
        'INFO': '\033[32m',       # Green
        'WARNING': '\033[33m',    # Yellow
        'ERROR': '\033[31m',      # Red
        'CRITICAL': '\033[35m',   # Magenta
        'RESET': '\033[0m'        # Reset
    }

    def format(self, record):
        original_levelname = record.levelname
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.levelname = f"{log_color}{record.levelname}{self.COLORS['RESET']}"
        try:
            return super().format(record)
        finally:
            record.levelname = original_levelname


def setup_app_logging(debug: bool = False, console_level: str = "WARNING"):
    """
    初始化应用程序日志系统

    Args:
        debug: 是否启用DEBUG级别日志
        console_level: 控制台日志级别 (DEBUG/INFO/WARNING/ERROR)
    """
    level = logging.DEBUG if debug else logging.INFO
    log_dir = _get_log_dir()
    timezone_name, log_timezone = _get_log_timezone()
    date_suffix = datetime.now(log_timezone).strftime('%Y%m%d')

    # 详细格式
    detailed_format = TimezoneFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        tzinfo=log_timezone,
    )

    # 根日志记录器
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # 清除已有的处理器
    root_logger.handlers.clear()

    # 1. 控制台处理器（彩色输出）
    console_handler = logging.StreamHandler()
    console_level_value = getattr(logging, console_level.upper(), logging.WARNING)
    console_handler.setLevel(console_level_value)
    console_formatter = ColoredFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S',
        tzinfo=log_timezone,
    )
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # 2. INFO 级别文件处理器
    info_file = log_dir / f"info_{date_suffix}.log"
    info_handler = logging.handlers.RotatingFileHandler(
        info_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(detailed_format)
    root_logger.addHandler(info_handler)

    # 3. WARNING 级别文件处理器
    warning_file = log_dir / f"warning_{date_suffix}.log"
    warning_handler = logging.handlers.RotatingFileHandler(
        warning_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    warning_handler.setLevel(logging.WARNING)
    warning_handler.setFormatter(detailed_format)
    root_logger.addHandler(warning_handler)

    # 4. ERROR 级别文件处理器
    error_file = log_dir / f"error_{date_suffix}.log"
    error_handler = logging.handlers.RotatingFileHandler(
        error_file,
        maxBytes=10 * 1024 * 1024,
        backupCount=10,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(detailed_format)
    root_logger.addHandler(error_handler)

    # 5. DEBUG 文件处理器（仅在DEBUG模式）
    if debug:
        debug_file = log_dir / f"debug_{date_suffix}.log"
        debug_handler = logging.handlers.RotatingFileHandler(
            debug_file,
            maxBytes=20 * 1024 * 1024,
            backupCount=3,
            encoding='utf-8'
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(detailed_format)
        root_logger.addHandler(debug_handler)

    # 降低第三方库的日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('selenium').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    logging.getLogger('playwright').setLevel(logging.WARNING)

    root_logger.info(f"日志系统初始化完成，级别: {logging.getLevelName(level)}")
    root_logger.info(f"控制台日志级别: {console_level.upper()}")
    root_logger.info(f"日志时区: {timezone_name}")
    root_logger.info(f"日志文件保存至: {log_dir.absolute()}")


def clean_old_logs(days: int = 7):
    """
    清理指定天数之前的日志文件

    Args:
        days: 保留最近多少天的日志
    """
    log_dir = _get_log_dir()
    _, log_timezone = _get_log_timezone()
    cutoff_date = (datetime.now(log_timezone) - timedelta(days=days)).date()

    if not log_dir.exists():
        return

    deleted_count = 0
    for log_file in log_dir.glob("*.log*"):
        try:
            # 从文件名提取日期（格式：info_20240101.log）
            parts = log_file.stem.split('_')
            if len(parts) >= 2:
                date_str = parts[1][:8]  # 取前8位日期
                file_date = datetime.strptime(date_str, '%Y%m%d').date()

                if file_date < cutoff_date:
                    log_file.unlink()
                    deleted_count += 1
        except (ValueError, IndexError):
            # 忽略无法解析日期的文件
            continue

    if deleted_count > 0:
        logging.info(f"已清理 {deleted_count} 个过期日志文件")


if __name__ == "__main__":
    # 测试日志配置
    setup_app_logging(debug=True, console_level="INFO")

    logger = logging.getLogger(__name__)

    logger.debug("这是一条DEBUG消息")
    logger.info("这是一条INFO消息")
    logger.warning("这是一条WARNING消息")
    logger.error("这是一条ERROR消息")

    try:
        1 / 0
    except Exception:
        logger.exception("捕获到异常")

    print(f"\n日志文件已保存到: {_get_log_dir().absolute()}")
