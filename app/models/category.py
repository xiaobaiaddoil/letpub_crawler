from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import relationship
from app.database import Base

class Category(Base):
    """分类表"""
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    field_tag = Column(String(50), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    total_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联期刊
    journals = relationship("Journal", back_populates="category")

    def __repr__(self):
        return f"<Category(id={self.id}, name={self.name}, field_tag={self.field_tag})>"
