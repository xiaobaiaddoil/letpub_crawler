"""Unit tests for ClashService."""
import pytest
from pathlib import Path

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
