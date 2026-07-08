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


def test_get_current_merge_file_uses_option_merge(service, tmp_clash_dir):
    """profile 的 option.merge 指向某 uid 时，返回该 uid 对应的 file。"""
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: P1\n"
        "items:\n"
        "  - uid: M1\n"
        "    type: merge\n"
        "    file: M1.yaml\n"
        "  - uid: P1\n"
        "    type: remote\n"
        "    file: P1.yaml\n"
        "    option:\n"
        "      merge: M1\n"
    )
    assert service.get_current_merge_file() == "M1.yaml"


def test_get_current_merge_file_no_option(service, tmp_clash_dir):
    """profile 无 option.merge 时返回顶层 'Merge.yaml'。"""
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: P1\n"
        "items:\n"
        "  - uid: P1\n"
        "    type: remote\n"
        "    file: P1.yaml\n"
    )
    assert service.get_current_merge_file() == "Merge.yaml"


import yaml as _yaml

MANAGED_HEADER = "# managed-by: letpub-crawler"


def test_render_merge_yaml_managed_header(service):
    out = service.render_merge_yaml(["A", "B"])
    assert out.startswith(MANAGED_HEADER)


def test_render_merge_yaml_structure(service):
    out = service.render_merge_yaml(["A", "B", "C"])
    data = _yaml.safe_load(out)
    assert "listeners" in data
    assert "proxy-groups" not in data
    assert len(data["listeners"]) == 3


def test_render_merge_yaml_listener_port(service):
    out = service.render_merge_yaml(["A"], listener_port=31234)
    data = _yaml.safe_load(out)
    listener = data["listeners"][0]
    assert listener["port"] == 31234
    assert listener["listen"] == "127.0.0.1"
    assert listener["type"] == "mixed"
    assert listener["proxy"] == "A"


def test_render_merge_yaml_one_listener_per_node(service):
    names = ["节点A", "节点B", "节点C"]
    out = service.render_merge_yaml(names)
    data = _yaml.safe_load(out)
    assert [listener["proxy"] for listener in data["listeners"]] == names
    assert [listener["port"] for listener in data["listeners"]] == [60000, 60001, 60002]


def test_render_merge_yaml_custom_group_name(service):
    out = service.render_merge_yaml(["A"], group_name="my-pool")
    data = _yaml.safe_load(out)
    assert "proxy-groups" not in data
    assert data["listeners"][0]["proxy"] == "A"


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


@pytest.mark.asyncio
async def test_reload_via_api_unix_socket(tmp_clash_dir, monkeypatch):
    """controller 为 unix:// 形式时使用 _build_async_client_unix。"""
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)

    def fake_unix_client(path: str) -> httpx.AsyncClient:
        captured["sock"] = path
        return httpx.AsyncClient(transport=transport, timeout=5.0)

    monkeypatch.setattr(
        "app.services.clash_service._build_async_client_unix",
        fake_unix_client,
    )

    svc = ClashService(
        profile_dir=tmp_clash_dir,
        controller="unix:///tmp/verge/verge-mihomo.sock",
        secret="s",
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    ok = await svc.reload_via_api(config_path=cfg)
    assert ok is True
    assert captured["sock"] == "/tmp/verge/verge-mihomo.sock"
    assert "/configs?force=true" in captured["url"]
    assert captured["auth"] == "Bearer s"


def test_sync_proxy_pool_inserts_one_entry_per_node(service, in_memory_db):
    affected = service.sync_proxy_pool(
        db=in_memory_db,
        node_count=3,
        listener_port=30000,
    )
    from app.models.proxy_pool import ProxyPool
    rows = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash"
    ).order_by(ProxyPool.port.asc()).all()
    assert len(rows) == 3
    p = rows[0]
    assert p.ip == "127.0.0.1"
    assert p.port == 30000
    assert p.protocol == "http"
    assert p.is_active is True
    assert p.is_valid is True
    assert [row.port for row in rows] == [30000, 30001, 30002]
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
    assert active == 15
    assert inactive == 10


def test_sync_proxy_pool_changing_port_deactivates_old(service, in_memory_db):
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30000)
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30001)
    from app.models.proxy_pool import ProxyPool
    new = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).order_by(ProxyPool.port.asc()).first()
    assert new.port == 30001


def test_inject_runtime_config_appends_block(service, tmp_clash_dir):
    runtime = tmp_clash_dir / "clash-verge.yaml"
    runtime.write_text("mode: rule\nmixed-port: 7897\n", encoding="utf-8")

    result_path = service.inject_runtime_config(
        proxy_names=["A", "B"],
        listener_port=30000,
    )
    assert result_path == runtime
    data = _yaml.safe_load(runtime.read_text(encoding="utf-8"))
    assert data["mode"] == "rule"
    listeners = data["listeners"]
    crawler_listeners = [l for l in listeners if l["name"].startswith("crawler-node-")]
    assert len(crawler_listeners) == 2
    assert [l["port"] for l in crawler_listeners] == [30000, 30001]
    assert [l["proxy"] for l in crawler_listeners] == ["A", "B"]


def test_inject_runtime_config_idempotent(service, tmp_clash_dir):
    """重复注入只保留最后一次 group/listener，不累加。"""
    runtime = tmp_clash_dir / "clash-verge.yaml"
    runtime.write_text("mode: rule\n", encoding="utf-8")
    service.inject_runtime_config(["A"], listener_port=30000)
    service.inject_runtime_config(["X", "Y", "Z"], listener_port=30001)
    data = _yaml.safe_load(runtime.read_text(encoding="utf-8"))
    pools = [g for g in data["proxy-groups"] if g["name"] == "crawler-pool"]
    assert len(pools) == 0
    listeners = [l for l in data["listeners"] if l["name"].startswith("crawler-node-")]
    assert len(listeners) == 3
    assert [l["port"] for l in listeners] == [30001, 30002, 30003]
    assert [l["proxy"] for l in listeners] == ["X", "Y", "Z"]


def test_inject_runtime_config_merges_with_existing_groups(service, tmp_clash_dir):
    """已有 proxy-groups 时新 group 追加，旧 group 保留。"""
    runtime = tmp_clash_dir / "clash-verge.yaml"
    runtime.write_text(
        "mode: rule\n"
        "proxy-groups:\n"
        "- name: existing\n"
        "  type: select\n"
        "  proxies: [a]\n",
        encoding="utf-8",
    )
    service.inject_runtime_config(["A", "B"], listener_port=30000)
    data = _yaml.safe_load(runtime.read_text(encoding="utf-8"))
    names = [g["name"] for g in data["proxy-groups"]]
    assert "existing" in names
    assert "crawler-pool" not in names


def test_inject_runtime_config_missing_runtime(service, tmp_clash_dir):
    with pytest.raises(FileNotFoundError, match="clash-verge.yaml"):
        service.inject_runtime_config(["A"])


def test_inject_runtime_config_creates_backup(service, tmp_clash_dir):
    runtime = tmp_clash_dir / "clash-verge.yaml"
    runtime.write_text("mode: rule\n", encoding="utf-8")
    service.inject_runtime_config(["A"])
    backups = list(tmp_clash_dir.glob("clash-verge.yaml.bak.*"))
    assert len(backups) == 1


def test_strip_managed_block_no_marker(service):
    content = "mode: rule\nmixed-port: 7897\n"
    assert service._strip_managed_block(content) == content


def test_strip_managed_block_removes_section(service):
    content = (
        "mode: rule\n"
        f"{ClashService.MANAGED_BLOCK_BEGIN}\n"
        "listeners:\n"
        "- port: 30000\n"
        f"{ClashService.MANAGED_BLOCK_END}\n"
        "extra: tail\n"
    )
    out = service._strip_managed_block(content)
    assert "listeners" not in out
    assert "extra: tail" in out
    assert "mode: rule" in out
