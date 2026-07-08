from app.services.proxy_runtime import httpx_proxy_url, proxy_display, runtime_proxy_host


def test_runtime_proxy_host_keeps_loopback_without_override(monkeypatch):
    monkeypatch.delenv("HOST_PROXY_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST_PROXY_HOST", raising=False)
    assert runtime_proxy_host("127.0.0.1") == "127.0.0.1"


def test_runtime_proxy_host_maps_loopback_with_override(monkeypatch):
    monkeypatch.setenv("HOST_PROXY_HOST", "host.docker.internal")
    info = {"ip": "127.0.0.1", "port": 30000, "protocol": "http"}

    assert runtime_proxy_host("127.0.0.1") == "host.docker.internal"
    assert proxy_display(info) == "127.0.0.1:30000->host.docker.internal:30000"
    assert httpx_proxy_url(info) == "http://host.docker.internal:30000"


def test_httpx_proxy_url_escapes_auth(monkeypatch):
    monkeypatch.delenv("HOST_PROXY_HOST", raising=False)
    info = {
        "ip": "proxy.example.com",
        "port": 8080,
        "protocol": "http",
        "username": "user@example.com",
        "password": "p@ss word",
    }

    assert httpx_proxy_url(info) == "http://user%40example.com:p%40ss%20word@proxy.example.com:8080"
