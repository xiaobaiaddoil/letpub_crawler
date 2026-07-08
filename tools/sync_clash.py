"""CLI: 读 Clash Verge profile，注入爬虫 listener，触发重载，写 ProxyPool。

用法:
    uv run python tools/sync_clash.py
    uv run python tools/sync_clash.py --listener-port 30000
    uv run python tools/sync_clash.py --watch --interval 10
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
from pathlib import Path
from typing import Optional

import yaml
from app.config import config
from app.database import SessionLocal
from app.services.clash_service import ClashService

logger = logging.getLogger(__name__)


def _get_db_session():
    return SessionLocal()


def listener_reachable(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return True when the local mihomo listener accepts TCP connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def runtime_has_crawler_listener(
    profile_dir: str | Path,
    listener_port: int,
    group_name: str,
    runtime_filename: str = "clash-verge.yaml",
) -> bool:
    """Check whether Verge runtime config still contains our managed entry."""
    runtime = Path(profile_dir) / runtime_filename
    if not runtime.exists():
        return False

    try:
        data = yaml.safe_load(runtime.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False

    groups = data.get("proxy-groups") or []
    listeners = data.get("listeners") or []

    has_group = any(
        isinstance(group, dict) and group.get("name") == group_name
        for group in groups
    )
    has_listener = any(
        isinstance(listener, dict)
        and listener.get("name") == "crawler-lb"
        and int(listener.get("port") or 0) == listener_port
        and listener.get("proxy") == group_name
        for listener in listeners
    )
    return has_group and has_listener


def _file_signature(path: Path) -> tuple[str, int, int]:
    try:
        stat = path.stat()
    except OSError:
        return (str(path), 0, -1)
    return (str(path), stat.st_mtime_ns, stat.st_size)


def current_profile_signature(profile_dir: str | Path) -> tuple[tuple[str, int, int], ...]:
    """Signature for files that change when Verge updates or switches profile."""
    root = Path(profile_dir)
    svc = ClashService(profile_dir=root, controller="", secret="")
    profile_path = svc.get_current_profile_path()
    return (
        _file_signature(root / "profiles.yaml"),
        _file_signature(profile_path),
    )


def sync_reason(
    profile_dir: str | Path,
    listener_port: int,
    group_name: str,
    previous_profile_signature: tuple[tuple[str, int, int], ...] | None,
) -> tuple[str | None, tuple[tuple[str, int, int], ...] | None]:
    """Return a reason to sync, or None when runtime still looks healthy."""
    try:
        signature = current_profile_signature(profile_dir)
    except FileNotFoundError as exc:
        return f"Clash profile 不可用: {exc}", None

    if previous_profile_signature is not None and signature != previous_profile_signature:
        return "Clash profile 已更新或已切换", signature

    if not runtime_has_crawler_listener(profile_dir, listener_port, group_name):
        return "clash-verge.yaml 缺少 crawler listener", signature

    if not listener_reachable("127.0.0.1", listener_port):
        return f"listener 127.0.0.1:{listener_port} 不可达", signature

    return None, signature


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


async def watch_sync(
    profile_dir: str,
    controller: str,
    secret: str,
    listener_port: int = 30000,
    group_name: str = "crawler-pool",
    interval: float = 10.0,
    sync_on_start: bool = True,
) -> None:
    """Long-running guard: resync after Verge rewrites config or port vanishes."""
    previous_signature: tuple[tuple[str, int, int], ...] | None = None

    if sync_on_start:
        logger.info("watcher 启动，先执行一次 Clash 同步")
        try:
            await run_sync(
                profile_dir=profile_dir,
                controller=controller,
                secret=secret,
                listener_port=listener_port,
                group_name=group_name,
            )
            previous_signature = current_profile_signature(profile_dir)
        except Exception:
            logger.exception("watcher 启动同步失败，稍后重试")

    while True:
        reason, signature = sync_reason(
            profile_dir=profile_dir,
            listener_port=listener_port,
            group_name=group_name,
            previous_profile_signature=previous_signature,
        )
        if reason:
            logger.info("触发 Clash 重同步: %s", reason)
            try:
                await run_sync(
                    profile_dir=profile_dir,
                    controller=controller,
                    secret=secret,
                    listener_port=listener_port,
                    group_name=group_name,
                )
                previous_signature = current_profile_signature(profile_dir)
            except Exception:
                logger.exception("Clash 重同步失败，%.1f 秒后重试", interval)
                previous_signature = signature
        else:
            previous_signature = signature

        await asyncio.sleep(interval)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="同步 Clash Verge 节点池到爬虫代理池")
    parser.add_argument("--profile-dir", default=None, help="覆盖 config.CLASH_PROFILE_DIR")
    parser.add_argument("--controller", default=None, help="覆盖 mihomo external-controller")
    parser.add_argument("--secret", default=None, help="覆盖 controller secret")
    parser.add_argument("--listener-port", type=int, default=None)
    parser.add_argument("--group-name", default=None)
    parser.add_argument("--watch", action="store_true", help="长期运行，检测配置更新或 listener 失效后自动重同步")
    parser.add_argument("--interval", type=float, default=10.0, help="watch 模式检测间隔秒数")
    parser.add_argument("--no-sync-on-start", action="store_true", help="watch 启动时不立即同步")
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
        if args.watch:
            print(
                f"启动 Clash 同步 watcher: listener=127.0.0.1:{listener_port}, "
                f"interval={args.interval}s"
            )
            asyncio.run(watch_sync(
                profile_dir=profile_dir,
                controller=controller,
                secret=secret,
                listener_port=listener_port,
                group_name=group_name,
                interval=args.interval,
                sync_on_start=not args.no_sync_on_start,
            ))
            return 0

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
    except KeyboardInterrupt:
        print("已停止 Clash 同步 watcher")
        return 0

    print(
        f"同步完成: {result['nodes']} 节点。"
        f"listener=127.0.0.1:{listener_port}。"
        f"reload={'成功' if result['reload_ok'] else '失败（需手动重载）'}。"
        f"ProxyPool id={result['proxy_id']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
