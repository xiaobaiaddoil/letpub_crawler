# LetPub Crawler

LetPub Crawler 是一个面向 LetPub 期刊数据的分布式爬虫系统。项目提供 Web/API 主节点、PostgreSQL 持久化、独立 worker 执行节点、代理池管理、Clash 本机代理接入、任务检索和失败重试能力。

## 功能概览

- Web 管理界面：任务、worker、代理、Cookie、账号和数据查询。
- 分布式任务执行：worker 通过 PostgreSQL 领取任务，支持多节点和每节点多消费者协程。
- Docker 部署：默认 `db + app` 主节点，worker 使用独立镜像/profile 启动。
- HTTP/API 爬取：默认不启动浏览器；需要时可通过配置回退 Playwright 浏览器模式。
- 代理池：支持普通代理、带账号密码代理、批量导入导出、本机 Clash 唯一出口节点池。
- 动态代理分配：成功提高权重，失败降低权重；限流日志记录是否走代理和具体代理信息。
- 增量数据：分类页索引、期刊详情更新检测、指标变化快照。
- 运行日志：Web/API 和 worker 日志分目录持久化。

## 目录结构

```text
app/                    FastAPI 应用、爬虫、服务层、模型和页面模板
config/                 YAML 配置示例
docker/                 Docker 入口脚本和辅助脚本
docs/                   部署、分布式、代理、日志等专题文档
tests/                  单元测试和集成测试
tools/                  运维工具脚本，例如 Clash 同步
worker.py               分布式 worker 入口
docker-compose.yml      主节点和本机 worker 编排
docker-compose.worker.yml 远程 worker 独立编排
```

## 快速启动

准备配置：

```bash
cp .env.example .env
```

按需修改 `.env` 中的数据库密码、`ENCRYPTION_KEY`、端口、日志目录和 worker 并发数。默认 PostgreSQL 数据目录为：

```text
/home/cc/database/letpub_crawler_v2/postgres
```

覆盖部署已有主节点时，必须复用服务器上现有 `.env`。`docker compose up -d --build app worker` 会重建容器并接管现有服务，PostgreSQL 数据依赖 `POSTGRES_DATA_DIR` 持久化；不要执行会删除 volume 或挂载目录的命令。数据库密码需要轮换时，先在 PostgreSQL 内修改用户密码，再把 `.env` 中的 `POSTGRES_PASSWORD` 或 `DB_PASSWORD` 更新为新值后重新部署。

启动主节点：

```bash
docker compose up -d db app
```

启动主节点本机 worker：

```bash
docker compose --profile worker up -d worker
```

访问 Web：

```text
http://127.0.0.1:8000
```

常用页面：

- `/tasks`：任务管理和检索
- `/workers`：worker 状态
- `/proxies`：代理池管理
- `/data`：期刊数据查询

## Worker 配置

worker 默认并发数为 `4`，可通过环境变量调整：

```env
PARALLEL_WORKERS=4
LOCAL_WORKER_CONCURRENCY=4
COMMENT_PARALLEL_WORKERS=1
```

默认爬取方式为 HTTP/API，不安装浏览器，构建更轻：

```env
CRAWLER_FETCH_MODE=http
WORKER_BUILD_TARGET=worker-http
CRAWLER_ALLOW_DIRECT_FALLBACK=false
PROXY_UNAVAILABLE_SLEEP_SECONDS=60
CRAWL_DELAY_MIN=1
CRAWL_DELAY_MAX=3
COMMENT_DELAY_MIN=0.5
COMMENT_DELAY_MAX=1.5
```

详情页和评论页现在拆成独立任务。`PARALLEL_WORKERS` 处理分类、列表和详情页；`COMMENT_PARALLEL_WORKERS` 只处理评论 API。评论请求不会占用详情页 consumer，并且默认按 0.5~1.5 秒/页降频，可通过 `COMMENT_DELAY_MIN/MAX` 单独调整。

若 HTTP/API 方式临时不可用，可保留旧浏览器方式：

```env
CRAWLER_FETCH_MODE=browser
WORKER_BUILD_TARGET=worker-slim
```

切换构建 target 后需重建 worker 镜像：

```bash
docker compose --profile worker build worker
docker compose --profile worker up -d --force-recreate worker
```

本机 worker 在 `docker-compose.yml` 中使用 `host` 网络，可通过以下变量覆盖连接：

```env
WORKER_DATABASE_URL=postgresql://<db-user>:<db-password>@127.0.0.1:15432/<db-name>
WORKER_MASTER_URL=http://127.0.0.1:8000
```

远程 worker 可使用：

```bash
docker compose -f docker-compose.worker.yml up -d
```

并设置：

```env
DATABASE_URL=postgresql://<db-user>:<db-password>@<master-ip>:15432/<db-name>
MASTER_URL=http://<master-ip>:8000
CRAWLER_WORKER_ID=worker-01
PARALLEL_WORKERS=4
```

## 代理、限流与 Clash

普通代理和账号密码代理可在 `/proxies` 页面批量导入，支持格式如：

```text
host:port
host:port:username:password
username:password@host:port
http://username:password@host:port
```

代理分配由主节点 `/api/proxies/random` 完成。选择时不再纯随机，而是按动态权重抽样：

- 成功：`success_count + 1`，连续失败数下降，后续更容易被选中。
- 失败：`fail_count/total_fail_count + 1`，后续权重降低。
- 连续失败达到阈值后标记为无效；未达到阈值只降权，不立即下架。

`/proxies` 面板中的“总记录”是代理表历史记录数，包含已禁用的旧 Clash listener；worker 实际只会从“可分配”代理中取值，即启用、有效且符合当前分配策略的代理。

worker 发生 LetPub 限流时会记录代理上下文，例如：

```text
[限流] 期刊 1234 详情页触发请求过快提示，20s 后重试 (1/3)
[use_proxy=true proxy=127.0.0.1:60012 id=531 source=clash proxy_type=direct area=local-clash remark=...egress_ip=1.2.3.4]
```

若代理请求失败，worker 会先上报失败并排除该代理，再从代理池换另一个代理重试。默认 `CRAWLER_ALLOW_DIRECT_FALLBACK=false`，代理池拿不到可用代理时会释放已领取任务并休眠等待，不会继续直连打 LetPub；确需临时直连时再显式改为 `true`。调度器会优先消费新任务；当同类型没有 pending 任务时，会继续调度 `retry_count < max_retry` 的失败任务。

本机 Clash 接入采用多 listener，每个 listener 固定绑定一个 Clash 节点：

```text
consumer-0 -> http://127.0.0.1:60000 -> Clash crawler-node-0 -> 节点A
consumer-1 -> http://127.0.0.1:60001 -> Clash crawler-node-1 -> 节点B
...
```

`tools/sync_clash.py` 会读取当前 Clash Verge profile，注入 listener，重载 mihomo，并通过每个 listener 探测实际出口 IP；同一出口 IP 只保留第一条，避免订阅重复节点导致代理池虚高。同步结果会写入 `ProxyPool`，`remark` 中包含节点名和 `egress_ip`。默认 listener 起始端口为 `60000`，降低与常见服务端口冲突的概率。

同步当前 Clash profile：

```bash
DATABASE_URL=postgresql://<db-user>:<db-password>@127.0.0.1:15432/<db-name> \
uv run python tools/sync_clash.py \
  --profile-dir ~/.local/share/io.github.clash-verge-rev.clash-verge-rev \
  --controller unix:///tmp/verge/verge-mihomo.sock \
  --listener-port 60000 \
  --egress-probe-concurrency 8
```

推荐长期运行 watcher，避免 Clash 更新订阅或切换 profile 后覆盖 crawler listener：

```bash
DATABASE_URL=postgresql://<db-user>:<db-password>@127.0.0.1:15432/<db-name> \
uv run python tools/sync_clash.py \
  --profile-dir ~/.local/share/io.github.clash-verge-rev.clash-verge-rev \
  --controller unix:///tmp/verge/verge-mihomo.sock \
  --listener-port 60000 \
  --watch \
  --interval 10
```

如果刚刚手动同步完成，只想启动守护进程而不立即重新探测，可加：

```bash
--no-sync-on-start
```

如需只按 profile 静态节点同步、跳过出口 IP 探测，可加：

```bash
--no-egress-dedupe
```

更多细节见 [docs/clash-proxy-pool.md](docs/clash-proxy-pool.md)。

## 本地开发

安装依赖：

```bash
uv sync
uv sync --extra crawler
```

启动 Web：

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动 worker：

```bash
uv run python worker.py
```

运行测试：

```bash
uv run pytest
```

当前测试默认跳过需要真实 Clash Verge 环境的 `integration` 测试。

## 数据备份

示例：

```bash
mkdir -p backups
docker exec letpub-crawler-db-1 pg_dump \
  -U <db-user> \
  -d <db-name> \
  -Fc > backups/<db-name>.dump
```

实际容器名取决于 compose project 名称，可用 `docker ps` 查看。

## 相关文档

- [Docker 部署](docs/DOCKER.md)
- [分布式部署](docs/DISTRIBUTED.md)
- [Clash 代理池接入](docs/clash-proxy-pool.md)
- [日志说明](docs/LOGGING.md)
- [账号管理](docs/ACCOUNTS.md)
- [功能说明](docs/FEATURES.md)
