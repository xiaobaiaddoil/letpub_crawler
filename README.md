# LetPub Crawler

LetPub Crawler 是一个面向 LetPub 期刊数据的分布式爬虫系统。项目提供 Web/API 主节点、PostgreSQL 持久化、独立 worker 执行节点、代理池管理、Clash 本机代理接入、任务检索和失败重试能力。

## 功能概览

- Web 管理界面：任务、worker、代理、Cookie、账号和数据查询。
- 分布式任务执行：worker 通过 PostgreSQL 领取任务，支持多节点和每节点多消费者协程。
- Docker 部署：默认 `db + app` 主节点，worker 使用独立镜像/profile 启动。
- 代理池：支持普通代理、带账号密码代理、批量导入导出、本机 Clash 节点池。
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
```

本机 worker 在 `docker-compose.yml` 中使用 `host` 网络，默认连接：

```env
WORKER_DATABASE_URL=postgresql://letpub:letpub_password@127.0.0.1:15432/letpub_crawler_v2
WORKER_MASTER_URL=http://127.0.0.1:8000
```

远程 worker 可使用：

```bash
docker compose -f docker-compose.worker.yml up -d
```

并设置：

```env
DATABASE_URL=postgresql://letpub:<password>@<master-ip>:15432/letpub_crawler_v2
MASTER_URL=http://<master-ip>:8000
CRAWLER_WORKER_ID=worker-01
PARALLEL_WORKERS=4
```

## 代理与 Clash

普通代理和账号密码代理可在 `/proxies` 页面批量导入，支持格式如：

```text
host:port
host:port:username:password
username:password@host:port
http://username:password@host:port
```

本机 Clash 接入采用一个本地入口：

```text
worker -> http://127.0.0.1:30000 -> Clash crawler-pool -> 节点池
```

同步当前 Clash profile：

```bash
DATABASE_URL=postgresql://letpub:letpub_password@127.0.0.1:15432/letpub_crawler_v2 \
uv run python tools/sync_clash.py \
  --profile-dir ~/.local/share/io.github.clash-verge-rev.clash-verge-rev \
  --controller unix:///tmp/verge/verge-mihomo.sock
```

推荐长期运行 watcher，避免 Clash 更新订阅或切换 profile 后覆盖 `127.0.0.1:30000` listener：

```bash
DATABASE_URL=postgresql://letpub:letpub_password@127.0.0.1:15432/letpub_crawler_v2 \
uv run python tools/sync_clash.py \
  --profile-dir ~/.local/share/io.github.clash-verge-rev.clash-verge-rev \
  --controller unix:///tmp/verge/verge-mihomo.sock \
  --watch \
  --interval 10
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
  -U letpub \
  -d letpub_crawler_v2 \
  -Fc > backups/letpub_crawler_v2.dump
```

实际容器名取决于 compose project 名称，可用 `docker ps` 查看。

## 相关文档

- [Docker 部署](docs/DOCKER.md)
- [分布式部署](docs/DISTRIBUTED.md)
- [Clash 代理池接入](docs/clash-proxy-pool.md)
- [日志说明](docs/LOGGING.md)
- [账号管理](docs/ACCOUNTS.md)
- [功能说明](docs/FEATURES.md)

