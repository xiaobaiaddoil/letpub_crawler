# 分布式爬虫部署指南

## 架构概述

本系统支持分布式部署，采用主从架构：

```
┌─────────────────────────────────────────────────────────────┐
│                     PostgreSQL 数据库                        │
│                   (共享任务队列和数据)                        │
└─────────────────────────────────────────────────────────────┘
                              ▲
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
        ▼                     ▼                     ▼
┌───────────────┐     ┌───────────────┐     ┌───────────────┐
│   主服务器     │     │   Worker 1    │     │   Worker N    │
│  (Master)     │     │   (爬虫节点)   │     │   (爬虫节点)   │
│               │     │               │     │               │
│ - Web管理界面  │     │ - 监听任务     │     │ - 监听任务     │
│ - 任务创建     │     │ - 执行爬取     │     │ - 执行爬取     │
│ - 数据查看     │     │ - 心跳上报     │     │ - 心跳上报     │
│ - Worker监控   │     │               │     │               │
└───────────────┘     └───────────────┘     └───────────────┘
```

## 核心特性

- **任务去重**: 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 确保任务不会被多个Worker重复获取
- **心跳机制**: Worker定期发送心跳，主服务器可监控Worker状态
- **超时释放**: 超时未完成的任务自动释放给其他Worker
- **故障恢复**: Worker异常下线后，其任务会被自动重新分配

## 部署步骤

### 1. 准备数据库

确保PostgreSQL数据库可被所有节点访问。

```bash
# 执行迁移脚本
psql -h db-host -U postgres -d letpub_crawler -f migrations/004_add_workers_table.sql
```

### 2. 部署主服务器

主服务器只运行Web管理界面，不执行爬虫任务。

**配置文件 `.env`:**
```env
RUN_MODE=master
DATABASE_URL=postgresql://user:pass@db-host:5432/letpub_crawler
HOST=0.0.0.0
PORT=8000
CONSOLE_LOG_LEVEL=INFO
```

**启动:**
```bash
# 安装依赖
pip install -r requirements.txt

# 启动主服务器
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 3. 部署Worker节点

在多台服务器上部署Worker节点。

**配置文件 `.env`:**
```env
DATABASE_URL=postgresql://user:pass@db-host:5432/letpub_crawler
WORKER_ID=worker-01
HEARTBEAT_INTERVAL=30
BATCH_SIZE=5
CONSOLE_LOG_LEVEL=INFO
```

**安装Playwright浏览器:**
```bash
pip install -r requirements.txt
playwright install chromium
```

**启动Worker:**
```bash
python worker.py --worker-id worker-01
```

### 4. 使用systemd管理（推荐）

**主服务器 `/etc/systemd/system/letpub-master.service`:**
```ini
[Unit]
Description=LetPub Crawler Master
After=network.target postgresql.service

[Service]
Type=simple
User=crawler
WorkingDirectory=/opt/letpub_crawler
Environment=PATH=/opt/letpub_crawler/venv/bin
ExecStart=/opt/letpub_crawler/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**Worker节点 `/etc/systemd/system/letpub-worker.service`:**
```ini
[Unit]
Description=LetPub Crawler Worker
After=network.target

[Service]
Type=simple
User=crawler
WorkingDirectory=/opt/letpub_crawler
Environment=PATH=/opt/letpub_crawler/venv/bin
ExecStart=/opt/letpub_crawler/venv/bin/python worker.py --worker-id worker-01
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

**启动服务:**
```bash
sudo systemctl daemon-reload
sudo systemctl enable letpub-worker
sudo systemctl start letpub-worker
```

## 使用方法

### 启动爬取任务

1. 访问主服务器管理界面 `http://master-ip:8000`
2. 点击"开始爬取"按钮创建任务
3. Worker节点会自动获取并执行任务

### 监控Worker状态

1. 访问 `http://master-ip:8000/workers` 查看所有Worker
2. 可以看到每个Worker的:
   - 在线状态
   - 当前处理任务数
   - 累计完成/失败数
   - 最后心跳时间

### API接口

```bash
# 获取Worker列表
GET /api/workers

# 获取Worker详情
GET /api/workers/{worker_id}

# 释放Worker任务
POST /api/workers/{worker_id}/release-tasks

# 清理离线Worker
POST /api/workers/cleanup-offline

# 创建爬取任务
POST /api/crawler/start

# 获取任务统计
GET /api/tasks/stats
```

## 配置说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `RUN_MODE` | standalone | 运行模式: master/worker/standalone |
| `WORKER_ID` | 自动生成 | Worker唯一标识 |
| `TASK_LOCK_TIMEOUT` | 300 | 任务锁定超时（秒） |
| `HEARTBEAT_INTERVAL` | 30 | 心跳间隔（秒） |
| `WORKER_TIMEOUT` | 120 | Worker超时时间（秒） |
| `BATCH_SIZE` | 5 | 每次获取任务数量 |

## 故障处理

### Worker异常下线

1. 系统会在 `WORKER_TIMEOUT` 秒后自动将Worker标记为离线
2. 该Worker的运行中任务会在 `TASK_LOCK_TIMEOUT` 秒后自动释放
3. 也可以手动在管理界面释放任务

### 手动释放任务

```bash
# 通过API释放指定Worker的任务
curl -X POST http://master-ip:8000/api/workers/worker-01/release-tasks

# 清理所有离线Worker并释放其任务
curl -X POST http://master-ip:8000/api/workers/cleanup-offline
```

## 扩展Worker

只需在新服务器上：

1. 复制项目代码
2. 配置 `.env` 文件（确保使用不同的 `WORKER_ID`）
3. 安装依赖和Playwright浏览器
4. 启动 `python worker.py`

系统会自动注册新Worker并开始分配任务。

## 注意事项

1. **数据库连接**: 所有节点必须能访问同一个PostgreSQL数据库
2. **WORKER_ID**: 建议手动设置有意义的ID（如 `crawler-bj-01`）
3. **网络延迟**: Worker和数据库之间的网络延迟会影响任务获取效率
4. **Cookie同步**: 如果网站需要登录，需要在每个Worker上配置相同的Cookie
