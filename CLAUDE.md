# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LetPub 期刊爬虫系统 - A distributed web crawler for scraping journal data from LetPub.com.cn. Built with FastAPI + SQLAlchemy + Playwright, supporting master-worker architecture for multi-server deployment.

## Common Commands

```bash
# Install uv (if not installed)
# Windows: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
# Linux/macOS: curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync
uv run playwright install chromium

# Run master server (management UI only)
RUN_MODE=master uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# Run standalone mode (UI + local crawler)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
uv run uvicorn app.main:app --host 127.0.0.1 --port 8999
# Run worker node (crawler only)
uv run python worker.py --worker-id worker-01

# Run database migrations
psql -h <host> -U <user> -d letpub_crawler -f migrations/<script>.sql

# Sync local Clash Verge nodes into proxy pool (see docs/clash-proxy-pool.md)
uv run python tools/sync_clash.py
```

## Architecture

### Distributed Mode

```
PostgreSQL (shared task queue)
       ↑
       ├── Master Server (RUN_MODE=master)
       │   └── Web UI + Task API (no crawling)
       │
       └── Worker Nodes (worker.py)
           └── Crawlers + Heartbeat
```

### Task Flow

Category tasks → List tasks → Detail tasks (with comments)

### Key Components

**Crawlers** (`app/crawler/`):

- `CategoryCrawler`: Scrapes subject categories
- `ListCrawler`: Scrapes journal lists per category/page
- `DetailCrawler`: Scrapes journal details + comments via API

**Services** (`app/services/`):

- `TaskManager`: Distributed task queue with `SELECT ... FOR UPDATE SKIP LOCKED`
- `CrawlerService`: Orchestrates crawlers in standalone mode

**Models** (`app/models/`):
端口触

- `CrawlTask`: Task queue with worker_id, locked_at for distributed locking
- `Worker`: Tracks worker nodes (heartbeat, status)
- `Journal`, `Category`, `Comment`: Data storage

### Task Locking Mechanism

Tasks use PostgreSQL row-level locking to prevent duplicate crawling:

1. `acquire_tasks()` uses `FOR UPDATE SKIP LOCKED` to atomically claim tasks
2. `TASK_LOCK_TIMEOUT` releases stuck tasks automatically
3. Workers send heartbeats; offline workers' tasks get released

### Configuration (`config/app.yaml`)

主配置走 YAML 文件（`config/app.yaml`，已 .gitignore；模板见 `config/app.example.yaml`）。`config/proxy.yaml` 存外部代理服务（kuaidaili 等）配置。

Key settings:

- `run_mode`: master/worker/standalone
- `database`: PostgreSQL connection (shared by all nodes)
- `distributed.worker_id`: Unique identifier for worker nodes
- `distributed.task_lock_timeout`: Seconds before stuck tasks are released (default: 600)
- `clash`: 本机 Clash Verge 代理池接入（见 `docs/clash-proxy-pool.md`）

### Clash 本机代理池

复用本机 Clash Verge Rev 节点池作为爬虫代理来源（不影响系统流量）：

- `tools/sync_clash.py`：CLI，读 Verge profile → 注入 mihomo 启动配置 `clash-verge.yaml` → 触发重载 → upsert ProxyPool（source=clash, 127.0.0.1:30000）
- `app/services/clash_service.py`：集成层（profile 读取 / Merge 渲染 / runtime 注入 / mihomo API）
- 爬虫 `BaseCrawler._get_proxy_from_pool()` 透过 `/api/proxies/random` 拿到 listener 地址，零改动
- ProxyService 对 `source='clash'` 跳过失败惩罚（健康检查交给 mihomo 内核）
- 详见 `docs/clash-proxy-pool.md` 与 `docs/superpowers/specs/2026-05-16-clash-proxy-pool-design.md`

## Database

PostgreSQL with JSONB support. Dict adapter registered in `app/database.py` for automatic dict→JSONB conversion.

Migration scripts in `migrations/` - run manually with psql.

新增的 SQL 脚本：`docs/db/202605160001_clash_proxy_index.sql`（ProxyPool source 复合索引；因 `migrations/` 在 .gitignore，本特性脚本暂存于 `docs/db/`）。

