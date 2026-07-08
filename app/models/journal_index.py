from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from app.database import Base


class CategoryIndexState(Base):
    """分类索引检测状态."""

    __tablename__ = "category_index_state"

    id = Column(Integer, primary_key=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, unique=True, index=True)
    field_tag = Column(String(50), nullable=False, unique=True, index=True)
    remote_total_count = Column(Integer, default=0)
    local_index_count = Column(Integer, default=0)
    total_pages = Column(Integer, default=0)
    last_page_count = Column(Integer, default=0)
    estimated_new_count = Column(Integer, default=0)
    status = Column(String(50), default="unknown", index=True)
    last_checked_at = Column(DateTime, default=datetime.utcnow)
    last_full_scanned_at = Column(DateTime)


class CategoryJournalIndex(Base):
    """分类和期刊的索引关系.

    一个期刊可能出现在多个分类中，不能只依赖 journals.category_id 统计分类数量。
    """

    __tablename__ = "category_journal_index"
    __table_args__ = (
        UniqueConstraint("category_id", "journal_id", name="uq_category_journal_index"),
    )

    id = Column(Integer, primary_key=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)
    journal_id = Column(Integer, ForeignKey("journals.journal_id"), nullable=False, index=True)
    page_no = Column(Integer)
    position_no = Column(Integer)
    active = Column(Boolean, default=True, index=True)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CategoryPageIndex(Base):
    """分类列表页指纹，用于后续做更快的差异定位."""

    __tablename__ = "category_page_index"
    __table_args__ = (
        UniqueConstraint("category_id", "page_no", name="uq_category_page_index"),
    )

    id = Column(Integer, primary_key=True, index=True)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False, index=True)
    page_no = Column(Integer, nullable=False)
    item_count = Column(Integer, default=0)
    first_journal_id = Column(Integer)
    last_journal_id = Column(Integer)
    journal_ids_hash = Column(String(64), index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class IndexScanRun(Base):
    """索引扫描批次记录."""

    __tablename__ = "index_scan_runs"

    id = Column(Integer, primary_key=True, index=True)
    mode = Column(String(50), default="index_check")
    status = Column(String(50), default="running", index=True)
    categories_checked = Column(Integer, default=0)
    pages_scheduled = Column(Integer, default=0)
    pages_scanned = Column(Integer, default=0)
    new_journals = Column(Integer, default=0)
    error_message = Column(Text)
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime)
