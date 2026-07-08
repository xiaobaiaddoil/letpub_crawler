from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from app.database import Base


class JournalMetricSnapshot(Base):
    """期刊关键指标快照."""

    __tablename__ = "journal_metric_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, ForeignKey("journals.journal_id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("crawl_tasks.id"), nullable=True, index=True)
    source = Column(String(50), default="detail", index=True)
    metrics = Column(JSONB)
    metric_hash = Column(String(64), index=True)
    crawled_at = Column(DateTime, default=datetime.utcnow, index=True)


class JournalMetricChange(Base):
    """期刊关键指标变化明细."""

    __tablename__ = "journal_metric_changes"

    id = Column(Integer, primary_key=True, index=True)
    journal_id = Column(Integer, ForeignKey("journals.journal_id"), nullable=False, index=True)
    task_id = Column(Integer, ForeignKey("crawl_tasks.id"), nullable=True, index=True)
    source = Column(String(50), default="detail", index=True)
    field_name = Column(String(100), nullable=False, index=True)
    old_value = Column(Text)
    new_value = Column(Text)
    changed_at = Column(DateTime, default=datetime.utcnow, index=True)
