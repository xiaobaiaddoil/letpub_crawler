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
        ip="127.0.0.1", port=60000, protocol="http",
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


def test_proxy_success_increments_success_and_reduces_fail_count(db):
    p = _add(db, source="clash")
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 0
    assert p.is_valid is True


def test_proxy_failure_lowers_weight_without_immediate_disable(db):
    p = _add(db, source="clash", fail_count=0, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 1
    assert p.total_fail_count == 1
    assert p.is_valid is True
    assert p.is_active is True


def test_clash_proxy_failure_keeps_listener_valid_at_threshold(db):
    p = _add(db, source="clash", fail_count=2, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 3
    assert p.total_fail_count == 1
    assert p.is_valid is True
    assert p.is_active is True


def test_proxy_failure_marks_invalid_at_threshold(db):
    p = _add(db, source="manual", fail_count=2, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 3
    assert p.total_fail_count == 1
    assert p.is_valid is False


def test_proxy_success_reduces_fail_count(db):
    p = _add(db, source="manual", fail_count=2, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 1


def test_proxy_weight_success_and_failure(db):
    service = ProxyService(db)
    good = _add(db, success_count=10, fail_count=0, total_fail_count=0)
    bad = _add(db, port=60001, success_count=0, fail_count=2, total_fail_count=10)

    assert service._proxy_weight(good) > service._proxy_weight(bad)


def test_proxy_stats_distinguish_total_active_and_assignable(db):
    _add(db, port=60000, source="clash", is_active=True, is_valid=True, fail_count=5)
    _add(db, port=60001, source="clash", is_active=False, is_valid=True, fail_count=0)
    _add(db, port=30002, source="manual", is_active=True, is_valid=True, fail_count=3)
    _add(db, port=30003, source="manual", is_active=True, is_valid=False, fail_count=3)

    stats = ProxyService(db).get_stats()

    assert stats["total"] == 4
    assert stats["active"] == 3
    assert stats["valid"] == 2
    assert stats["assignable"] == 1
    assert stats["inactive"] == 1
    assert stats["active_invalid"] == 1


@pytest.mark.asyncio
async def test_excluding_all_available_proxies_does_not_reuse_one(db):
    p = _add(db)

    proxy = await ProxyService(db).get_random_proxy(exclude_ids=[p.id])

    assert proxy is None


@pytest.mark.asyncio
async def test_clash_proxy_with_high_fail_count_remains_candidate(db):
    p = _add(db, fail_count=10, total_fail_count=10, is_valid=True, source="clash")

    proxy = await ProxyService(db).get_random_proxy()

    assert proxy is not None
    assert proxy.id == p.id


@pytest.mark.asyncio
async def test_stale_clash_listener_below_configured_port_is_not_candidate(db):
    _add(db, port=30000, source="clash", is_active=True, is_valid=True)

    proxy = await ProxyService(db).get_random_proxy()

    assert proxy is None


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
