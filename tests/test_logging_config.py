import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.logging_config import setup_app_logging


def test_logging_uses_configured_directory_and_timezone(monkeypatch, tmp_path):
    log_dir = tmp_path / "worker"
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    monkeypatch.setenv("LOG_TIMEZONE", "Asia/Shanghai")

    root_logger = logging.getLogger()
    old_handlers = root_logger.handlers[:]
    old_level = root_logger.level

    try:
        setup_app_logging(debug=False, console_level="ERROR")
        logging.getLogger("tests.logging").info("worker log directory check")

        for handler in root_logger.handlers:
            handler.flush()

        date_suffix = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
        info_log = log_dir / f"info_{date_suffix}.log"

        assert info_log.exists()
        assert "worker log directory check" in info_log.read_text(encoding="utf-8")
    finally:
        for handler in root_logger.handlers:
            handler.close()
        root_logger.handlers.clear()
        root_logger.handlers.extend(old_handlers)
        root_logger.setLevel(old_level)
