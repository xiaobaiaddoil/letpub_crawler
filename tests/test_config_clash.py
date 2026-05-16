"""Test Clash-related config properties."""
import yaml
from pathlib import Path

from app.config import Config


def write_yaml(p: Path, data: dict):
    p.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_clash_defaults_when_section_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    write_yaml(tmp_path / "app.yaml", {"app": {"port": 8000}})
    write_yaml(tmp_path / "proxy.yaml", {})
    cfg = Config()
    assert cfg.CLASH_ENABLED is False
    assert cfg.CLASH_PROFILE_DIR == ""
    assert cfg.CLASH_CONTROLLER == "http://127.0.0.1:9097"
    assert cfg.CLASH_LISTENER_PORT == 30000
    assert cfg.CLASH_GROUP_NAME == "crawler-pool"


def test_clash_reads_section(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    write_yaml(tmp_path / "app.yaml", {
        "clash": {
            "enabled": True,
            "profile_dir": "/x/y",
            "controller": "http://127.0.0.1:1234",
            "secret": "abc",
            "listener_port": 31000,
            "group_name": "g",
        },
    })
    write_yaml(tmp_path / "proxy.yaml", {})
    cfg = Config()
    assert cfg.CLASH_ENABLED is True
    assert cfg.CLASH_PROFILE_DIR == "/x/y"
    assert cfg.CLASH_CONTROLLER == "http://127.0.0.1:1234"
    assert cfg.CLASH_SECRET == "abc"
    assert cfg.CLASH_LISTENER_PORT == 31000
    assert cfg.CLASH_GROUP_NAME == "g"
