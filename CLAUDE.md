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

### Configuration (`.env`)

Key settings:

- `RUN_MODE`: master/worker/standalone
- `DATABASE_URL`: PostgreSQL connection (shared by all nodes)
- `WORKER_ID`: Unique identifier for worker nodes
- `TASK_LOCK_TIMEOUT`: Seconds before stuck tasks are released (default: 300)

## Database

PostgreSQL with JSONB support. Dict adapter registered in `app/database.py` for automatic dict→JSONB conversion.

Migration scripts in `migrations/` - run manually with psql.
端口触
