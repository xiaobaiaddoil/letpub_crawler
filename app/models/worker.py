"""Worker模型 - 用于追踪分布式爬虫节点"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from app.database import Base


class Worker(Base):
    """爬虫Worker节点"""
    __tablename__ = "workers"

    id = Column(Integer, primary_key=True, index=True)
    worker_id = Column(String(100), unique=True, nullable=False, index=True)  # 唯一标识
    hostname = Column(String(200))  # 主机名
    ip_address = Column(String(50))  # IP地址

    # 状态
    status = Column(String(20), default="offline")  # online, offline, busy
    current_task_count = Column(Integer, default=0)  # 当前处理任务数
    total_completed = Column(Integer, default=0)  # 累计完成任务数
    total_failed = Column(Integer, default=0)  # 累计失败任务数

    # 时间戳
    registered_at = Column(DateTime, default=datetime.utcnow)  # 首次注册时间
    last_heartbeat = Column(DateTime)  # 最后心跳时间
    started_at = Column(DateTime)  # 本次启动时间

    # 额外信息
    version = Column(String(50))  # Worker版本
    extra_info = Column(Text)  # JSON格式的额外信息


class WorkerStatus:
    """Worker状态枚举"""
    ONLINE = "online"
    OFFLINE = "offline"
    BUSY = "busy"
