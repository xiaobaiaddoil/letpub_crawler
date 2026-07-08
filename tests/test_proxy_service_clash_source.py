"""ProxyService 对 source=clash 的特殊处理。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.proxy_pool import ProxyPool
from app.services.proxy_service import ProxyService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    # 仅建测试需要的表（避免 JSONB 等非 SQLite 兼容类型）
    ProxyPool.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _add(db, **kwargs) -> ProxyPool:
    defaults = dict(
        ip="127.0.0.1", port=30000, protocol="http",
        proxy_type="direct", source="clash",
        is_active=True, is_valid=True,
        success_count=0, fail_count=0, total_fail_count=0,
    )
    defaults.update(kwargs)
    p = ProxyPool(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_clash_success_increments_success_only(db):
    p = _add(db, source="clash")
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 0
    assert p.is_valid is True


def test_clash_failure_no_penalty(db):
    p = _add(db, source="clash", fail_count=0, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 0
    assert p.is_valid is True
    assert p.is_active is True


def test_non_clash_failure_marks_invalid(db):
    p = _add(db, source="manual", fail_count=0, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 1
    assert p.total_fail_count == 1
    assert p.is_valid is False


def test_non_clash_success_resets_fail_count(db):
    p = _add(db, source="manual", fail_count=2, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 0


@pytest.mark.asyncio
async def test_clash_check_proxy_skips_external_probe(db, monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("httpx AsyncClient should not be called for clash proxy")

    monkeypatch.setattr("httpx.AsyncClient", fail_if_called)
    p = _add(db, source="clash", is_valid=False)

    result = await ProxyService(db).check_proxy(p)

    db.refresh(p)
    assert result is True
    assert p.is_valid is True
    assert p.last_check_at is not None
