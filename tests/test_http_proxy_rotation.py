import httpx as real_httpx
import pytest

from app.crawler.base import BaseCrawler


class DummyCrawler(BaseCrawler):
    async def crawl(self, *args, **kwargs):
        return None


@pytest.mark.asyncio
async def test_request_http_reselects_proxy_for_each_request(monkeypatch):
    crawler = DummyCrawler()
    selected_proxy_ids = []
    created_clients = []

    proxies = [
        {"id": 1, "ip": "127.0.0.1", "port": 60001, "source": "direct"},
        {"id": 2, "ip": "127.0.0.1", "port": 60002, "source": "direct"},
    ]

    async def fake_get_proxy(exclude_ids=None):
        proxy = proxies[len(selected_proxy_ids)]
        selected_proxy_ids.append(proxy["id"])
        crawler._current_proxy_info = proxy
        return proxy

    async def fake_probe(_proxy):
        return True

    class FakeResponse:
        status_code = 200
        text = "ok"
        cookies = real_httpx.Cookies()

        @property
        def headers(self):
            return real_httpx.Headers({})

    class FakeAsyncClient:
        def __init__(self, *args, cookies=None, proxy=None, **kwargs):
            self.cookies = cookies or real_httpx.Cookies()
            self.incoming_seen = self.cookies.get("seen")
            self.proxy = proxy
            created_clients.append(self)

        async def request(self, method, url, **kwargs):
            self.cookies.set("seen", str(len(created_clients)), domain="www.letpub.com.cn", path="/")
            return FakeResponse()

        async def aclose(self):
            return None

    monkeypatch.setattr(crawler, "_get_proxy_from_pool", fake_get_proxy)
    monkeypatch.setattr(crawler, "_probe_proxy", fake_probe)
    monkeypatch.setattr("app.crawler.base.httpx.AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(crawler, "report_proxy_result", lambda success: _async_noop())

    crawler.set_http_cookie("PHPSESSID=abc")

    await crawler.request_http("GET", "https://www.letpub.com.cn/a", throttle=False)
    await crawler.request_http("GET", "https://www.letpub.com.cn/b", throttle=False)

    assert selected_proxy_ids == [1, 2]
    assert len(created_clients) == 2
    assert created_clients[1].cookies.get("PHPSESSID") == "abc"
    assert created_clients[1].incoming_seen == "1"


async def _async_noop():
    return None
