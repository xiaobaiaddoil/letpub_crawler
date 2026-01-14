"""问题任务记录表 - 记录出现警告或错误的任务"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Enum
import enum
from app.database import Base


class ProblemType(str, enum.Enum):
    """问题类型"""
    WARNING = "warning"  # 警告（如评论数量不匹配）
    ERROR = "error"      # 错误（任务失败）


class ProblemTask(Base):
    """问题任务记录表"""
    __tablename__ = "problem_tasks"

    id = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, nullable=False, index=True)
    problem_type = Column(String(20), nullable=False, index=True)  # warning / error
    problem_code = Column(String(50), index=True)  # 问题代码，如 comment_mismatch, crawl_failed
    message = Column(Text)  # 问题描述
    
    # 期望值和实际值（用于评论数量不匹配等场景）
    expected_value = Column(Integer)
    actual_value = Column(Integer)
    
    # 状态
    resolved = Column(Integer, default=0, index=True)  # 0=未解决, 1=已解决
    resolved_at = Column(DateTime)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<ProblemTask(journal_id={self.journal_id}, type={self.problem_type}, code={self.problem_code})>"
