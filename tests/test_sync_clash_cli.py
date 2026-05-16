"""sync_clash CLI 编排测。"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tools.sync_clash import run_sync


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_run_sync_full_flow(tmp_clash_dir, in_memory_db, monkeypatch):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\nitems:\n  - uid: AAA\n    file: AAA.yaml\n"
    )
    profile_yaml = (FIXTURES / "sample_profile.yaml").read_text(encoding="utf-8")
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text(profile_yaml)
    # 模拟 Verge 已生成 mihomo 启动文件
    (tmp_clash_dir / "clash-verge.yaml").write_text(
        "mode: rule\nmixed-port: 7897\n"
    )

    reload_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(
        "tools.sync_clash._get_db_session",
        lambda: in_memory_db,
    )

    with patch.object(
        __import__(
            "app.services.clash_service", fromlist=["ClashService"]
        ).ClashService,
        "reload_via_api",
        reload_mock,
    ):
        result = await run_sync(
            profile_dir=str(tmp_clash_dir),
            controller="http://127.0.0.1:9097",
            secret="x",
            listener_port=30000,
            group_name="crawler-pool",
        )

    assert result["nodes"] == 3
    assert result["runtime_path"].endswith("clash-verge.yaml")
    assert result["reload_ok"] is True
    assert result["proxy_id"]

    runtime = tmp_clash_dir / "clash-verge.yaml"
    assert runtime.exists()
    content = runtime.read_text(encoding="utf-8")
    assert "letpub-crawler" in content
    assert "节点A" in content
    assert "crawler-pool" in content
    assert "listeners:" in content

    from app.models.proxy_pool import ProxyPool
    rows = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).all()
    assert len(rows) == 1
    assert rows[0].port == 30000


@pytest.mark.asyncio
async def test_run_sync_reload_failure_still_writes_runtime(
    tmp_clash_dir, in_memory_db, monkeypatch,
):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\nitems:\n  - uid: AAA\n    file: AAA.yaml\n"
    )
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text(
        (FIXTURES / "sample_profile.yaml").read_text(encoding="utf-8")
    )
    (tmp_clash_dir / "clash-verge.yaml").write_text("mode: rule\n")

    reload_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "tools.sync_clash._get_db_session",
        lambda: in_memory_db,
    )

    with patch.object(
        __import__(
            "app.services.clash_service", fromlist=["ClashService"]
        ).ClashService,
        "reload_via_api",
        reload_mock,
    ):
        result = await run_sync(
            profile_dir=str(tmp_clash_dir),
            controller="http://127.0.0.1:9097",
            secret="x",
        )

    assert result["reload_ok"] is False
    runtime = tmp_clash_dir / "clash-verge.yaml"
    assert "letpub-crawler" in runtime.read_text(encoding="utf-8")
    from app.models.proxy_pool import ProxyPool
    assert in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).count() == 1
