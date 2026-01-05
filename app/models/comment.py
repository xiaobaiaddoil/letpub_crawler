from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey
from sqlalchemy.orm import relationship
from app.database import Base

class Comment(Base):
    """评论表"""
    __tablename__ = "comments"

    id = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, ForeignKey("journals.id"), nullable=False)
    comment_id = Column(String(100), unique=True, nullable=False, index=True)  # 用于去重

    content = Column(Text)
    author = Column(String(200))
    rating = Column(String(50))  # 评分
    submit_experience = Column(Text)  # 投稿经历

    comment_time = Column(DateTime)  # 评论时间
    crawled_at = Column(DateTime, default=datetime.utcnow)

    # 关联
    journal = relationship("Journal", back_populates="comments")

    def __repr__(self):
        return f"<Comment(id={self.id}, journal_id={self.journal_id}, author={self.author})>"
