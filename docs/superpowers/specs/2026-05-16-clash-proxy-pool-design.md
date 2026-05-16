# Clash Verge 本机代理池接入设计

- **日期**：2026-05-16
- **分支**：`feature/clash-proxy-pool`
- **方案**：C（mihomo `load-balance` group + 单 listener）
- **备用方案文档**：[2026-05-16-clash-proxy-pool-alt-multi-listener.md](./2026-05-16-clash-proxy-pool-alt-multi-listener.md)

## 1. 背景与目标

爬虫频繁被 LetPub 封 IP。已有外部代理服务（kuaidaili 等）成本高且节点质量参差。本机已运行 Clash Verge Rev，订阅含 70+ 节点（trojan/vless）。

**目标**：复用本机 Clash 节点作为代理池，让爬虫请求随机出自不同节点，降低封 IP 概率，无需外部代理服务。

**硬约束**：

- 不影响本机正常上网（系统流量、规则、selector 全不动）
- Verge profile 不被改写
- 节点订阅更新流程不被破坏

## 2. 可行性分析

### 2.1 Clash Verge Rev 内核能力

Verge Rev 内核为 mihomo（Clash.Meta），原生支持：

- 顶层 `listeners:` 段：每项绑一本地端口，固定 outbound 到指定 proxy 或 group
- `load-balance` proxy-group：内核 round-robin / consistent-hashing / sticky-sessions 多策略分发
- Verge `Merge.yaml` 机制：profile 之上的纯追加层，订阅更新不覆盖

### 2.2 关键决策点

- **不能动 selector / proxy-groups**：会污染系统流量。否决「Clash API 切 selector」路径。
- **不能改 profile yaml**：订阅更新会覆盖。Merge.yaml 是唯一安全注入点。
- **节点身份不重要**：用户明言只关心拿到数据，不关心是哪个节点。→ load-balance 黑盒分发完全够用，无需逐节点 listener。

### 2.3 结论

可行。方案 C（load-balance + 单 listener）以最少配置变更达成目标。

## 3. 架构

```
┌─────────────────────────────────────────────┐
│ Clash Verge Rev (mihomo core)               │
│  ├ mixed-port: 7897         ← 系统流量(不动) │
│  ├ proxy-groups (原)        ← 系统选节点(不动) │
│  └ Merge.yaml 注入                          │
│     ├ proxy-groups[+]: crawler-pool         │
│     │    type=load-balance, strategy=rr     │
│     │    proxies: [全量 N 节点]              │
│     └ listeners[+]: 127.0.0.1:30000         │
│          proxy: crawler-pool                │
└─────────────────────────────────────────────┘
            ▲                    ▲
    (CLI 写 Merge.yaml)     (爬虫走 30000)
            │                    │
┌───────────┴──────────┐  ┌──────┴──────────┐
│ tools/sync_clash.py  │  │ BaseCrawler     │
│  读 profile          │  │  /api/proxies/  │
│  抽 proxies 名       │  │   random        │
│  写 Merge.yaml       │  │ → 127.0.0.1:    │
│  PUT /configs?force  │  │      30000      │
│  upsert ProxyPool    │  │ (source=clash)  │
│   (1 条聚合)         │  └─────────────────┘
└──────────────────────┘
```

**核心承诺**：mixed-port、原 proxy-groups、rules、selector 全不改。Merge 纯追加 1 个 group + 1 个 listener。系统流量行为零变。

## 4. 组件分解

### 4.1 新增

- `tools/sync_clash.py` — CLI 同步脚本入口
- `app/services/clash_service.py` — Clash Verge 集成层
- `migrations/XXX_add_clash_proxy_index.sql` — 给 ProxyPool 加 source 索引（可选）

### 4.2 改动

- `app/services/proxy_service.py` — `report_proxy_result` 对 `source='clash'` 跳过失败惩罚
- `app/config.py` — 新增 Clash 相关配置项
- `.env.example` — 同步示例配置

### 4.3 零改

- `BaseCrawler.init_browser` 代理注入逻辑（Playwright 接 `127.0.0.1:30000` 即可）
- `/api/proxies/random` 端点
- `ProxyPool` 表结构

### 4.4 `app/services/clash_service.py` 接口

```python
class ClashService:
    def __init__(self, profile_dir: Path, controller: str, secret: str): ...

    def get_current_profile_path(self) -> Path:
        """读 profiles.yaml current uid，返回对应 profile yaml 绝对路径"""

    def extract_proxy_names(self, profile_path: Path) -> list[str]:
        """yaml.safe_load + 取 proxies[*].name，去重"""

    def render_merge_yaml(
        self,
        proxy_names: list[str],
        listener_port: int = 30000,
        group_name: str = "crawler-pool",
    ) -> str:
        """构造 Merge.yaml 内容字符串，首行含 managed-by 标记"""

    def write_merge(self, content: str) -> Path:
        """原子写 profiles/Merge.yaml；非托管旧文件备份 .bak.<ts>"""

    async def reload_via_api(self) -> bool:
        """PUT /configs?force=true，返回是否成功"""
```

### 4.5 `tools/sync_clash.py` 流程

```
入: --dry-run? --profile <path>?
流:
  1. ClashService.get_current_profile_path()
  2. extract_proxy_names() → N 节点名列表
  3. render_merge_yaml(names, port=30000)
  4. write_merge(content)
  5. await reload_via_api()
     失败 → 提示用户 Verge UI 手动重载
  6. upsert ProxyPool:
       UPDATE source='clash' SET is_active=false
       INSERT (127.0.0.1, 30000, http, source='clash',
               remark=f'clash load-balance: {N} nodes')
出: 控制台报「同步 N 节点完成。listener=127.0.0.1:30000」
```

### 4.6 ProxyService 改动点

`report_proxy_result(proxy_id, success)` 入口加判：

```python
proxy = db.query(ProxyPool).filter_by(id=proxy_id).first()
if proxy and proxy.source == 'clash':
    proxy.last_used_at = datetime.utcnow()
    if success:
        proxy.success_count += 1
    db.commit()
    return
# 原有逻辑（fail_count++、is_valid=false 等）走非 clash 路径
```

### 4.7 配置（`.env` 增）

```
CLASH_PROFILE_DIR=/home/cc/.local/share/io.github.clash-verge-rev.clash-verge-rev
CLASH_CONTROLLER=http://127.0.0.1:9097
CLASH_SECRET=set-your-secret
CLASH_LISTENER_PORT=30000
CLASH_GROUP_NAME=crawler-pool
```

## 5. Merge.yaml 模板

```yaml
# managed-by: letpub-crawler
# 自动生成，请勿手动修改。重生方式：uv run python tools/sync_clash.py
proxy-groups:
  - name: crawler-pool
    type: load-balance
    strategy: round-robin
    url: https://www.gstatic.com/generate_204
    interval: 300
    lazy: true
    proxies:
      - "🇯🇵 上海电信转日本NTT[M][Trojan][倍率:1]"
      - "🇺🇸 美国BGP[M][Trojan][倍率:0.6]"
      # ... 全量 N 项

listeners:
  - name: crawler-lb
    type: mixed
    port: 30000
    listen: 127.0.0.1
    proxy: crawler-pool
```

## 6. 数据流

### 6.1 同步流（CLI 触发）

```
$ uv run python tools/sync_clash.py
  → 读 profiles.yaml.current
  → 加载 profile yaml
  → 抽 proxies[*].name
  → 渲染 Merge.yaml
  → 备份原 Merge.yaml（若非托管）
  → 原子写 Merge.yaml
  → PUT mihomo /configs?force=true
  → upsert ProxyPool（source=clash）
  → 报告 N 节点同步完成
```

### 6.2 爬取流（每任务）

```
worker 拉任务
  → crawler.init_browser()
  → _get_proxy_from_pool() → /api/proxies/random
  → 返 {ip:127.0.0.1, port:30000, source:clash}
  → Playwright launch(proxy={server:"http://127.0.0.1:30000"})
  → page.goto(url)
     - TCP → 127.0.0.1:30000
     - mihomo listener 接收
     - 派 crawler-pool group → round-robin 一节点
     - 出该节点 → 目标站
  → 成功 → report_proxy_result(success=True) [仅累计 success_count]
  → 失败 → 既有自动切直连重试（base.py:177）
```

## 7. 错误处理

| 场景 | 处理 |
|---|---|
| profiles.yaml 缺失 / current uid 无对应 file | CLI 报错退出，不写 Merge，不动 ProxyPool |
| profile yaml 无 proxies 栏 | CLI 报「节点 0 个」退出 |
| Merge.yaml 已存且无 managed-by 标记 | 备份 → `Merge.yaml.bak.<ts>` → 全量覆写并加标记头 |
| Merge.yaml 已存且含管理标记 | 直覆盖，无 .bak |
| mihomo API 返 401 | secret 错，提示从 `config.yaml` 抄 secret 到 `.env` |
| mihomo API 返 5xx / 网络错 | log warn，提示「Merge 已写，请 Verge UI 点 profile 重载」 |
| Verge 未启动（端口 9097 不通）| 同上，CLI 提示启动 Verge |
| listener 30000 被占 | mihomo 启动 log error 但不 crash；CLI 重载后自检：`httpx.get` 经 30000 → 失败则告警 |
| 重复执行 CLI | 幂等：UPDATE 旧 source=clash 条目下架 + INSERT 新；Merge 直覆 |
| 节点全部失活（mihomo 健检全红）| Playwright 连 30000 → 连超时 → 既有自动切直连逻辑生效 |

## 8. 测试方案

### 8.1 单元测试（无 Verge 依赖）

`tests/test_clash_service.py`：

| 用例 | 验 |
|---|---|
| `test_extract_proxy_names_basic` | yaml fixture 5 节点 → 返 5 个 name |
| `test_extract_proxy_names_empty_proxies` | proxies: [] → 返 [] |
| `test_extract_proxy_names_missing_key` | 无 proxies key → raise ValueError |
| `test_extract_proxy_names_dedupe` | 含重名 → 去重 |
| `test_render_merge_yaml_structure` | 输出可被 yaml.safe_load，含 proxy-groups + listeners |
| `test_render_merge_yaml_managed_header` | 输出首行含 `managed-by: letpub-crawler` |
| `test_render_merge_yaml_listener_port` | port=30000、listen=127.0.0.1、proxy=group_name |
| `test_render_merge_yaml_group_proxies` | group.proxies 完全等于入参列表，顺序保留 |
| `test_get_current_profile_path_resolves_uid` | profiles.yaml current=X，items[uid=X].file=Y → 返 `<dir>/profiles/Y` |
| `test_get_current_profile_path_no_match` | current 无对应 item → raise FileNotFoundError |
| `test_write_merge_creates_backup_when_unmanaged` | 已存非托管 → 备份 .bak.<ts> 后覆写 |
| `test_write_merge_overwrites_managed` | 已存且托管 → 直覆，无 .bak |
| `test_write_merge_atomic` | 写过程中断不留半文件（temp + rename）|

`tests/test_proxy_service_clash_source.py`：

| 用例 | 验 |
|---|---|
| `test_report_clash_success_increments_only_success` | source=clash 成功 → success_count++，fail_count 不动 |
| `test_report_clash_failure_no_penalty` | source=clash 失败 → fail_count 不变、is_valid 仍 True |
| `test_report_non_clash_failure_penalizes` | source=manual 失败 → 走原逻辑 |
| `test_get_random_returns_clash_when_only_source` | 池中只 1 条 source=clash → random 必中 |

### 8.2 集成测试（需 Verge 运行）

`tests/integration/test_clash_e2e.py`，pytest mark `@pytest.mark.integration`，CI 默认 skip。

| 用例 | 验 |
|---|---|
| `test_sync_cli_full_flow` | CLI 跑 → Merge.yaml + mihomo 重载 200 + ProxyPool 有 source=clash |
| `test_listener_reachable` | 同步后 `httpx.get("https://www.gstatic.com/generate_204", proxies="http://127.0.0.1:30000")` → 204 |
| `test_listener_round_robin` | 连 5 次经 30000 访 ipify → 至少 2 个不同 IP |
| `test_system_proxy_unaffected` | 重载前后经 mixed-port 7897 访 ipify → IP 不变 |
| `test_reload_api_auth_failure` | secret 错 → reload_via_api 返 False，Merge 仍写成 |
| `test_idempotent_resync` | 连跑 CLI 两次 → ProxyPool 仍 1 条 active source=clash |

### 8.3 手动验收

```
[ ] Verge UI 显示原 profile 节点数 = 同步前
[ ] Verge UI selector 仍可手动切节点
[ ] 浏览器经系统代理访 ipify → 看到 selector 当选节点 IP
[ ] curl -x http://127.0.0.1:30000 https://api.ipify.org → 成功
[ ] 连发 10 次上述 curl → 出现多个出口 IP
[ ] 跑 worker 一个任务 → 日志见 [代理] 从代理池获取: 127.0.0.1:30000
[ ] 杀 Verge → 跑任务 → 日志见 直连重试 → 任务仍可成功
[ ] 重启 Verge → 跑 CLI 重新同步 → 恢复走 30000
```

### 8.4 手动回滚

```bash
cp Merge.yaml.bak.<ts> Merge.yaml
# Verge UI 点 profile 重载
psql -c "UPDATE proxy_pool SET is_active=false WHERE source='clash'"
```

## 9. 范围边界

**包含**：

- CLI 同步工具
- ClashService 集成层
- ProxyService source=clash 分支
- 配置项 + 文档
- 单元测试 + 集成测试

**不包含**（YAGNI）：

- Web UI 触发同步按钮（CLI 已够；如需可后续加）
- 节点级监控（load-balance 黑盒，mihomo 内部探活已够）
- 多 Verge 实例支持
- Windows / macOS 路径自适配（项目本机为 Linux Fedora）
- Verge 自动重启（用户须保持 Verge 在线）

## 10. 风险与缓解

| 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|
| mihomo 版本不支持 listeners 段 | 低 | 高 | Verge Rev 当前版默认含；启动后查 mihomo 日志 |
| Merge 合并行为差异（旧版 Verge）| 低 | 中 | 已读现 Verge 4.x merge 机制；CLI 备份原文件兜底 |
| Playwright HTTP/2 连接复用使节点变化弱 | 中 | 低 | 既有 BrowserContext 任务级重建；用户已接受 |
| 节点全部被封同时段 | 低 | 高 | 既有自动切直连兜底；Cookie 池补偿 |
| Verge 升级破坏 listener 字段名 | 低 | 中 | 集成测试覆盖；版本变更时人工验 |

## 11. 后续演进点（不在本次范围）

- 若 round-robin 不够散，可换 `consistent-hashing`（按 URL hash 黏节点）或 `sticky-sessions`
- 若需 IP 多样性强约束，切方案 A（多 listener，每节点独立端口；备援文已备）
- 若 Web UI 需触发同步，加 `POST /api/proxies/sync-clash` 端点包装 CLI 逻辑
- 若 Verge 重启后 listener 失活率高，加 worker 端心跳健康检查 30000
