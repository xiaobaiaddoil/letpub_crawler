from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Boolean, Numeric, ForeignKey, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.database import Base

class Journal(Base):
    """期刊表"""
    __tablename__ = "journals"

    id = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, unique=True, nullable=False, index=True)
    name = Column(String(500), nullable=False)
    issn = Column(String(20))
    eissn = Column(String(20))

    # 影响因子相关
    impact_factor = Column(Numeric(10, 4))  # 影响因子
    impact_factor_realtime = Column(Numeric(10, 4))  # 实时影响因子
    self_citation_rate = Column(String(100))  # 自引率

    # 分区和评分
    jcr_partition = Column(String(10000))  # JCR分区
    cas_partition = Column(String(10000))  # 中科院分区（可能包含多个分区信息）
    cas_warning = Column(String(10000))  # CAS预警信息
    citescore = Column(String(10000))  # CiteScore

    # 审稿相关
    review_speed = Column(String(10000))  # 审稿速度
    acceptance_rate = Column(String(10000))  # 录用比例

    category_id = Column(Integer, ForeignKey("categories.id"))
    detail_crawled = Column(Boolean, default=False)
    comments_crawled = Column(Boolean, default=False)
    detail_data = Column(JSONB)  # 存储完整详情数据

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    category = relationship("Category", back_populates="journals")
    comments = relationship("Comment", back_populates="journal")

    def __repr__(self):
        return f"<Journal(id={self.id}, journal_id={self.journal_id}, name={self.name})>"
