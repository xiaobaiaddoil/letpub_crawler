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
