# Docker 部署

## 启动

```bash
cp docker/.env.example .env
# 编辑 .env，至少改 POSTGRES_PASSWORD 和 ENCRYPTION_KEY
docker compose up -d --build
```

默认 `web` 镜像基于轻量 Python 镜像，不安装 Playwright 依赖、不下载浏览器层，适合主节点 Web/API 和数据库恢复。默认 `worker` 镜像使用 `worker-slim` 目标：复用 Python 基础镜像，安装 Playwright Python 依赖和 Debian `chromium-headless-shell`，避免拉取较大的 Playwright 官方基础镜像。仍可用 `worker-playwright` 目标构建官方 Playwright 基础镜像版本。

## 构建镜像

推荐使用脚本构建，脚本会启用 BuildKit 缓存，并给镜像打一个 Git 短哈希 tag。默认只构建轻量 `web` 镜像，不下载 Playwright 浏览器基础层：

```bash
docker/build_image.sh
```

常用参数：

```bash
# 构建主节点 Web/API 镜像，下载量最小
BUILD_TARGET=web LETPUB_WEB_IMAGE=registry.example.com/letpub-crawler:web-20260707 docker/build_image.sh

# 构建 worker 镜像，默认使用 worker-slim，不拉 Playwright 官方大基础层
BUILD_TARGET=worker LETPUB_WORKER_IMAGE=registry.example.com/letpub-crawler:worker-20260707 docker/build_image.sh

# 构建基于 Playwright 官方基础镜像的 worker 版本
BUILD_TARGET=worker-playwright LETPUB_WORKER_IMAGE=registry.example.com/letpub-crawler:worker-playwright-20260707 docker/build_image.sh

# 构建后导出 tar.gz，适合没有镜像仓库时传到主节点
SAVE_TAR=1 BUILD_TARGET=web LETPUB_WEB_IMAGE=letpub-crawler:web-20260707 docker/build_image.sh

# 使用内网镜像源里的基础镜像
PYTHON_BASE_IMAGE=registry.example.com/python:3.12-slim \
PLAYWRIGHT_BASE_IMAGE=registry.example.com/playwright/python:v1.57.0-noble \
docker/build_image.sh
```

`docker compose` 也支持同样的变量：

```env
LETPUB_WEB_IMAGE=letpub-crawler:web
LETPUB_WORKER_IMAGE=letpub-crawler:worker
PYTHON_BASE_IMAGE=public.ecr.aws/docker/library/python:3.12-slim
PLAYWRIGHT_BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.57.0-noble
UV_VERSION=0.11.15
WORKER_BUILD_TARGET=worker-slim
PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium-headless-shell
```

如果主节点可以访问镜像仓库，优先推送镜像仓库；如果不能访问仓库，用 `SAVE_TAR=1` 导出后在主节点执行：

```bash
docker load < letpub-crawler_web-20260707.tar.gz
```

默认启动 `db` 和 `app`，`RUN_MODE=master` 时 Web 管理界面负责发布任务，爬取任务由 worker 容器执行。访问：

```text
http://localhost:8000
```

PostgreSQL 数据默认绑定到主节点宿主目录：

```text
/home/cc/database/letpub_crawler_v2/postgres
```

如需换目录，改 `.env`：

```env
POSTGRES_DATA_DIR=/data/letpub_crawler_v2/postgres
```

## 迁移旧 PostgreSQL 数据

当前机器找到的旧 PostgreSQL 18 数据目录是：

```text
/home/cc/database/pg/18/docker
```

迁移到 Docker Compose 的 PostgreSQL：

```bash
OLD_PGDATA=/home/cc/database/pg/18/docker \
POSTGRES_DB=letpub_crawler_v2 \
POSTGRES_USER=letpub \
POSTGRES_PASSWORD=change_me \
docker/migrate_from_old_pgdata.sh
```

脚本会复制旧数据目录到 `/tmp/letpub_pg_migration`，启动临时 PostgreSQL 18 容器导出 dump，再导入当前 compose 的 `db` 服务。默认 `RESET_TARGET=1`，会清理目标库里同名对象后恢复。

如果旧库账号或库名不是自动识别到的值，可显式指定：

```bash
SOURCE_USER=myuser SOURCE_DB=letpub_crawler2 docker/migrate_from_old_pgdata.sh
```

## 备份当前 PostgreSQL

生成可迁移备份：

```bash
docker/backup_database.sh
```

输出包含：

```text
backups/letpub_crawler_<timestamp>.dump
backups/letpub_crawler_<timestamp>.dump.list
backups/letpub_crawler_<timestamp>.dump.meta
backups/letpub_crawler_<timestamp>.dump.sha256
```

恢复到新主节点：

```bash
docker compose up -d db
docker/restore_database.sh backups/letpub_crawler_<timestamp>.dump
```

## 分布式模式

主服务只负责 Web/API：

```env
RUN_MODE=master
CRAWLER_AUTO_START=false
```

启动 worker：

```bash
docker compose --profile worker up -d --build
docker compose --profile worker up -d --scale worker=3
```

主节点也可以承担一部分 worker 职责。推荐保持 `app` 使用轻量 `web` 镜像，同时在同一台机器按需启动本地 worker 容器：

```bash
docker/start_local_worker.sh
# 或指定本机 worker 容器数量
docker/start_local_worker.sh 2
```

停止主节点本地 worker：

```bash
docker/stop_local_worker.sh
```

主节点本地 worker 可用 `.env` 控制资源占用：

```env
# 多副本本地 worker 请保持 LOCAL_WORKER_ID 为空，让容器自动生成唯一 ID。
LOCAL_WORKER_ID=
LOCAL_WORKER_CONCURRENCY=4
LOCAL_WORKER_SCALE=1
```

本机 worker 在 `docker-compose.yml` 中使用 host 网络，可通过宿主机端口连接主节点服务：

```env
WORKER_DATABASE_URL=postgresql://<db-user>:<db-password>@127.0.0.1:15432/<db-name>
WORKER_MASTER_URL=http://127.0.0.1:8000
```

这样 worker 可以访问只监听宿主机 `127.0.0.1` 的 Clash/本地代理端口。若改回非 host 网络，需要把本地代理绑定到容器可访问的地址，并设置 `HOST_PROXY_HOST`。

`app`/`web` 负责创建任务和管理界面，worker 通过同一个 PostgreSQL 使用 `FOR UPDATE SKIP LOCKED` 领取任务并执行。多台 worker 机器部署时，只要使用 worker 镜像，并把 `DATABASE_URL` 指向主节点数据库、`MASTER_URL` 指向主节点 Web 地址即可。

远程 worker 节点可以只使用 `docker-compose.worker.yml`：

```env
LETPUB_WORKER_IMAGE=letpub-crawler:worker
DATABASE_URL=postgresql://<db-user>:<db-password>@<master-ip>:15432/<db-name>
MASTER_URL=http://<master-ip>:8000
CRAWLER_WORKER_ID=worker-01
PARALLEL_WORKERS=4
```

```bash
docker compose -f docker-compose.worker.yml up -d
```

如果没有可迁移数据，启动主节点和 worker 后在 Web 页面或 API 创建爬取任务即可重新爬取：

```bash
curl -X POST http://localhost:8000/api/crawler/start
```
