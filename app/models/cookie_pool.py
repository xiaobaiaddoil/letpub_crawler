"""Cookie池模型 - 用于管理多个Cookie供Worker随机选择使用"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from app.database import Base


class CookiePool(Base):
    """Cookie池"""
    __tablename__ = "cookie_pool"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False)  # Cookie名称/备注
    cookie_value = Column(Text, nullable=False)  # Cookie值 (PHPSESSID=xxx)

    # 状态
    is_active = Column(Boolean, default=True)  # 是否启用
    success_count = Column(Integer, default=0)  # 成功使用次数
    fail_count = Column(Integer, default=0)  # 失败次数
    last_used_at = Column(DateTime)  # 最后使用时间
    last_success_at = Column(DateTime)  # 最后成功时间
    last_fail_at = Column(DateTime)  # 最后失败时间

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 额外信息
    remark = Column(Text)  # 备注信息
