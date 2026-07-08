import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.proxy_pool import ProxyPool
from app.services.proxy_service import ProxyService, parse_proxy_text_line


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    ProxyPool.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_parse_proxy_text_line_common_formats():
    p = parse_proxy_text_line("1.2.3.4:8080:user:pass")
    assert (p.ip, p.port, p.username, p.password) == ("1.2.3.4", 8080, "user", "pass")

    p = parse_proxy_text_line("user:pass@proxy.example.com:8000")
    assert (p.ip, p.port, p.username, p.password) == ("proxy.example.com", 8000, "user", "pass")

    p = parse_proxy_text_line("http://u:p%40ss@proxy.example.com:8000")
    assert (p.protocol, p.ip, p.port, p.username, p.password) == (
        "http",
        "proxy.example.com",
        8000,
        "u",
        "p@ss",
    )


def test_import_proxies_adds_and_updates(db):
    service = ProxyService(db)
    result = service.import_proxies_from_text(
        "1.2.3.4:8080\n1.2.3.4:8080:user:pass\nbad-line",
        proxy_type="private",
    )

    assert result["added"] == 1
    assert result["updated"] == 1
    assert result["failed"] == 1
    assert db.query(ProxyPool).count() == 1

    proxy = db.query(ProxyPool).one()
    assert proxy.ip == "1.2.3.4"
    assert proxy.port == 8080
    assert proxy.username == "user"
    assert proxy.proxy_type == "private"


def test_export_proxies_with_auth(db):
    service = ProxyService(db)
    service.add_proxy(
        ip="1.2.3.4",
        port=8080,
        proxy_type="private",
        username="user",
        password="pass",
    )

    assert service.export_proxies_as_text(fmt="hostport_auth") == "1.2.3.4:8080:user:pass"
    assert service.export_proxies_as_text(fmt="url") == "http://user:pass@1.2.3.4:8080"
    assert service.export_proxies_as_text(fmt="hostport") == "1.2.3.4:8080"
