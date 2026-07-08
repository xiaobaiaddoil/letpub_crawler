# Clash 代理池接入

复用本机 Clash Verge Rev 节点池作为爬虫代理来源，无需外部代理服务。

## 工作原理

```
爬虫 ──→ http://127.0.0.1:30000 ──→ mihomo listener ──→ crawler-pool (load-balance) ──→ 节点 N
                                          │
系统流量 ──→ 127.0.0.1:7897 (mixed-port) ──→ 原 selector ──→ 节点 X    （互不影响）
```

`tools/sync_clash.py` 读 Verge 当前 profile，把节点名注入 mihomo 启动配置 `clash-verge.yaml`：
- 在 `proxy-groups` 中追加 `crawler-pool`（type=load-balance, strategy=round-robin）
- 顶层加 `listeners` 段，绑 `127.0.0.1:30000` → `crawler-pool`
- 调 mihomo external-controller `PUT /configs?force=true` 触发重载
- ProxyPool 写入一条聚合条目（source=clash），爬虫透过 `/api/proxies/random` 取得

## 前置条件

- Clash Verge Rev 已安装并运行
- 当前 profile 含可用节点（≥2 个，否则 round-robin 无效）
- mihomo external-controller 可访问（unix socket 或 HTTP，见下）

## 配置

编辑 `config/app.yaml`，启用 clash 段：

```yaml
clash:
  enabled: true
  profile_dir: "/home/<user>/.local/share/io.github.clash-verge-rev.clash-verge-rev"
  # controller 支持两种形式：
  #   unix:///tmp/verge/verge-mihomo.sock          (Verge 默认)
  #   http://127.0.0.1:9097                        (须在 Verge 设置中开启 TCP controller)
  controller: "unix:///tmp/verge/verge-mihomo.sock"
  secret: "set-your-secret"
  listener_port: 30000
  group_name: "crawler-pool"
```

### 定位 Verge 数据目录

```bash
# Linux (Clash Verge Rev)
ls ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/

# 应见 clash-verge.yaml / config.yaml / profiles/ / profiles.yaml
```

### 取 secret

`secret` 抄自 Verge 数据目录下 `clash-verge.yaml`（mihomo 实际启动文件）的 `secret:` 字段。无需修改 Verge 设置。

```bash
grep '^secret:' ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml
```

### 取 controller socket 路径

```bash
grep -E '^external-controller(-unix)?:' ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml
```

若 `external-controller-unix:` 有值（如 `/tmp/verge/verge-mihomo.sock`），用 `unix://<path>` 形式；若 `external-controller:` 为 `host:port` 形式，用 `http://host:port`。

## 同步

```bash
uv run python tools/sync_clash.py
```

输出示例：

```
2026-05-16 21:40:56,170 [INFO] HTTP Request: PUT http://localhost/configs?force=true "HTTP/1.1 204 No Content"
2026-05-16 21:40:56,171 [INFO] mihomo 已重载配置
同步完成: 58 节点。listener=127.0.0.1:30000。reload=成功。ProxyPool id=259
```

### 长期守护

推荐在宿主机长期运行 watcher。它会在以下情况自动重跑同步：

- `profiles.yaml` 或当前 profile 文件变化（订阅更新、切换 profile）
- `clash-verge.yaml` 中的 `crawler-pool` / `crawler-lb` 被覆盖
- `127.0.0.1:30000` listener 不可达

```bash
DATABASE_URL=postgresql://letpub:letpub_password@127.0.0.1:15432/letpub_crawler_v2 \
uv run python tools/sync_clash.py \
  --profile-dir ~/.local/share/io.github.clash-verge-rev.clash-verge-rev \
  --controller unix:///tmp/verge/verge-mihomo.sock \
  --watch \
  --interval 10
```

watcher 启动时默认先同步一次，之后每 10 秒检测一次。若不想启动时立即同步，可追加 `--no-sync-on-start`。

### 何时需重跑

- Verge 订阅更新（节点列表变化）
- 在 Verge UI 切换 profile（Verge 会重生成 `clash-verge.yaml`，覆盖我们的注入）
- 重启 Verge

### 工作机制（重要）

本工具**直接修改 mihomo 启动配置 `clash-verge.yaml`**（在原 proxy-groups 追加 crawler-pool + 顶层 listeners 段），**不走** Verge 的 profile chain merge 机制（实测 chain merge 会过滤 `listeners` 字段）。

每次执行均备份原文件至 `clash-verge.yaml.bak.<unix-ts>`。

## 验证

```bash
# 1. listener 端口已开
ss -tlnp 2>/dev/null | grep ":30000"
# 期望见: LISTEN 0 4096 127.0.0.1:30000 0.0.0.0:*

# 2. 经 listener 出口可达
curl -sx http://127.0.0.1:30000 https://api.ipify.org

# 3. 连发 5 次，验证 round-robin
for i in 1 2 3 4 5; do
  curl -sx http://127.0.0.1:30000 https://api.ipify.org
  echo ""
done
# 期望见: 多个不同 IP

# 4. 系统流量未受影响（出口 IP 应为 selector 选中节点，与上不同段）
curl -sx http://127.0.0.1:7897 https://api.ipify.org
```

## 数据库迁移

ProxyPool 加 `(source, is_active, is_valid)` 复合索引（加速 source=clash 查询）：

```bash
# 有 psql:
psql -h <host> -U <user> -d letpub_crawler -f docs/db/202605160001_clash_proxy_index.sql

# 无 psql（如纯 Python 环境）:
uv run python -c "
import psycopg2
conn = psycopg2.connect(host='localhost', port=5432, user='myuser', password='<pwd>', dbname='letpub_crawler2')
conn.autocommit = True
with open('docs/db/202605160001_clash_proxy_index.sql') as f:
    conn.cursor().execute(f.read())
print('OK')
"
```

## 安全

- listener 默认绑 `127.0.0.1`，仅本机可访问，**不对 LAN 暴露**
- 若需跨机使用，须自行加防火墙白名单 + 调整 `listen` 字段为 LAN IP（不推荐）
- `secret` 是 mihomo controller 鉴权令牌，勿提交至公共仓库（已 .gitignore `config/app.yaml`）

## 故障排查

| 现象 | 处理 |
|---|---|
| `mihomo API 鉴权失败 (401)` | `clash.secret` 与 `clash-verge.yaml` 中 `secret:` 不一致，重抄 |
| `调 mihomo API 失败（ConnectError）` | controller 路径错；按上节"取 controller socket 路径"重核 |
| `mihomo 自动重载失败` | 在 Verge UI 手动点选当前 profile 触发重载 |
| `curl -x 30000` 超时 | 节点全失活；查 Verge UI 节点延迟，或重启 Verge 后重跑 CLI |
| `mapping key "proxy-groups" already defined` | 旧版工具 bug，已修。若仍见，从备份恢复 `clash-verge.yaml` 后重跑 |
| ss 未见 30000 监听 | mihomo 未读到 listeners；用户手动切了 profile，重跑 CLI |
| 出口 IP 始终为单节点 | HTTP keep-alive 黏单连接所致；爬虫已在 BaseCrawler 任务级重建 BrowserContext，正常使用无影响 |
| 测试 round-robin 仅见 1 IP | 同上；测试代码须每次新建 `httpx.AsyncClient` |

### 查 mihomo 日志

```bash
tail -f ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/logs/service/service_latest.log
```

## 回滚

```bash
# 1. 还原 clash-verge.yaml
ls -t ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml.bak.* | head -1
cp <最新备份> ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml

# 2. 在 Verge UI 重新点选 profile（强制 Verge 重生成 clash-verge.yaml）

# 3. 下架 ProxyPool 条目
psql -d letpub_crawler -c "UPDATE proxy_pool SET is_active=false WHERE source='clash';"

# 4. 关闭 config/app.yaml 中 clash.enabled
```

## 相关文档

- 主设计（方案 C, load-balance）: [`docs/superpowers/specs/2026-05-16-clash-proxy-pool-design.md`](./superpowers/specs/2026-05-16-clash-proxy-pool-design.md)
- 备用方案（方案 A, 多 listener）: [`docs/superpowers/specs/2026-05-16-clash-proxy-pool-alt-multi-listener.md`](./superpowers/specs/2026-05-16-clash-proxy-pool-alt-multi-listener.md)
- 实施计划: [`docs/superpowers/plans/2026-05-16-clash-proxy-pool.md`](./superpowers/plans/2026-05-16-clash-proxy-pool.md)
