"""LetPub账号模型 - 用于存储登录账号，密码加密存储"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text
from app.database import Base


class Account(Base):
    """LetPub登录账号"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), nullable=False, unique=True)  # 登录邮箱
    password_encrypted = Column(Text, nullable=False)  # 加密后的密码

    # 状态
    is_active = Column(Boolean, default=True)  # 是否启用
    login_success_count = Column(Integer, default=0)  # 登录成功次数
    login_fail_count = Column(Integer, default=0)  # 登录失败次数
    last_login_at = Column(DateTime)  # 最后登录时间
    last_cookie_refresh_at = Column(DateTime)  # 最后Cookie刷新时间

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 备注
    remark = Column(Text)
