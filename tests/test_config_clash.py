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
    assert cfg.CLASH_LISTENER_PORT == 60000
    assert cfg.CLASH_GROUP_NAME == "crawler-pool"
    assert cfg.HOST_PROXY_HOST == ""


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


def test_host_proxy_host_reads_yaml_and_env(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    write_yaml(tmp_path / "app.yaml", {"proxy": {"host_proxy_host": "host.docker.internal"}})
    write_yaml(tmp_path / "proxy.yaml", {})

    cfg = Config()
    assert cfg.HOST_PROXY_HOST == "host.docker.internal"

    monkeypatch.setenv("HOST_PROXY_HOST", "172.17.0.1")
    assert cfg.HOST_PROXY_HOST == "172.17.0.1"


def test_crawler_fetch_mode_defaults_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CRAWLER_FETCH_MODE", raising=False)
    write_yaml(tmp_path / "app.yaml", {})
    write_yaml(tmp_path / "proxy.yaml", {})

    cfg = Config()
    assert cfg.CRAWLER_FETCH_MODE == "http"

    write_yaml(tmp_path / "app.yaml", {"crawler": {"fetch_mode": "browser"}})
    cfg = Config()
    assert cfg.CRAWLER_FETCH_MODE == "browser"

    write_yaml(tmp_path / "app.yaml", {"crawler": {"fetch_mode": "invalid"}})
    cfg = Config()
    assert cfg.CRAWLER_FETCH_MODE == "http"

    monkeypatch.setenv("CRAWLER_FETCH_MODE", "browser")
    assert cfg.CRAWLER_FETCH_MODE == "browser"


def test_direct_fallback_and_proxy_wait_config(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    monkeypatch.delenv("CRAWLER_ALLOW_DIRECT_FALLBACK", raising=False)
    monkeypatch.delenv("PROXY_UNAVAILABLE_SLEEP_SECONDS", raising=False)
    write_yaml(tmp_path / "app.yaml", {
        "crawler": {
            "allow_direct_fallback": False,
            "proxy_unavailable_sleep_seconds": 90,
        },
    })
    write_yaml(tmp_path / "proxy.yaml", {})

    cfg = Config()
    assert cfg.CRAWLER_ALLOW_DIRECT_FALLBACK is False
    assert cfg.PROXY_UNAVAILABLE_SLEEP_SECONDS == 90

    monkeypatch.setenv("CRAWLER_ALLOW_DIRECT_FALLBACK", "true")
    monkeypatch.setenv("PROXY_UNAVAILABLE_SLEEP_SECONDS", "15")
    assert cfg.CRAWLER_ALLOW_DIRECT_FALLBACK is True
    assert cfg.PROXY_UNAVAILABLE_SLEEP_SECONDS == 15


def test_env_overrides_yaml(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    monkeypatch.setenv("RUN_MODE", "master")
    monkeypatch.setenv("CRAWLER_AUTO_START", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql://env_user:env_pass@db:5432/env_db")
    monkeypatch.setenv("PARALLEL_WORKERS", "7")
    monkeypatch.setenv("COMMENT_PARALLEL_WORKERS", "2")
    monkeypatch.setenv("COMMENT_DELAY_MIN", "2.5")
    monkeypatch.setenv("COMMENT_DELAY_MAX", "6.5")
    monkeypatch.setenv("LETPUB_COOKIE", "a=b")
    write_yaml(tmp_path / "app.yaml", {
        "run_mode": "standalone",
        "crawler_auto_start": True,
        "database": {
            "host": "localhost",
            "port": 5432,
            "name": "yaml_db",
            "user": "yaml_user",
            "password": "yaml_pass",
        },
        "crawler": {"parallel_workers": 3},
    })
    write_yaml(tmp_path / "proxy.yaml", {})

    cfg = Config()

    assert cfg.RUN_MODE == "master"
    assert cfg.CRAWLER_AUTO_START is False
    assert cfg.DATABASE_URL == "postgresql://env_user:env_pass@db:5432/env_db"
    assert cfg.PARALLEL_WORKERS == 7
    assert cfg.COMMENT_PARALLEL_WORKERS == 2
    assert cfg.COMMENT_DELAY_MIN == 2.5
    assert cfg.COMMENT_DELAY_MAX == 6.5
    assert cfg.LETPUB_COOKIE == "a=b"
