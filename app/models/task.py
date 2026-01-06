from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Enum
import enum
from app.database import Base

class TaskType(str, enum.Enum):
    CATEGORY = "category"
    LIST = "list"
    DETAIL = "detail"
    COMMENT = "comment"

class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class CrawlTask(Base):
    """爬虫任务表"""
    __tablename__ = "crawl_tasks"

    id = Column(Integer, primary_key=True, index=True)
    task_type = Column(String(50), nullable=False, index=True)
    target_id = Column(String(100), nullable=False)  # 分类ID、期刊ID或页码
    target_url = Column(Text)  # 目标URL

    status = Column(String(20), default=TaskStatus.PENDING.value, index=True)
    retry_count = Column(Integer, default=0)
    max_retry = Column(Integer, default=3)
    error_message = Column(Text)

    # 分布式支持字段
    worker_id = Column(String(100), nullable=True, index=True)  # 执行节点标识
    locked_at = Column(DateTime, nullable=True)  # 任务锁定时间（用于超时释放）

    # 额外数据
    extra_data = Column(Text)  # JSON格式的额外数据

    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    def __repr__(self):
        return f"<CrawlTask(id={self.id}, type={self.task_type}, target={self.target_id}, status={self.status}, worker={self.worker_id})>"
