"""Pytest fixtures for clash proxy pool tests."""
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture
def tmp_clash_dir(tmp_path: Path) -> Path:
    """模拟 Clash Verge 数据目录结构。"""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return tmp_path


@pytest.fixture
def in_memory_db():
    """内存 SQLite DB（用于 ProxyService 单测）。"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
