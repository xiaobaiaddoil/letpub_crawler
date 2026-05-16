"""Unit tests for ClashService."""
import pytest
from pathlib import Path

import httpx

from app.services.clash_service import ClashService


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def service(tmp_clash_dir):
    return ClashService(
        profile_dir=tmp_clash_dir,
        controller="http://127.0.0.1:9097",
        secret="test-secret",
    )


def test_extract_proxy_names_basic(service):
    names = service.extract_proxy_names(FIXTURES / "sample_profile.yaml")
    assert names == ["节点A", "节点B", "节点C"]


def test_extract_proxy_names_empty_proxies(service, tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("proxies: []\n")
    assert service.extract_proxy_names(p) == []


def test_extract_proxy_names_missing_key(service, tmp_path):
    p = tmp_path / "no_proxies.yaml"
    p.write_text("port: 7890\nmode: Rule\n")
    with pytest.raises(ValueError, match="proxies"):
        service.extract_proxy_names(p)


def test_extract_proxy_names_dedupe(service, tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "proxies:\n"
        "  - {name: A, type: trojan, server: a, port: 1, password: x}\n"
        "  - {name: B, type: trojan, server: b, port: 1, password: x}\n"
        "  - {name: A, type: trojan, server: c, port: 1, password: x}\n"
    )
    assert service.extract_proxy_names(p) == ["A", "B"]


def test_get_current_profile_path_resolves_uid(service, tmp_clash_dir):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\n"
        "items:\n"
        "  - uid: AAA\n"
        "    type: remote\n"
        "    file: AAA.yaml\n"
        "  - uid: BBB\n"
        "    type: remote\n"
        "    file: BBB.yaml\n"
    )
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text("proxies: []\n")
    path = service.get_current_profile_path()
    assert path == tmp_clash_dir / "profiles" / "AAA.yaml"


def test_get_current_profile_path_no_match(service, tmp_clash_dir):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: ZZZ\n"
        "items:\n"
        "  - uid: AAA\n"
        "    file: AAA.yaml\n"
    )
    with pytest.raises(FileNotFoundError, match="ZZZ"):
        service.get_current_profile_path()


def test_get_current_profile_path_no_profiles_yaml(service, tmp_clash_dir):
    with pytest.raises(FileNotFoundError, match="profiles.yaml"):
        service.get_current_profile_path()


import yaml as _yaml

MANAGED_HEADER = "# managed-by: letpub-crawler"


def test_render_merge_yaml_managed_header(service):
    out = service.render_merge_yaml(["A", "B"])
    assert out.startswith(MANAGED_HEADER)


def test_render_merge_yaml_structure(service):
    out = service.render_merge_yaml(["A", "B", "C"])
    data = _yaml.safe_load(out)
    assert "proxy-groups" in data
    assert "listeners" in data
    assert len(data["proxy-groups"]) == 1
    assert len(data["listeners"]) == 1


def test_render_merge_yaml_listener_port(service):
    out = service.render_merge_yaml(["A"], listener_port=31234)
    data = _yaml.safe_load(out)
    listener = data["listeners"][0]
    assert listener["port"] == 31234
    assert listener["listen"] == "127.0.0.1"
    assert listener["type"] == "mixed"
    assert listener["proxy"] == "crawler-pool"


def test_render_merge_yaml_group_proxies(service):
    names = ["节点A", "节点B", "节点C"]
    out = service.render_merge_yaml(names)
    data = _yaml.safe_load(out)
    group = data["proxy-groups"][0]
    assert group["name"] == "crawler-pool"
    assert group["type"] == "load-balance"
    assert group["strategy"] == "round-robin"
    assert group["proxies"] == names


def test_render_merge_yaml_custom_group_name(service):
    out = service.render_merge_yaml(["A"], group_name="my-pool")
    data = _yaml.safe_load(out)
    assert data["proxy-groups"][0]["name"] == "my-pool"
    assert data["listeners"][0]["proxy"] == "my-pool"


def test_write_merge_creates_file(service, tmp_clash_dir):
    content = service.render_merge_yaml(["A"])
    path = service.write_merge(content)
    assert path == tmp_clash_dir / "profiles" / "Merge.yaml"
    assert path.read_text(encoding="utf-8") == content


def test_write_merge_overwrites_managed(service, tmp_clash_dir):
    target = tmp_clash_dir / "profiles" / "Merge.yaml"
    target.write_text(
        f"{ClashService.MANAGED_HEADER}\nold\n", encoding="utf-8"
    )
    new = service.render_merge_yaml(["A"])
    service.write_merge(new)
    assert target.read_text(encoding="utf-8") == new
    backups = list((tmp_clash_dir / "profiles").glob("Merge.yaml.bak.*"))
    assert backups == []


def test_write_merge_backups_unmanaged(service, tmp_clash_dir):
    target = tmp_clash_dir / "profiles" / "Merge.yaml"
    original = "profile:\n  store-selected: true\n"
    target.write_text(original, encoding="utf-8")
    new = service.render_merge_yaml(["A"])
    service.write_merge(new)
    assert target.read_text(encoding="utf-8") == new
    backups = list((tmp_clash_dir / "profiles").glob("Merge.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original


@pytest.mark.asyncio
async def test_reload_via_api_success(service, tmp_clash_dir, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("mixed-port: 7897\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is True
    assert captured["method"] == "PUT"
    assert "/configs?force=true" in captured["url"]
    assert captured["auth"] == "Bearer test-secret"
    assert str(cfg) in captured["body"]


@pytest.mark.asyncio
async def test_reload_via_api_unauthorized(service, tmp_clash_dir, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is False


@pytest.mark.asyncio
async def test_reload_via_api_connection_error(service, tmp_clash_dir, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is False


def test_sync_proxy_pool_inserts_single_entry(service, in_memory_db):
    affected = service.sync_proxy_pool(
        db=in_memory_db,
        node_count=42,
        listener_port=30000,
    )
    from app.models.proxy_pool import ProxyPool
    rows = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash"
    ).all()
    assert len(rows) == 1
    p = rows[0]
    assert p.ip == "127.0.0.1"
    assert p.port == 30000
    assert p.protocol == "http"
    assert p.is_active is True
    assert p.is_valid is True
    assert "42 nodes" in (p.remark or "")
    assert affected == p.id


def test_sync_proxy_pool_idempotent(service, in_memory_db):
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30000)
    service.sync_proxy_pool(in_memory_db, node_count=15, listener_port=30000)
    from app.models.proxy_pool import ProxyPool
    active = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).count()
    inactive = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == False,
    ).count()
    assert active == 1
    assert inactive == 1


def test_sync_proxy_pool_changing_port_deactivates_old(service, in_memory_db):
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30000)
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30001)
    from app.models.proxy_pool import ProxyPool
    new = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).one()
    assert new.port == 30001
