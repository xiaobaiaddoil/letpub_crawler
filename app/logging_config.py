"""
日志配置模块
支持：
- 分级别文件存储（INFO、WARNING、ERROR）
- 控制台彩色输出
- 日志轮转
- 统一格式化
"""
import os
import logging
import logging.handlers
from pathlib import Path
from datetime import datetime
from app.config import config

# 日志目录
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# 日志格式
DETAILED_FORMAT = logging.Formatter(
    fmt='%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

SIMPLE_FORMAT = logging.Formatter(
    fmt='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%H:%M:%S'
)


class ColoredFormatter(logging.Formatter):
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
        log_color = self.COLORS.get(record.levelname, self.COLORS['RESET'])
        record.levelname = f"{log_color}{record.levelname}{self.COLORS['RESET']}"
        return super().format(record)


def setup_logger(name: str = None, level: int = logging.INFO) -> logging.Logger:
    """
    设置日志记录器

    Args:
        name: 日志记录器名称，None表示根记录器
        level: 日志级别

    Returns:
        配置好的日志记录器
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # 避免重复添加处理器
    if logger.handlers:
        return logger

    # 1. 控制台处理器（彩色输出，INFO及以上）
    console_handler = logging.StreamHandler()
    # 
    
    console_handler.setLevel(logging.WARNING)
    console_formatter = ColoredFormatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    # 2. INFO 级别文件处理器（所有INFO及以上日志）
    info_file = LOG_DIR / f"info_{datetime.now().strftime('%Y%m%d')}.log"
    info_handler = logging.handlers.RotatingFileHandler(
        info_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(DETAILED_FORMAT)
    logger.addHandler(info_handler)

    # 3. WARNING 级别文件处理器（WARNING及以上）
    warning_file = LOG_DIR / f"warning_{datetime.now().strftime('%Y%m%d')}.log"
    warning_handler = logging.handlers.RotatingFileHandler(
        warning_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    warning_handler.setLevel(logging.WARNING)
    warning_handler.setFormatter(DETAILED_FORMAT)
    logger.addHandler(warning_handler)

    # 4. ERROR 级别文件处理器（ERROR及以上，包含完整堆栈）
    error_file = LOG_DIR / f"error_{datetime.now().strftime('%Y%m%d')}.log"
    error_handler = logging.handlers.RotatingFileHandler(
        error_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=10,  # ERROR日志保留更多备份
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(DETAILED_FORMAT)
    logger.addHandler(error_handler)

    # 5. DEBUG 文件处理器（开发调试用，可选）
    if level == logging.DEBUG:
        debug_file = LOG_DIR / f"debug_{datetime.now().strftime('%Y%m%d')}.log"
        debug_handler = logging.handlers.RotatingFileHandler(
            debug_file,
            maxBytes=20 * 1024 * 1024,  # 20MB
            backupCount=3,
            encoding='utf-8'
        )
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(DETAILED_FORMAT)
        logger.addHandler(debug_handler)

    return logger


def setup_app_logging(debug: bool = False):
    """
    初始化应用程序日志系统

    Args:
        debug: 是否启用DEBUG级别日志
    """
    level = logging.DEBUG if debug else logging.INFO

    # 设置根日志记录器
    root_logger = setup_logger(None, level)

    # 设置各模块的日志记录器
    modules = [
        'app.crawler',
        'app.services',
        'app.models',
        'app.api',
    ]

    for module in modules:
        setup_logger(module, level)

    # 降低第三方库的日志级别
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('selenium').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)

    root_logger.info(f"日志系统初始化完成，级别: {logging.getLevelName(level)}")
    root_logger.info(f"日志文件保存至: {LOG_DIR.absolute()}")


# 清理旧日志文件的工具函数
def clean_old_logs(days: int = 7):
    """
    清理指定天数之前的日志文件

    Args:
        days: 保留最近多少天的日志
    """
    from datetime import timedelta

    cutoff_date = datetime.now() - timedelta(days=days)

    if not LOG_DIR.exists():
        return

    deleted_count = 0
    for log_file in LOG_DIR.glob("*.log*"):
        try:
            # 从文件名提取日期（格式：info_20240101.log）
            parts = log_file.stem.split('_')
            if len(parts) >= 2:
                date_str = parts[1][:8]  # 取前8位日期
                file_date = datetime.strptime(date_str, '%Y%m%d')

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
    setup_app_logging(debug=True)

    logger = logging.getLogger(__name__)

    logger.debug("这是一条DEBUG消息")
    logger.info("这是一条INFO消息")
    logger.warning("这是一条WARNING消息")
    logger.error("这是一条ERROR消息")

    try:
        1 / 0
    except Exception as e:
        logger.exception("捕获到异常")

    print(f"\n日志文件已保存到: {LOG_DIR.absolute()}")