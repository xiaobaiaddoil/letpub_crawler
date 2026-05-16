"""CLI: 读 Clash Verge profile，注入 Merge.yaml，触发重载，写 ProxyPool。

用法:
    uv run python tools/sync_clash.py
    uv run python tools/sync_clash.py --listener-port 30000
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

from app.config import config
from app.database import SessionLocal
from app.services.clash_service import ClashService

logger = logging.getLogger(__name__)


def _get_db_session():
    return SessionLocal()


async def run_sync(
    profile_dir: str,
    controller: str,
    secret: str,
    listener_port: int = 30000,
    group_name: str = "crawler-pool",
) -> dict:
    """同步主流程，返回结果摘要。"""
    svc = ClashService(
        profile_dir=Path(profile_dir),
        controller=controller,
        secret=secret,
    )

    profile_path = svc.get_current_profile_path()
    logger.info(f"当前 profile: {profile_path}")

    names = svc.extract_proxy_names(profile_path)
    if not names:
        raise RuntimeError(f"profile {profile_path} 无可用节点")
    logger.info(f"提取节点 {len(names)} 个")

    runtime_path = svc.inject_runtime_config(
        proxy_names=names,
        listener_port=listener_port,
        group_name=group_name,
    )
    logger.info(f"已注入运行时配置: {runtime_path}")

    reload_ok = await svc.reload_via_api(config_path=runtime_path)
    if not reload_ok:
        logger.warning(
            "mihomo 自动重载失败。请在 Verge UI 手动点选当前 profile 触发重载。"
        )

    db = _get_db_session()
    try:
        proxy_id = svc.sync_proxy_pool(
            db=db,
            node_count=len(names),
            listener_port=listener_port,
        )
    finally:
        db.close()

    return {
        "nodes": len(names),
        "runtime_path": str(runtime_path),
        "reload_ok": reload_ok,
        "proxy_id": proxy_id,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="同步 Clash Verge 节点池到爬虫代理池")
    parser.add_argument("--profile-dir", default=None, help="覆盖 config.CLASH_PROFILE_DIR")
    parser.add_argument("--controller", default=None, help="覆盖 mihomo external-controller")
    parser.add_argument("--secret", default=None, help="覆盖 controller secret")
    parser.add_argument("--listener-port", type=int, default=None)
    parser.add_argument("--group-name", default=None)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    profile_dir = args.profile_dir or config.CLASH_PROFILE_DIR
    if not profile_dir:
        print(
            "错误: 未配置 clash.profile_dir（config/app.yaml）",
            file=sys.stderr,
        )
        return 2

    controller = args.controller or config.CLASH_CONTROLLER
    secret = args.secret or config.CLASH_SECRET
    listener_port = args.listener_port or config.CLASH_LISTENER_PORT
    group_name = args.group_name or config.CLASH_GROUP_NAME

    try:
        result = asyncio.run(run_sync(
            profile_dir=profile_dir,
            controller=controller,
            secret=secret,
            listener_port=listener_port,
            group_name=group_name,
        ))
    except FileNotFoundError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1

    print(
        f"同步完成: {result['nodes']} 节点。"
        f"listener=127.0.0.1:{listener_port}。"
        f"reload={'成功' if result['reload_ok'] else '失败（需手动重载）'}。"
        f"ProxyPool id={result['proxy_id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
