# 备用方案：Clash Verge 多 listener 逐节点接入

- **日期**：2026-05-16
- **状态**：备用（已选方案 C 实施，本文档为后续技术选型可能切换的参考）
- **主方案文档**：[2026-05-16-clash-proxy-pool-design.md](./2026-05-16-clash-proxy-pool-design.md)

## 1. 何时考虑切换至本方案

主方案 C 使用 mihomo `load-balance` group 黑盒分发节点。若运行中遇到以下信号，可考虑切换至本备用方案 A：

1. **load-balance round-robin 实际散度不足**：监控显示出口 IP 多样性低于预期（譬如同一任务批次内多次撞同一节点），切 `consistent-hashing` / `sticky-sessions` 后仍不理想
2. **封 IP 仍频繁但定位不到坏节点**：load-balance 黑盒，无法判定哪个节点被站点拉黑。需要逐节点统计才能精准下架
3. **节点质量差异大需差异化策略**：如优质节点优先、低倍率节点限频。group 单一策略无法表达
4. **需要在 ProxyPool 层做精细调度**：如黏性会话（一个 worker 锁定一个节点直到任务结束）、地区路由（某些目标域必须走某地区节点）
5. **mihomo load-balance 行为受 Verge 升级破坏**：listeners.proxy 指向单节点是更基础的能力，更稳

## 2. 与方案 C 的差别（一图）

| 维度 | C（主，load-balance）| A（备，多 listener）|
|---|---|---|
| Merge.yaml 写入 | 1 group + 1 listener | 0 group + N listeners |
| 占用本地端口 | 仅 30000 | 30000-30000+N |
| ProxyPool 行数 | 1 条聚合 | N 条逐节点 |
| 节点轮换 | mihomo 内核 round-robin | 应用层 Python random |
| 健康检查 | mihomo 自动 | 应用层 success/fail 计数 |
| 节点可见性 | 黑盒 | 每节点独立可监控 / 禁用 |
| 失败追踪 | 不知具体节点 | 知具体节点 |
| ProxyService 代码 | 加 source=clash 跳过惩罚分支 | 完全复用现有评分逻辑（每条目独立计分）|
| 与现有评分系统贴合度 | 中（特例分支）| 高（无需特例）|

## 3. 架构

```
┌─────────────────────────────────────────────┐
│ Clash Verge Rev (mihomo core)               │
│  ├ mixed-port: 7897         ← 系统流量(不动) │
│  └ Merge.yaml 注入                          │
│     listeners (N 条):                       │
│      ├ 127.0.0.1:30000 → 节点1             │
│      ├ 127.0.0.1:30001 → 节点2             │
│      └ ... × N                              │
└─────────────────────────────────────────────┘
            ▲                    ▲
    (CLI 写 Merge.yaml)     (爬虫走 30000+i)
            │                    │
┌───────────┴──────────┐  ┌──────┴────────────┐
│ tools/sync_clash.py  │  │ BaseCrawler       │
│  读 profile          │  │  /api/proxies/    │
│  抽节点名            │  │   random          │
│  生成 N listeners    │  │ → 127.0.0.1:300xx │
│  写 Merge.yaml       │  │ (source=clash,    │
│  PUT /configs?force  │  │  remark=节点名)    │
│  upsert ProxyPool    │  └───────────────────┘
│   (N 条逐节点)        │
└──────────────────────┘
```

## 4. 关键差异点

### 4.1 Merge.yaml 模板

```yaml
# managed-by: letpub-crawler
listeners:
  - name: crawler-jp-ntt-1
    type: mixed
    port: 30000
    listen: 127.0.0.1
    proxy: "🇯🇵 上海电信转日本NTT[M][Trojan][倍率:1]"
  - name: crawler-us-bgp-1
    type: mixed
    port: 30001
    listen: 127.0.0.1
    proxy: "🇺🇸 美国BGP[M][Trojan][倍率:0.6]"
  # ... × N
```

无 proxy-groups 段。listener.proxy 直指节点名而非 group。

### 4.2 sync_clash.py 流程差异

```
3. render_merge_yaml(names, port_start=30000):
     for i, name in enumerate(names):
         listeners.append({
             name: f"crawler-{slugify(name)}-{i}",
             type: "mixed",
             port: 30000 + i,
             listen: "127.0.0.1",
             proxy: name,
         })

6. upsert ProxyPool:
     UPDATE source='clash' SET is_active=false
     for i, name in enumerate(names):
         INSERT (127.0.0.1, 30000+i, http, source='clash',
                 proxy_type='direct', remark=name,
                 is_active=true, is_valid=true)
```

### 4.3 ProxyService 行为

**完全复用既有逻辑**，无需 source=clash 特例分支：

- `report_proxy_result(success=False)` → `fail_count++` → 阈值后 `is_valid=false` → 自动下架坏节点
- `get_random_proxy()` → ORDER BY RANDOM() 在 N 条 source=clash 中均匀挑

ProxyPool 评分系统直接生效，每节点独立分数。

### 4.4 端口区间

30000-30099（预留 100 端口）。当前 70+ 节点占 30000-30069。

## 5. 错误处理差异

| 场景 | A 处理 |
|---|---|
| 单节点失活 | mihomo listener 仍接受连接，但 outbound 不通 → Playwright 超时 → ProxyService fail_count++ → 累积下架 |
| 节点名含特殊字符 | listener.name 需 slugify（mihomo 限定字符）；listener.proxy 字段保留原名 |
| 端口段冲突 | 启动时尝试连 30000-30000+N 探活，冲突端口 log error 提示用户调 `CLASH_LISTENER_PORT` |
| ProxyPool 旧 source=clash 与新订阅节点列表不一致 | UPDATE 全部下架 + INSERT 新；旧条目历史保留供查询 |

## 6. 测试方案差异

新增 / 调整用例：

| 用例 | 验 |
|---|---|
| `test_render_merge_yaml_one_listener_per_node` | N 节点输入 → N 个 listeners，无 proxy-groups |
| `test_render_merge_yaml_port_assignment` | 节点 i 对应 port=30000+i |
| `test_render_merge_yaml_listener_name_slugify` | 含中文 / emoji 节点名 → listener.name 合法 |
| `test_proxy_pool_upsert_per_node` | CLI 后 ProxyPool 有 N 条 source=clash，端口连续 |
| `test_listener_each_node_reachable`（集成）| 每端口经 curl → 各得稳定不同 IP |
| `test_node_failure_isolation`（集成）| 故意填错节点名 → 该端口失败 → fail_count 累计 → 自动下架 |

`test_report_clash_failure_no_penalty` **不再需要**（A 方案恢复原评分行为）。

## 7. 切换工作量估算

从 C 切换至 A 主要工作：

1. 改 `ClashService.render_merge_yaml` 渲染逻辑（约 30 行）
2. 改 `tools/sync_clash.py` 的 ProxyPool upsert（从 1 条改为循环 N 条）
3. 撤销 `ProxyService.report_proxy_result` 中的 source=clash 特例分支
4. 调整测试用例

预估半天内完成切换，含验收。

## 8. 何时不要切换

如以下条件均成立，方案 C 仍优：

- mihomo round-robin 实测散度可接受
- 封 IP 频率已显著降低
- 不需要节点级监控（业务仅关心整体成功率）
- 用户希望保持 Merge.yaml 简洁

不要为「以备万一」而切换。切换有真实成本：70 个 listener 端口监听、ProxyPool 行数膨胀、订阅更新时同步开销增加。
