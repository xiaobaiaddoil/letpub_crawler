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
