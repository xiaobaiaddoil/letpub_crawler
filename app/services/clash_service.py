"""Clash Verge Rev 集成服务 - 读 profile / 写 Merge / 调 mihomo API。"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Hashable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, TYPE_CHECKING

import httpx
import yaml

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ClashService:
    """封装 Clash Verge profile 读取与 Merge.yaml 注入。"""

    MANAGED_HEADER = "# managed-by: letpub-crawler"

    def __init__(
        self,
        profile_dir: Path,
        controller: str,
        secret: str,
    ):
        self.profile_dir = Path(profile_dir)
        self.controller = controller.rstrip("/")
        self.secret = secret

    def extract_proxy_names(self, profile_path: Path) -> List[str]:
        """从 profile yaml 抽取唯一节点名，按真实出口端点和名称双重去重。"""
        with open(profile_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "proxies" not in data:
            raise ValueError(
                f"profile {profile_path} 缺少 'proxies' 字段"
            )

        proxies = data["proxies"] or []
        seen_names = set()
        seen_nodes = set()
        names: List[str] = []
        named_count = 0
        skipped_name_duplicates = 0
        skipped_node_duplicates = 0
        for item in proxies:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            named_count += 1
            name = str(name)
            if name in seen_names:
                skipped_name_duplicates += 1
                continue

            node_key = self._proxy_dedupe_key(item)
            if node_key in seen_nodes:
                skipped_node_duplicates += 1
                continue

            seen_names.add(name)
            seen_nodes.add(node_key)
            names.append(name)

        skipped = skipped_name_duplicates + skipped_node_duplicates
        if skipped:
            logger.info(
                "Clash 节点去重: 原始=%s, 唯一=%s, 同名重复=%s, 同端点重复=%s",
                named_count,
                len(names),
                skipped_name_duplicates,
                skipped_node_duplicates,
            )
        return names

    def _proxy_dedupe_key(self, proxy: dict[str, Any]) -> tuple[Any, ...]:
        """Return a stable key for one real Clash node.

        Most subscription duplicates differ only by name. For crawler load
        distribution the important dimension is the likely egress endpoint, so
        nodes sharing type/server/port are treated as one candidate.
        """
        proxy_type = proxy.get("type")
        server = proxy.get("server")
        port = proxy.get("port")
        if proxy_type and server and port is not None:
            return (
                "endpoint",
                str(proxy_type).strip().lower(),
                str(server).strip().lower(),
                self._normalize_port(port),
            )

        config = {
            key: value
            for key, value in proxy.items()
            if key != "name"
        }
        return ("config", self._freeze_config(config))

    def _normalize_port(self, value: Any) -> int | str:
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value).strip()

    def _freeze_config(self, value: Any) -> Hashable:
        if isinstance(value, dict):
            return tuple(
                sorted((str(k), self._freeze_config(v)) for k, v in value.items())
            )
        if isinstance(value, list):
            return tuple(self._freeze_config(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(self._freeze_config(item) for item in value))
        if isinstance(value, Hashable):
            return value
        return repr(value)

    def get_current_profile_path(self) -> Path:
        """读 profiles.yaml 取当前 current uid，返回对应 profile 文件绝对路径。"""
        meta = self.profile_dir / "profiles.yaml"
        if not meta.exists():
            raise FileNotFoundError(
                f"profiles.yaml 不存在: {meta}（确认 Clash Verge 已启动并加载过 profile）"
            )

        with open(meta, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        current = data.get("current")
        if not current:
            raise FileNotFoundError(
                "profiles.yaml 中无 current 字段（请在 Verge UI 选中一个 profile）"
            )

        for item in data.get("items", []) or []:
            if item.get("uid") == current:
                file = item.get("file")
                if not file:
                    raise FileNotFoundError(
                        f"profile uid={current} 缺少 file 字段"
                    )
                return self.profile_dir / "profiles" / file

        raise FileNotFoundError(
            f"profile uid={current} 在 items 中无匹配项"
        )

    def get_current_merge_file(self) -> str:
        """获取当前 profile 关联的 merge chain 文件名。

        Verge 的 Merge 机制是 per-profile：每个 remote profile 可在
        items[].option.merge 字段指定一个 uid，对应的 file 才是真正
        参与合并的 yaml。顶层 'Merge' uid 不一定生效。

        若当前 profile 未指定 option.merge，返回 'Merge.yaml'（顶层备用）。
        """
        meta = self.profile_dir / "profiles.yaml"
        if not meta.exists():
            raise FileNotFoundError(
                f"profiles.yaml 不存在: {meta}"
            )

        with open(meta, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        current_uid = data.get("current")
        items = data.get("items", []) or []

        merge_uid: Optional[str] = None
        for item in items:
            if item.get("uid") == current_uid:
                option = item.get("option") or {}
                merge_uid = option.get("merge")
                break

        if not merge_uid:
            return "Merge.yaml"

        for item in items:
            if item.get("uid") == merge_uid and item.get("type") == "merge":
                file = item.get("file")
                if file:
                    return file

        return "Merge.yaml"

    def render_merge_yaml(
        self,
        proxy_names: List[str],
        listener_port: int = 60000,
        group_name: str = "crawler-pool",
    ) -> str:
        """渲染 Merge.yaml 内容字符串，含 managed-by 标记头。"""
        listeners = [
            {
                "name": f"crawler-node-{idx}",
                "type": "mixed",
                "port": listener_port + idx,
                "listen": "127.0.0.1",
                "proxy": proxy_name,
            }
            for idx, proxy_name in enumerate(proxy_names)
        ]
        body = {
            "listeners": listeners,
        }
        body_yaml = yaml.safe_dump(
            body,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        return (
            f"{self.MANAGED_HEADER}\n"
            "# 自动生成，请勿手动修改。重生方式：uv run python tools/sync_clash.py\n"
            f"{body_yaml}"
        )

    def write_merge(self, content: str, filename: str = "Merge.yaml") -> Path:
        """原子写 profiles/<filename>；非托管旧文件备份后覆盖。"""
        target = self.profile_dir / "profiles" / filename
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if not existing.startswith(self.MANAGED_HEADER):
                ts = int(time.time())
                backup = target.with_name(f"{target.name}.bak.{ts}")
                backup.write_text(existing, encoding="utf-8")
                logger.info(f"备份原 {target.name} → {backup}")

        tmp = target.with_name(f"{target.name}.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        return target

    MANAGED_BLOCK_BEGIN = "# >>> managed-by: letpub-crawler clash listener >>>"
    MANAGED_BLOCK_END = "# <<< managed-by: letpub-crawler clash listener <<<"

    def inject_runtime_config(
        self,
        proxy_names: List[str],
        listener_port: int = 60000,
        group_name: str = "crawler-pool",
        runtime_filename: str = "clash-verge.yaml",
    ) -> Path:
        """直接修改 mihomo 启动配置（clash-verge.yaml）：在原 proxy-groups 列表
        中加入 crawler-pool group，加入顶层 listeners 字段。

        idempotent: 已有同名 group / listener 时先剥离再重写。
        副作用: Verge UI 切换 profile 会重生成此文件，需重跑 CLI。
        """
        runtime = self.profile_dir / runtime_filename
        if not runtime.exists():
            raise FileNotFoundError(
                f"Verge 运行时配置不存在: {runtime}（确认 Verge 已启动）"
            )

        existing = runtime.read_text(encoding="utf-8")
        data = yaml.safe_load(existing) or {}

        groups = data.get("proxy-groups") or []
        groups = [g for g in groups if g.get("name") != group_name]
        data["proxy-groups"] = groups

        listeners = data.get("listeners") or []
        listeners = [
            l for l in listeners
            if not str(l.get("name", "")).startswith("crawler-node-")
            and not (
                isinstance(l.get("port"), int)
                and listener_port <= int(l.get("port")) < listener_port + len(proxy_names)
            )
        ]
        for idx, proxy_name in enumerate(proxy_names):
            listeners.append(
                {
                    "name": f"crawler-node-{idx}",
                    "type": "mixed",
                    "port": listener_port + idx,
                    "listen": "127.0.0.1",
                    "proxy": proxy_name,
                }
            )
        data["listeners"] = listeners

        ts = int(time.time())
        backup = runtime.with_name(f"{runtime.name}.bak.{ts}")
        backup.write_text(existing, encoding="utf-8")

        new_content = (
            f"# Generated by Clash Verge, modified by letpub-crawler sync_clash.py\n"
            + yaml.safe_dump(
                data,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            )
        )
        tmp = runtime.with_name(f"{runtime.name}.tmp")
        tmp.write_text(new_content, encoding="utf-8")
        os.replace(tmp, runtime)
        logger.info(f"已注入运行时配置: {runtime}")
        return runtime

    def _strip_managed_block(self, content: str) -> str:
        """从 yaml 内容中剥离托管段（含 begin/end 标记之间的所有行）。

        保留以兼容历史；新版 inject_runtime_config 直接操作 yaml 结构。
        """
        if self.MANAGED_BLOCK_BEGIN not in content:
            return content
        lines = content.splitlines(keepends=True)
        out: List[str] = []
        in_block = False
        for line in lines:
            if line.startswith(self.MANAGED_BLOCK_BEGIN):
                in_block = True
                continue
            if line.startswith(self.MANAGED_BLOCK_END):
                in_block = False
                continue
            if not in_block:
                out.append(line)
        return "".join(out)

    async def reload_via_api(self, config_path: Optional[Path] = None) -> bool:
        """调 mihomo external-controller PUT /configs?force=true 触发重载。

        controller 支持两种形式：
            http://127.0.0.1:9097     标准 TCP HTTP
            unix:///tmp/verge/verge-mihomo.sock   Unix domain socket
        """
        if config_path is None:
            config_path = self.profile_dir / "config.yaml"

        if self.controller.startswith("unix://"):
            sock = self.controller[len("unix://"):]
            client_factory = lambda: _build_async_client_unix(sock)
            url = "http://localhost/configs?force=true"
        else:
            client_factory = _build_async_client
            url = f"{self.controller}/configs?force=true"

        headers = {}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        body = {"path": str(config_path)}

        try:
            client = client_factory()
            async with client:
                resp = await client.put(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            logger.warning(f"调 mihomo API 失败（{type(e).__name__}）: {e}")
            return False

        if 200 <= resp.status_code < 300:
            logger.info("mihomo 已重载配置")
            return True

        if resp.status_code == 401:
            logger.warning(
                "mihomo API 鉴权失败 (401)。请确认 controller secret 与 config.yaml 中一致。"
            )
        else:
            logger.warning(
                f"mihomo API 返 {resp.status_code}: {resp.text[:200]}"
            )
        return False

    def sync_proxy_pool(
        self,
        db: "Session",
        node_count: int,
        listener_port: int = 60000,
        proxy_names: Optional[List[str]] = None,
        egress_ips: Optional[List[Optional[str]]] = None,
    ) -> int:
        """同步 ProxyPool：旧 source=clash 全下架，按节点写入多条本地 listener。返回首条 id。"""
        from app.models.proxy_pool import ProxyPool

        if proxy_names is not None:
            node_count = len(proxy_names)
        if node_count <= 0:
            raise ValueError("node_count 必须大于 0")

        db.query(ProxyPool).filter(
            ProxyPool.source == "clash",
            ProxyPool.is_active == True,
        ).update({"is_active": False}, synchronize_session=False)

        entries = []
        now = datetime.now(timezone.utc)
        for idx in range(node_count):
            node_name = proxy_names[idx] if proxy_names and idx < len(proxy_names) else None
            egress_ip = egress_ips[idx] if egress_ips and idx < len(egress_ips) else None
            remark_parts = [f"clash node listener {idx + 1}/{node_count}"]
            if node_name:
                remark_parts.append(f"node={node_name}")
            if egress_ip:
                remark_parts.append(f"egress_ip={egress_ip}")
            entries.append(
                ProxyPool(
                    ip="127.0.0.1",
                    port=listener_port + idx,
                    protocol="http",
                    proxy_type="direct",
                    source="clash",
                    area="local-clash",
                    is_active=True,
                    is_valid=True,
                    success_count=0,
                    fail_count=0,
                    total_fail_count=0,
                    remark="; ".join(remark_parts),
                    created_at=now,
                )
            )
        db.add_all(entries)
        db.commit()
        db.refresh(entries[0])
        return entries[0].id


def _build_async_client() -> httpx.AsyncClient:
    """工厂方法，便于测试 monkeypatch。"""
    return httpx.AsyncClient(timeout=5.0)


def _build_async_client_unix(socket_path: str) -> httpx.AsyncClient:
    """通过 Unix domain socket 连接 mihomo external-controller。"""
    transport = httpx.AsyncHTTPTransport(uds=socket_path)
    return httpx.AsyncClient(transport=transport, timeout=5.0)
