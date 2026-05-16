# Clash 代理池接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入本机 Clash Verge Rev 节点池作为爬虫代理来源，复用 mihomo `load-balance` group 让请求随机出自不同节点，不影响系统流量。

**Architecture:** CLI 脚本读 Verge profile 提取节点名 → 写入 `Merge.yaml`（注入 1 个 load-balance group + 1 个 listener）→ 触发 mihomo 重载 → upsert ProxyPool 单条聚合记录（127.0.0.1:30000, source=clash）。爬虫透明经此 listener 出口。

**Tech Stack:** Python 3.10+ / SQLAlchemy / httpx / pyyaml / pytest / mihomo external-controller API

**Spec:** `docs/superpowers/specs/2026-05-16-clash-proxy-pool-design.md`

---

## File Structure

**新增**：

- `app/services/clash_service.py` — Clash Verge 集成原子操作（读 profile / 渲染 Merge / 写文件 / 调 API）
- `tools/__init__.py` — 标记 tools 为包
- `tools/sync_clash.py` — CLI 同步入口
- `tests/__init__.py` — 已存
- `tests/test_clash_service.py` — ClashService 单测
- `tests/test_sync_clash_cli.py` — CLI 端到端逻辑测（mock 文件系统 + httpx）
- `tests/test_proxy_service_clash_source.py` — ProxyService source=clash 分支测
- `tests/conftest.py` — pytest fixtures（临时目录、内存 DB）
- `tests/fixtures/sample_profile.yaml` — 用于单测的最小 profile
- `migrations/202605160001_clash_proxy_index.sql` — ProxyPool source 索引

**修改**：

- `app/services/proxy_service.py:78-94` — `report_proxy_result` 加 source=clash 分支
- `app/config.py` — 增 Clash 相关 properties
- `config/app.example.yaml` — 增 clash 段示例
- `pyproject.toml` — dev 依赖加 pytest + pytest-asyncio
- `requirements.txt` — 同步 pytest 依赖

**零改**：

- `app/crawler/base.py` — 现有 `_get_proxy_from_pool` 与 Playwright 注入逻辑透明可用
- `app/api/proxies.py` — `/api/proxies/random` 端点
- `app/models/proxy_pool.py` — 表结构

---

## Task 1: 加 pytest 依赖与 conftest 基础

**Files:**
- Modify: `pyproject.toml`
- Modify: `requirements.txt`
- Create: `tests/conftest.py`

- [ ] **Step 1: 加 dev 依赖到 pyproject.toml**

修改 `pyproject.toml` `[dependency-groups]` 段：

```toml
[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
]
```

并在文件末尾加 pytest 配置段：

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "integration: tests requiring running Clash Verge",
]
testpaths = ["tests"]
```

- [ ] **Step 2: 同步 requirements.txt**

在 `requirements.txt` 末尾加：

```
pytest>=8.0.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 3: 安装依赖**

Run: `uv sync --group dev`
Expected: 成功，无 resolution 错误

- [ ] **Step 4: 写 conftest.py**

文件 `tests/conftest.py`：

```python
"""Pytest fixtures for clash proxy pool tests."""
import pytest
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base


@pytest.fixture
def tmp_clash_dir(tmp_path: Path) -> Path:
    """模拟 Clash Verge 数据目录结构"""
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    return tmp_path


@pytest.fixture
def in_memory_db():
    """内存 SQLite DB（用于 ProxyService 单测）"""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
```

- [ ] **Step 5: 验证 pytest 可发现**

Run: `uv run pytest --collect-only -q 2>&1 | head -20`
Expected: 输出 collected 数量 ≥0，无 ImportError

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt tests/conftest.py
git commit -m "test: add pytest deps and conftest fixtures for clash work"
```

---

## Task 2: ClashService — `extract_proxy_names` 提取节点名

**Files:**
- Create: `app/services/clash_service.py` (骨架 + 此方法)
- Create: `tests/test_clash_service.py` (此方法的测)
- Create: `tests/fixtures/sample_profile.yaml`

- [ ] **Step 1: 写 fixture 文件**

文件 `tests/fixtures/sample_profile.yaml`：

```yaml
port: 7890
mode: Rule
proxies:
  - {name: "节点A", server: a.example.com, port: 443, type: trojan, password: x}
  - {name: "节点B", server: b.example.com, port: 443, type: vless, uuid: y}
  - {name: "节点C", server: c.example.com, port: 443, type: trojan, password: z}
proxy-groups:
  - name: select
    type: select
    proxies: ["节点A", "节点B", "节点C"]
rules:
  - "MATCH,select"
```

- [ ] **Step 2: 写失败测**

文件 `tests/test_clash_service.py`：

```python
"""Unit tests for ClashService."""
import pytest
from pathlib import Path

from app.services.clash_service import ClashService


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def service(tmp_clash_dir):
    return ClashService(
        profile_dir=tmp_clash_dir,
        controller="http://127.0.0.1:9097",
        secret="test-secret",
    )


def test_extract_proxy_names_basic(service):
    names = service.extract_proxy_names(FIXTURES / "sample_profile.yaml")
    assert names == ["节点A", "节点B", "节点C"]


def test_extract_proxy_names_empty_proxies(service, tmp_path):
    p = tmp_path / "empty.yaml"
    p.write_text("proxies: []\n")
    assert service.extract_proxy_names(p) == []


def test_extract_proxy_names_missing_key(service, tmp_path):
    p = tmp_path / "no_proxies.yaml"
    p.write_text("port: 7890\nmode: Rule\n")
    with pytest.raises(ValueError, match="proxies"):
        service.extract_proxy_names(p)


def test_extract_proxy_names_dedupe(service, tmp_path):
    p = tmp_path / "dup.yaml"
    p.write_text(
        "proxies:\n"
        "  - {name: A, type: trojan, server: a, port: 1, password: x}\n"
        "  - {name: B, type: trojan, server: b, port: 1, password: x}\n"
        "  - {name: A, type: trojan, server: c, port: 1, password: x}\n"
    )
    assert service.extract_proxy_names(p) == ["A", "B"]
```

- [ ] **Step 3: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: ImportError / ModuleNotFoundError on `app.services.clash_service`

- [ ] **Step 4: 写最小实现**

文件 `app/services/clash_service.py`：

```python
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
```

- [ ] **Step 5: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py tests/fixtures/sample_profile.yaml
git commit -m "feat(clash): extract_proxy_names from Verge profile yaml"
```

---

## Task 3: ClashService — `get_current_profile_path` 解析当前 profile

**Files:**
- Modify: `app/services/clash_service.py` (加方法)
- Modify: `tests/test_clash_service.py` (加测)

- [ ] **Step 1: 加失败测**

在 `tests/test_clash_service.py` 末尾追加：

```python
def test_get_current_profile_path_resolves_uid(service, tmp_clash_dir):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\n"
        "items:\n"
        "  - uid: AAA\n"
        "    type: remote\n"
        "    file: AAA.yaml\n"
        "  - uid: BBB\n"
        "    type: remote\n"
        "    file: BBB.yaml\n"
    )
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text("proxies: []\n")
    path = service.get_current_profile_path()
    assert path == tmp_clash_dir / "profiles" / "AAA.yaml"


def test_get_current_profile_path_no_match(service, tmp_clash_dir):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: ZZZ\n"
        "items:\n"
        "  - uid: AAA\n"
        "    file: AAA.yaml\n"
    )
    with pytest.raises(FileNotFoundError, match="ZZZ"):
        service.get_current_profile_path()


def test_get_current_profile_path_no_profiles_yaml(service, tmp_clash_dir):
    with pytest.raises(FileNotFoundError, match="profiles.yaml"):
        service.get_current_profile_path()
```

- [ ] **Step 2: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py::test_get_current_profile_path_resolves_uid -v`
Expected: AttributeError on `get_current_profile_path`

- [ ] **Step 3: 写实现**

在 `app/services/clash_service.py` `ClashService` 类内追加：

```python
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
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py
git commit -m "feat(clash): resolve current profile path from profiles.yaml"
```

---

## Task 4: ClashService — `render_merge_yaml` 渲染 Merge 内容

**Files:**
- Modify: `app/services/clash_service.py`
- Modify: `tests/test_clash_service.py`

- [ ] **Step 1: 加失败测**

追加到 `tests/test_clash_service.py`：

```python
import yaml as _yaml

MANAGED_HEADER = "# managed-by: letpub-crawler"


def test_render_merge_yaml_managed_header(service):
    out = service.render_merge_yaml(["A", "B"])
    assert out.startswith(MANAGED_HEADER)


def test_render_merge_yaml_structure(service):
    out = service.render_merge_yaml(["A", "B", "C"])
    data = _yaml.safe_load(out)
    assert "proxy-groups" in data
    assert "listeners" in data
    assert len(data["proxy-groups"]) == 1
    assert len(data["listeners"]) == 1


def test_render_merge_yaml_listener_port(service):
    out = service.render_merge_yaml(["A"], listener_port=31234)
    data = _yaml.safe_load(out)
    listener = data["listeners"][0]
    assert listener["port"] == 31234
    assert listener["listen"] == "127.0.0.1"
    assert listener["type"] == "mixed"
    assert listener["proxy"] == "crawler-pool"


def test_render_merge_yaml_group_proxies(service):
    names = ["节点A", "节点B", "节点C"]
    out = service.render_merge_yaml(names)
    data = _yaml.safe_load(out)
    group = data["proxy-groups"][0]
    assert group["name"] == "crawler-pool"
    assert group["type"] == "load-balance"
    assert group["strategy"] == "round-robin"
    assert group["proxies"] == names


def test_render_merge_yaml_custom_group_name(service):
    out = service.render_merge_yaml(["A"], group_name="my-pool")
    data = _yaml.safe_load(out)
    assert data["proxy-groups"][0]["name"] == "my-pool"
    assert data["listeners"][0]["proxy"] == "my-pool"
```

- [ ] **Step 2: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py -k render -v`
Expected: AttributeError on `render_merge_yaml`

- [ ] **Step 3: 写实现**

在 `ClashService` 类内追加：

```python
    MANAGED_HEADER = "# managed-by: letpub-crawler"

    def render_merge_yaml(
        self,
        proxy_names: List[str],
        listener_port: int = 30000,
        group_name: str = "crawler-pool",
    ) -> str:
        """渲染 Merge.yaml 内容字符串，含 managed-by 标记头。"""
        body = {
            "proxy-groups": [
                {
                    "name": group_name,
                    "type": "load-balance",
                    "strategy": "round-robin",
                    "url": "https://www.gstatic.com/generate_204",
                    "interval": 300,
                    "lazy": True,
                    "proxies": list(proxy_names),
                }
            ],
            "listeners": [
                {
                    "name": "crawler-lb",
                    "type": "mixed",
                    "port": listener_port,
                    "listen": "127.0.0.1",
                    "proxy": group_name,
                }
            ],
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
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py
git commit -m "feat(clash): render Merge.yaml with load-balance group + listener"
```

---

## Task 5: ClashService — `write_merge` 原子写 + 备份

**Files:**
- Modify: `app/services/clash_service.py`
- Modify: `tests/test_clash_service.py`

- [ ] **Step 1: 加失败测**

追加到 `tests/test_clash_service.py`：

```python
import time as _time


def test_write_merge_creates_file(service, tmp_clash_dir):
    content = service.render_merge_yaml(["A"])
    path = service.write_merge(content)
    assert path == tmp_clash_dir / "profiles" / "Merge.yaml"
    assert path.read_text(encoding="utf-8") == content


def test_write_merge_overwrites_managed(service, tmp_clash_dir):
    target = tmp_clash_dir / "profiles" / "Merge.yaml"
    target.write_text(
        f"{ClashService.MANAGED_HEADER}\nold\n", encoding="utf-8"
    )
    new = service.render_merge_yaml(["A"])
    service.write_merge(new)
    assert target.read_text(encoding="utf-8") == new
    backups = list((tmp_clash_dir / "profiles").glob("Merge.yaml.bak.*"))
    assert backups == []


def test_write_merge_backups_unmanaged(service, tmp_clash_dir):
    target = tmp_clash_dir / "profiles" / "Merge.yaml"
    original = "profile:\n  store-selected: true\n"
    target.write_text(original, encoding="utf-8")
    new = service.render_merge_yaml(["A"])
    service.write_merge(new)
    assert target.read_text(encoding="utf-8") == new
    backups = list((tmp_clash_dir / "profiles").glob("Merge.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
```

- [ ] **Step 2: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py -k write_merge -v`
Expected: AttributeError on `write_merge`

- [ ] **Step 3: 写实现**

`app/services/clash_service.py` 顶部 import 加：

```python
import os
import time
```

`ClashService` 类内追加：

```python
    def write_merge(self, content: str) -> Path:
        """原子写 profiles/Merge.yaml；非托管旧文件备份后覆盖。"""
        target = self.profile_dir / "profiles" / "Merge.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if not existing.startswith(self.MANAGED_HEADER):
                ts = int(time.time())
                backup = target.with_suffix(f".yaml.bak.{ts}")
                backup.write_text(existing, encoding="utf-8")
                logger.info(f"备份原 Merge.yaml → {backup}")

        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
        return target
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 15 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py
git commit -m "feat(clash): atomic write of Merge.yaml with auto-backup"
```

---

## Task 6: ClashService — `reload_via_api` 触发 mihomo 重载

**Files:**
- Modify: `app/services/clash_service.py`
- Modify: `tests/test_clash_service.py`

- [ ] **Step 1: 加失败测（用 httpx mock transport）**

追加到 `tests/test_clash_service.py` 顶部 import：

```python
import httpx
import pytest
```

末尾追加测：

```python
@pytest.mark.asyncio
async def test_reload_via_api_success(service, tmp_clash_dir, monkeypatch):
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content.decode()
        return httpx.Response(204)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("mixed-port: 7897\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is True
    assert captured["method"] == "PUT"
    assert "/configs?force=true" in captured["url"]
    assert captured["auth"] == "Bearer test-secret"
    assert str(cfg) in captured["body"]


@pytest.mark.asyncio
async def test_reload_via_api_unauthorized(service, tmp_clash_dir, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is False


@pytest.mark.asyncio
async def test_reload_via_api_connection_error(service, tmp_clash_dir, monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        "app.services.clash_service._build_async_client",
        lambda: httpx.AsyncClient(transport=transport, timeout=5.0),
    )
    cfg = tmp_clash_dir / "config.yaml"
    cfg.write_text("x: 1\n", encoding="utf-8")
    ok = await service.reload_via_api(config_path=cfg)
    assert ok is False
```

- [ ] **Step 2: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py -k reload -v`
Expected: AttributeError on `reload_via_api` 或 `_build_async_client`

- [ ] **Step 3: 写实现**

`app/services/clash_service.py` 顶部 import 加：

```python
import httpx
from typing import Optional
```

文件末尾（类外）加工厂：

```python
def _build_async_client() -> httpx.AsyncClient:
    """工厂方法，便于测试 monkeypatch。"""
    return httpx.AsyncClient(timeout=5.0)
```

`ClashService` 类内追加：

```python
    async def reload_via_api(self, config_path: Optional[Path] = None) -> bool:
        """调 mihomo external-controller PUT /configs?force=true 触发重载。"""
        if config_path is None:
            config_path = self.profile_dir / "config.yaml"

        url = f"{self.controller}/configs?force=true"
        headers = {"Authorization": f"Bearer {self.secret}"}
        body = {"path": str(config_path)}

        try:
            client = _build_async_client()
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
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py
git commit -m "feat(clash): reload mihomo via external-controller API"
```

---

## Task 7: 配置项 — Clash 设置注入 Config

**Files:**
- Modify: `app/config.py`
- Modify: `config/app.example.yaml`
- Modify: `config/app.yaml`

- [ ] **Step 1: 在 app.example.yaml 加 clash 段**

`config/app.example.yaml` 末尾追加：

```yaml
# Clash Verge Rev 集成（让爬虫复用本机 Clash 节点池）
# 启用后，运行 `uv run python tools/sync_clash.py` 注入 Merge.yaml + 写入 ProxyPool
clash:
  enabled: false
  # Verge Rev 数据目录
  profile_dir: "/home/cc/.local/share/io.github.clash-verge-rev.clash-verge-rev"
  # mihomo external-controller
  controller: "http://127.0.0.1:9097"
  secret: "set-your-secret"
  # 注入的 listener 本地端口
  listener_port: 30000
  # crawler-pool 在 Merge 中的 group 名
  group_name: "crawler-pool"
```

同步追加同样段到 `config/app.yaml`（保持本地配置文件可用）。

- [ ] **Step 2: 在 Config 类加 properties**

`app/config.py` 在 `proxy_config` property 之前插入：

```python
    @property
    def CLASH_ENABLED(self) -> bool:
        return bool(get_nested(self._app_config, "clash", "enabled", default=False))

    @property
    def CLASH_PROFILE_DIR(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "profile_dir",
            default="",
        ) or "")

    @property
    def CLASH_CONTROLLER(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "controller",
            default="http://127.0.0.1:9097",
        ))

    @property
    def CLASH_SECRET(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "secret",
            default="",
        ) or "")

    @property
    def CLASH_LISTENER_PORT(self) -> int:
        return int(get_nested(
            self._app_config, "clash", "listener_port",
            default=30000,
        ))

    @property
    def CLASH_GROUP_NAME(self) -> str:
        return str(get_nested(
            self._app_config, "clash", "group_name",
            default="crawler-pool",
        ))
```

- [ ] **Step 3: 加 config 单测**

文件 `tests/test_config_clash.py`：

```python
"""Test Clash-related config properties."""
import yaml
from pathlib import Path

from app.config import Config


def write_yaml(p: Path, data: dict):
    p.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_clash_defaults_when_section_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    write_yaml(tmp_path / "app.yaml", {"app": {"port": 8000}})
    write_yaml(tmp_path / "proxy.yaml", {})
    cfg = Config()
    assert cfg.CLASH_ENABLED is False
    assert cfg.CLASH_PROFILE_DIR == ""
    assert cfg.CLASH_CONTROLLER == "http://127.0.0.1:9097"
    assert cfg.CLASH_LISTENER_PORT == 30000
    assert cfg.CLASH_GROUP_NAME == "crawler-pool"


def test_clash_reads_section(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.CONFIG_DIR", tmp_path)
    write_yaml(tmp_path / "app.yaml", {
        "clash": {
            "enabled": True,
            "profile_dir": "/x/y",
            "controller": "http://127.0.0.1:1234",
            "secret": "abc",
            "listener_port": 31000,
            "group_name": "g",
        },
    })
    write_yaml(tmp_path / "proxy.yaml", {})
    cfg = Config()
    assert cfg.CLASH_ENABLED is True
    assert cfg.CLASH_PROFILE_DIR == "/x/y"
    assert cfg.CLASH_CONTROLLER == "http://127.0.0.1:1234"
    assert cfg.CLASH_SECRET == "abc"
    assert cfg.CLASH_LISTENER_PORT == 31000
    assert cfg.CLASH_GROUP_NAME == "g"
```

- [ ] **Step 4: 运行测**

Run: `uv run pytest tests/test_config_clash.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add app/config.py config/app.example.yaml config/app.yaml tests/test_config_clash.py
git commit -m "feat(config): add clash section properties to Config"
```

---

## Task 8: ProxyService — source=clash 跳过失败惩罚

**Files:**
- Modify: `app/services/proxy_service.py:78-94`
- Create: `tests/test_proxy_service_clash_source.py`

- [ ] **Step 1: 写失败测**

文件 `tests/test_proxy_service_clash_source.py`：

```python
"""ProxyService 对 source=clash 的特殊处理。"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models.proxy_pool import ProxyPool
from app.services.proxy_service import ProxyService


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def _add(db, **kwargs) -> ProxyPool:
    defaults = dict(
        ip="127.0.0.1", port=30000, protocol="http",
        proxy_type="direct", source="clash",
        is_active=True, is_valid=True,
        success_count=0, fail_count=0, total_fail_count=0,
    )
    defaults.update(kwargs)
    p = ProxyPool(**defaults)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def test_clash_success_increments_success_only(db):
    p = _add(db, source="clash")
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 0
    assert p.is_valid is True


def test_clash_failure_no_penalty(db):
    p = _add(db, source="clash", fail_count=0, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 0
    assert p.is_valid is True
    assert p.is_active is True


def test_non_clash_failure_marks_invalid(db):
    p = _add(db, source="manual", fail_count=0, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=False)
    db.refresh(p)
    assert p.fail_count == 1
    assert p.total_fail_count == 1
    assert p.is_valid is False


def test_non_clash_success_resets_fail_count(db):
    p = _add(db, source="manual", fail_count=2, is_valid=True)
    ProxyService(db).report_proxy_result(p.id, success=True)
    db.refresh(p)
    assert p.success_count == 1
    assert p.fail_count == 0
```

- [ ] **Step 2: 运行测，验有失败**

Run: `uv run pytest tests/test_proxy_service_clash_source.py -v`
Expected: `test_clash_failure_no_penalty` 失败（现行代码会惩罚）

- [ ] **Step 3: 改 ProxyService**

`app/services/proxy_service.py` 中找到 `report_proxy_result` 方法（约 78 行），整体替换为：

```python
    def report_proxy_result(self, proxy_id: int, success: bool):
        """报告代理使用结果。

        source='clash' 的条目代表 mihomo load-balance 入口，
        节点健康检查由 mihomo 内核负责，应用层不打分、不下架。
        """
        proxy = self.db.query(ProxyPool).filter(ProxyPool.id == proxy_id).first()
        if not proxy:
            return

        if proxy.source == "clash":
            if success:
                proxy.success_count += 1
            self.db.commit()
            return

        if success:
            proxy.success_count += 1
            proxy.fail_count = 0
        else:
            proxy.fail_count += 1
            proxy.total_fail_count += 1
            proxy.is_valid = False
            logger.warning(f"[代理] {proxy.ip}:{proxy.port} 请求失败，已标记无效")

        self.db.commit()
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_proxy_service_clash_source.py -v`
Expected: 4 passed

- [ ] **Step 5: 跑全测确保无回归**

Run: `uv run pytest -v`
Expected: 所有现有测 + 新增测 全 pass

- [ ] **Step 6: Commit**

```bash
git add app/services/proxy_service.py tests/test_proxy_service_clash_source.py
git commit -m "feat(proxy): skip failure penalty for source=clash entries"
```

---

## Task 9: 同步流程 — `sync_proxy_pool` 业务函数

**Files:**
- Modify: `app/services/clash_service.py` (加方法 `sync_proxy_pool`)
- Modify: `tests/test_clash_service.py`

- [ ] **Step 1: 写失败测**

追加到 `tests/test_clash_service.py`：

```python
def test_sync_proxy_pool_inserts_single_entry(service, in_memory_db):
    affected = service.sync_proxy_pool(
        db=in_memory_db,
        node_count=42,
        listener_port=30000,
    )
    from app.models.proxy_pool import ProxyPool
    rows = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash"
    ).all()
    assert len(rows) == 1
    p = rows[0]
    assert p.ip == "127.0.0.1"
    assert p.port == 30000
    assert p.protocol == "http"
    assert p.is_active is True
    assert p.is_valid is True
    assert "42 nodes" in (p.remark or "")
    assert affected == 1


def test_sync_proxy_pool_idempotent(service, in_memory_db):
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30000)
    service.sync_proxy_pool(in_memory_db, node_count=15, listener_port=30000)
    from app.models.proxy_pool import ProxyPool
    active = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).count()
    inactive = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == False,
    ).count()
    assert active == 1
    assert inactive == 1


def test_sync_proxy_pool_changing_port_deactivates_old(service, in_memory_db):
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30000)
    service.sync_proxy_pool(in_memory_db, node_count=10, listener_port=30001)
    from app.models.proxy_pool import ProxyPool
    new = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).one()
    assert new.port == 30001
```

- [ ] **Step 2: 运行测，验失败**

Run: `uv run pytest tests/test_clash_service.py -k sync_proxy_pool -v`
Expected: AttributeError on `sync_proxy_pool`

- [ ] **Step 3: 写实现**

`app/services/clash_service.py` 顶部 import 加：

```python
from datetime import datetime, timezone
from sqlalchemy.orm import Session
```

`ClashService` 类内追加：

```python
    def sync_proxy_pool(
        self,
        db: "Session",
        node_count: int,
        listener_port: int = 30000,
    ) -> int:
        """同步 ProxyPool：旧 source=clash 全下架，写入 1 条聚合条目。返回新插入条目的 id。"""
        from app.models.proxy_pool import ProxyPool

        db.query(ProxyPool).filter(
            ProxyPool.source == "clash",
            ProxyPool.is_active == True,
        ).update({"is_active": False}, synchronize_session=False)

        entry = ProxyPool(
            ip="127.0.0.1",
            port=listener_port,
            protocol="http",
            proxy_type="direct",
            source="clash",
            area="local-clash",
            is_active=True,
            is_valid=True,
            success_count=0,
            fail_count=0,
            total_fail_count=0,
            remark=f"clash load-balance: {node_count} nodes",
            created_at=datetime.now(timezone.utc),
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry.id
```

- [ ] **Step 4: 运行测，验通过**

Run: `uv run pytest tests/test_clash_service.py -v`
Expected: 21 passed

- [ ] **Step 5: Commit**

```bash
git add app/services/clash_service.py tests/test_clash_service.py
git commit -m "feat(clash): sync_proxy_pool upserts aggregate clash entry"
```

---

## Task 10: CLI 入口 — `tools/sync_clash.py`

**Files:**
- Create: `tools/__init__.py`
- Create: `tools/sync_clash.py`
- Create: `tests/test_sync_clash_cli.py`

- [ ] **Step 1: 建包**

文件 `tools/__init__.py`：（空文件）

```python
```

- [ ] **Step 2: 写失败测**

文件 `tests/test_sync_clash_cli.py`：

```python
"""sync_clash CLI 编排测。"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tools.sync_clash import run_sync


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
async def test_run_sync_full_flow(tmp_clash_dir, in_memory_db, monkeypatch):
    # 准备 profiles.yaml + 节点 profile
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\nitems:\n  - uid: AAA\n    file: AAA.yaml\n"
    )
    profile_yaml = (FIXTURES / "sample_profile.yaml").read_text(encoding="utf-8")
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text(profile_yaml)

    reload_mock = AsyncMock(return_value=True)

    monkeypatch.setattr(
        "tools.sync_clash._get_db_session",
        lambda: in_memory_db,
    )

    with patch.object(
        __import__("app.services.clash_service", fromlist=["ClashService"]).ClashService,
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
    assert result["merge_path"].endswith("Merge.yaml")
    assert result["reload_ok"] is True
    assert result["proxy_id"]

    # 验证 Merge.yaml 写入了
    merge = tmp_clash_dir / "profiles" / "Merge.yaml"
    assert merge.exists()
    content = merge.read_text(encoding="utf-8")
    assert content.startswith("# managed-by: letpub-crawler")
    assert "节点A" in content
    assert "crawler-pool" in content

    # 验证 ProxyPool
    from app.models.proxy_pool import ProxyPool
    rows = in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).all()
    assert len(rows) == 1
    assert rows[0].port == 30000


@pytest.mark.asyncio
async def test_run_sync_reload_failure_still_writes_merge(
    tmp_clash_dir, in_memory_db, monkeypatch,
):
    (tmp_clash_dir / "profiles.yaml").write_text(
        "current: AAA\nitems:\n  - uid: AAA\n    file: AAA.yaml\n"
    )
    (tmp_clash_dir / "profiles" / "AAA.yaml").write_text(
        (FIXTURES / "sample_profile.yaml").read_text(encoding="utf-8")
    )

    reload_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "tools.sync_clash._get_db_session",
        lambda: in_memory_db,
    )

    with patch.object(
        __import__("app.services.clash_service", fromlist=["ClashService"]).ClashService,
        "reload_via_api",
        reload_mock,
    ):
        result = await run_sync(
            profile_dir=str(tmp_clash_dir),
            controller="http://127.0.0.1:9097",
            secret="x",
        )

    assert result["reload_ok"] is False
    assert (tmp_clash_dir / "profiles" / "Merge.yaml").exists()
    from app.models.proxy_pool import ProxyPool
    assert in_memory_db.query(ProxyPool).filter(
        ProxyPool.source == "clash",
        ProxyPool.is_active == True,
    ).count() == 1
```

- [ ] **Step 3: 运行测，验失败**

Run: `uv run pytest tests/test_sync_clash_cli.py -v`
Expected: ImportError on `tools.sync_clash`

- [ ] **Step 4: 写 CLI 实现**

文件 `tools/sync_clash.py`：

```python
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

    content = svc.render_merge_yaml(
        names,
        listener_port=listener_port,
        group_name=group_name,
    )
    merge_path = svc.write_merge(content)
    logger.info(f"Merge.yaml 已写入: {merge_path}")

    reload_ok = await svc.reload_via_api()
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
        "merge_path": str(merge_path),
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
```

- [ ] **Step 5: 运行测，验通过**

Run: `uv run pytest tests/test_sync_clash_cli.py -v`
Expected: 2 passed

- [ ] **Step 6: 运行 CLI --help 烟雾测**

Run: `uv run python tools/sync_clash.py --help`
Expected: 显示 argparse help，无 import 错

- [ ] **Step 7: Commit**

```bash
git add tools/__init__.py tools/sync_clash.py tests/test_sync_clash_cli.py
git commit -m "feat(clash): CLI tools/sync_clash.py orchestrates full sync flow"
```

---

## Task 11: 数据库迁移 — ProxyPool source 索引

**Files:**
- Create: `migrations/202605160001_clash_proxy_index.sql`

- [ ] **Step 1: 建 migrations 目录**

```bash
mkdir -p migrations
```

- [ ] **Step 2: 写迁移脚本**

文件 `migrations/202605160001_clash_proxy_index.sql`：

```sql
-- 给 ProxyPool 加 (source, is_active, is_valid) 复合索引，
-- 加速 source=clash 路径与 random 选取查询。
CREATE INDEX IF NOT EXISTS idx_proxy_pool_source_active
    ON proxy_pool (source, is_active, is_valid);
```

- [ ] **Step 3: 验证 SQL 语法（用 psql --dry-run 等价）**

若本地有 PostgreSQL，跑：

```bash
psql -h <host> -U <user> -d letpub_crawler -f migrations/202605160001_clash_proxy_index.sql
```

Expected: `CREATE INDEX` 输出，或「索引已存在」提示。无报错。

若无 PostgreSQL 环境，仅用 `sqlfluff` 或 `psql --echo-queries -X -1 -f` 校验语法即可（可跳过）。

- [ ] **Step 4: Commit**

```bash
git add migrations/202605160001_clash_proxy_index.sql
git commit -m "chore(db): add migration for proxy_pool source index"
```

---

## Task 12: 集成测试占位（手动跑，CI skip）

**Files:**
- Create: `tests/integration/__init__.py`
- Create: `tests/integration/test_clash_e2e.py`

- [ ] **Step 1: 建集成测目录**

文件 `tests/integration/__init__.py`：（空）

- [ ] **Step 2: 写集成测**

文件 `tests/integration/test_clash_e2e.py`：

```python
"""集成测试（需本机 Clash Verge 已运行 + 当前 profile 含 ≥3 节点）。

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
    """端到端: CLI 跑 → mihomo 重载成功 → listener 可达"""
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
    """连发多次经 listener，至少出现 2 个不同出口 IP"""
    proxy = f"http://127.0.0.1:{listener_port}"
    ips = set()
    async with httpx.AsyncClient(proxy=proxy, timeout=15.0) as client:
        for _ in range(8):
            try:
                resp = await client.get("https://api.ipify.org")
                if resp.status_code == 200:
                    ips.add(resp.text.strip())
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    assert len(ips) >= 2, f"出口 IP 仅 {ips}，分发未生效"


@pytest.mark.asyncio
async def test_system_proxy_unaffected():
    """经系统 mixed-port 7897 访 ipify，应仍按 selector 出"""
    async with httpx.AsyncClient(
        proxy="http://127.0.0.1:7897", timeout=15.0,
    ) as client:
        resp = await client.get("https://api.ipify.org")
    assert resp.status_code == 200
```

- [ ] **Step 3: 收集测（验证 mark 生效）**

Run: `uv run pytest tests/integration/ --collect-only -q`
Expected: 4 测被收集，但默认未带 `-m integration` 时 skip 出现

- [ ] **Step 4: Commit**

```bash
git add tests/integration/__init__.py tests/integration/test_clash_e2e.py
git commit -m "test(clash): add integration tests (skipped by default)"
```

---

## Task 13: 文档 — README 节录使用方式

**Files:**
- Modify: `CLAUDE.md`（加运行命令）
- Create: `docs/clash-proxy-pool.md`（用户向使用文档）

- [ ] **Step 1: 写用户向文档**

文件 `docs/clash-proxy-pool.md`：

```markdown
# Clash 代理池接入

复用本机 Clash Verge Rev 节点池作为爬虫代理来源，无需外部代理服务。

## 前置条件

- Clash Verge Rev 已安装并运行
- 当前 profile 含可用节点
- mihomo external-controller 端口可访问（默认 9097）

## 配置

编辑 `config/app.yaml`，启用 clash 段：

```yaml
clash:
  enabled: true
  profile_dir: "/home/<user>/.local/share/io.github.clash-verge-rev.clash-verge-rev"
  controller: "http://127.0.0.1:9097"
  secret: "<从 config.yaml 抄 secret 字段>"
  listener_port: 30000
  group_name: "crawler-pool"
```

`secret` 取自 Clash Verge 数据目录下 `config.yaml` 的 `secret:` 字段。

## 同步

```bash
uv run python tools/sync_clash.py
```

输出示例：

```
同步完成: 73 节点。listener=127.0.0.1:30000。reload=成功。ProxyPool id=42
```

订阅更新后重跑此命令即可。

## 验证

```bash
# 经 listener 出口
curl -x http://127.0.0.1:30000 https://api.ipify.org

# 连发 5 次，验证轮换
for i in 1 2 3 4 5; do
  curl -s -x http://127.0.0.1:30000 https://api.ipify.org
  echo ""
done
```

应见多个不同 IP 输出。

## 故障排查

| 现象 | 处理 |
|---|---|
| `mihomo API 鉴权失败 (401)` | 核对 `config/app.yaml` `clash.secret` 与 Verge `config.yaml` `secret` 字段 |
| `mihomo 自动重载失败` | 在 Verge UI 手动点选当前 profile 触发重载 |
| `curl -x 30000` 超时 | mihomo 节点全失活；查 Verge UI 节点延迟，或重启 Verge |
| 出口 IP 始终为本机 | listener 未生效；查 Verge 日志 `tail -f ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/logs/*.log` |

## 回滚

```bash
# 备份还原
cp ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/profiles/Merge.yaml.bak.<ts> \
   ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/profiles/Merge.yaml

# Verge UI 重新选 profile

# 下架 ProxyPool 条目
psql -d letpub_crawler -c "UPDATE proxy_pool SET is_active=false WHERE source='clash';"
```
```

- [ ] **Step 2: 在 CLAUDE.md 加命令引用**

`CLAUDE.md` `## Common Commands` 段末尾追加（紧邻 `psql` 行之后）：

```bash
# Sync local Clash Verge nodes into proxy pool
uv run python tools/sync_clash.py
```

- [ ] **Step 3: Commit**

```bash
git add docs/clash-proxy-pool.md CLAUDE.md
git commit -m "docs(clash): user guide for Clash proxy pool integration"
```

---

## Task 14: 端到端冒烟（人工，规划文档化）

**Files:**
- 无文件改动（运行验证）

- [ ] **Step 1: 启 Verge，确认运行**

```bash
pgrep -af "clash-verge\|mihomo"
```

Expected: 见进程

- [ ] **Step 2: 跑 CLI**

```bash
uv run python tools/sync_clash.py
```

Expected: 输出 `同步完成: N 节点...reload=成功`

- [ ] **Step 3: 验 Merge.yaml**

```bash
head -5 ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/profiles/Merge.yaml
```

Expected: 首行 `# managed-by: letpub-crawler`

- [ ] **Step 4: 验 listener 可达**

```bash
curl -sx http://127.0.0.1:30000 https://api.ipify.org
```

Expected: 一个 IPv4

- [ ] **Step 5: 验 ProxyPool 写入**

```bash
psql -h <host> -U <user> -d letpub_crawler2 \
  -c "SELECT id, ip, port, source, is_active, remark FROM proxy_pool WHERE source='clash';"
```

Expected: 1 行 active=t，port=30000

- [ ] **Step 6: 验 worker 拉随机代理可见 clash 条目**

```bash
curl -s http://127.0.0.1:8000/api/proxies/random
```

Expected: JSON 含 `"ip":"127.0.0.1","port":30000`

- [ ] **Step 7: 跑现有集成测（若用户愿意）**

```bash
uv run pytest tests/integration -m integration -v
```

Expected: 4 测全 pass（或 reachable 测因节点状态偶尔失败可重试）

- [ ] **Step 8: 验证系统流量未受影响**

浏览器访 ipify.org → 看到当前 selector 选中节点的 IP（不应固定为 listener 出口）。

---

## Self-Review

### Spec coverage

| Spec 章节 | 实施任务 |
|---|---|
| §3 架构 | Task 4（Merge 渲染）+ Task 10（CLI 编排）|
| §4.1 新增 sync_clash.py | Task 10 |
| §4.1 新增 clash_service.py | Tasks 2-6, 9 |
| §4.1 migrations 索引 | Task 11 |
| §4.2 改动 proxy_service.py | Task 8 |
| §4.2 改动 config.py | Task 7 |
| §4.2 改动 app.example.yaml（spec 写 .env，实际项目走 yaml）| Task 7 |
| §4.4 ClashService 接口 | Tasks 2-6, 9 一一对应 |
| §4.5 sync_clash.py 流程 | Task 10 step 4 |
| §4.6 ProxyService source=clash 分支 | Task 8 |
| §4.7 配置项 | Task 7（已转 yaml 形式）|
| §5 Merge.yaml 模板 | Task 4 |
| §6 数据流 | Task 10 编排 |
| §7 错误处理 | Tasks 3/5/6 各失败用例覆盖 |
| §8.1 单测 | Tasks 2-6, 8, 9 |
| §8.2 集成测 | Task 12 |
| §8.3 手动验收 | Task 14 |
| §8.4 回滚 | Task 13 文档化 |
| §9 范围（不含 Web UI 等）| 严格遵守，无超纲 |

### Placeholder scan

无 TBD / TODO / 「实现略」/ 「类似 Task N」 / 未给代码块的 step。

### Type 一致性

- `ClashService.__init__` 签名 (profile_dir, controller, secret) — 全任务一致
- `extract_proxy_names(profile_path) -> List[str]` — Tasks 2/10 一致
- `render_merge_yaml(proxy_names, listener_port=30000, group_name='crawler-pool')` — Tasks 4/10 一致
- `write_merge(content) -> Path` — Tasks 5/10 一致
- `reload_via_api(config_path=None) -> bool` — Tasks 6/10 一致
- `sync_proxy_pool(db, node_count, listener_port=30000) -> int` — Tasks 9/10 一致
- `run_sync(profile_dir, controller, secret, listener_port, group_name) -> dict` — Tasks 10/12 一致
- ProxyPool 字段 `source` 字符串 `'clash'` — Tasks 8/9 一致
- listener_port 默认 30000 — Tasks 4/9/10/手测一致

无型别或命名漂移。
