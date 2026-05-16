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
同步完成: 58 节点。listener=127.0.0.1:30000。reload=成功。ProxyPool id=256
```

订阅更新或在 Verge UI 切换 profile 后重跑此命令即可。

注意：本工具**直接修改 mihomo 启动配置 `clash-verge.yaml`**（在原 proxy-groups
追加 crawler-pool group + 顶层 listeners 段）。Verge UI 切 profile 时会重新生成
此文件，覆盖我们的注入，需重跑 CLI。原文件备份至 `clash-verge.yaml.bak.<ts>`。

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

## 数据库迁移

```bash
psql -h <host> -U <user> -d letpub_crawler -f docs/db/202605160001_clash_proxy_index.sql
```

## 回滚

```bash
# 备份还原
cp ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml.bak.<ts> \
   ~/.local/share/io.github.clash-verge-rev.clash-verge-rev/clash-verge.yaml

# 在 Verge UI 重新点选 profile（强制 Verge 重生成 clash-verge.yaml）

# 下架 ProxyPool 条目
psql -d letpub_crawler -c "UPDATE proxy_pool SET is_active=false WHERE source='clash';"
```

## 相关文档

- 主设计: [`docs/superpowers/specs/2026-05-16-clash-proxy-pool-design.md`](./superpowers/specs/2026-05-16-clash-proxy-pool-design.md)
- 备用方案: [`docs/superpowers/specs/2026-05-16-clash-proxy-pool-alt-multi-listener.md`](./superpowers/specs/2026-05-16-clash-proxy-pool-alt-multi-listener.md)
- 实施计划: [`docs/superpowers/plans/2026-05-16-clash-proxy-pool.md`](./superpowers/plans/2026-05-16-clash-proxy-pool.md)
