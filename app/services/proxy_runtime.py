"""Runtime proxy helpers shared by crawler and proxy checks."""
from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote

from app.config import config

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def is_loopback_proxy_host(host: str | None) -> bool:
    return (host or "").strip().lower() in LOOPBACK_HOSTS


def runtime_proxy_host(host: str | None) -> str:
    raw_host = str(host or "").strip()
    if is_loopback_proxy_host(raw_host) and config.HOST_PROXY_HOST:
        return config.HOST_PROXY_HOST
    return raw_host


def proxy_display(proxy_info: Mapping[str, Any] | None) -> str:
    if not proxy_info:
        return "直连"

    host = str(proxy_info.get("ip") or "")
    port = proxy_info.get("port")
    runtime_host = runtime_proxy_host(host)
    if runtime_host and runtime_host != host:
        return f"{host}:{port}->{runtime_host}:{port}"
    return f"{host}:{port}"


def playwright_proxy_server(proxy_info: Mapping[str, Any]) -> str:
    protocol = proxy_info.get("protocol") or "http"
    return f"{protocol}://{runtime_proxy_host(proxy_info.get('ip'))}:{proxy_info.get('port')}"


def httpx_proxy_url(proxy_info: Mapping[str, Any] | None) -> str | None:
    if not proxy_info:
        return None

    protocol = proxy_info.get("protocol") or "http"
    host = runtime_proxy_host(proxy_info.get("ip"))
    port = proxy_info.get("port")
    username = proxy_info.get("username")
    password = proxy_info.get("password")
    if username and password:
        auth = f"{quote(str(username))}:{quote(str(password))}@"
    else:
        auth = ""
    return f"{protocol}://{auth}{host}:{port}"
