"""Clash Verge Rev 集成服务 - 读 profile / 写 Merge / 调 mihomo API。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import yaml

logger = logging.getLogger(__name__)


class ClashService:
    """封装 Clash Verge profile 读取与 Merge.yaml 注入。"""

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
        """从 profile yaml 抽取 proxies[*].name，去重保序。"""
        with open(profile_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "proxies" not in data:
            raise ValueError(
                f"profile {profile_path} 缺少 'proxies' 字段"
            )

        proxies = data["proxies"] or []
        seen = set()
        names: List[str] = []
        for item in proxies:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
        return names

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
