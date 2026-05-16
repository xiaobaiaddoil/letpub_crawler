"""集成测试（需本机 Clash Verge 已运行 + 当前 profile 含 ≥2 节点）。

跑法:
    uv run pytest tests/integration/test_clash_e2e.py -m integration -v

CI 默认 skip。
"""
import os
import asyncio
from pathlib import Path

import httpx
import pytest

from app.config import config
from app.services.clash_service import ClashService
from tools.sync_clash import run_sync


pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def listener_port() -> int:
    return int(os.environ.get("CLASH_TEST_LISTENER_PORT", "30099"))


@pytest.fixture(scope="module")
def svc():
    if not config.CLASH_PROFILE_DIR:
        pytest.skip("config.CLASH_PROFILE_DIR 未配置")
    return ClashService(
        profile_dir=Path(config.CLASH_PROFILE_DIR),
        controller=config.CLASH_CONTROLLER,
        secret=config.CLASH_SECRET,
    )


@pytest.mark.asyncio
async def test_sync_cli_full_flow(listener_port, svc):
    """端到端: CLI 跑 → mihomo 重载成功 → listener 可达。"""
    result = await run_sync(
        profile_dir=config.CLASH_PROFILE_DIR,
        controller=config.CLASH_CONTROLLER,
        secret=config.CLASH_SECRET,
        listener_port=listener_port,
    )
    assert result["nodes"] >= 1
    assert result["reload_ok"] is True


@pytest.mark.asyncio
async def test_listener_reachable(listener_port):
    proxy = f"http://127.0.0.1:{listener_port}"
    async with httpx.AsyncClient(proxy=proxy, timeout=15.0) as client:
        resp = await client.get("https://www.gstatic.com/generate_204")
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_listener_round_robin_diverse_ips(listener_port):
    """连发多次经 listener，至少出现 2 个不同出口 IP。

    每次用新 AsyncClient（避免 HTTP keep-alive 黏单 conn → 单节点）。
    """
    proxy = f"http://127.0.0.1:{listener_port}"
    ips = set()
    for _ in range(8):
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=15.0) as client:
                resp = await client.get("https://api.ipify.org")
                if resp.status_code == 200:
                    ips.add(resp.text.strip())
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.5)
    assert len(ips) >= 2, f"出口 IP 仅 {ips}，分发未生效"


@pytest.mark.asyncio
async def test_system_proxy_unaffected():
    """经系统 mixed-port 7897 访 ipify，应仍按 selector 出。"""
    async with httpx.AsyncClient(
        proxy="http://127.0.0.1:7897", timeout=15.0,
    ) as client:
        resp = await client.get("https://api.ipify.org")
    assert resp.status_code == 200
