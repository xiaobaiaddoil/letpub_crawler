# 功能增强总结

## 1. 任务管理增强

### 新增 API 端点

#### Worker 管理
- `GET /api/tasks/workers` - 获取活跃的 worker 列表
  - 显示最近5分钟内活动的 worker
  - 包含每个 worker 的任务数量和最后活跃时间

#### 任务操作
- `POST /api/tasks/{task_id}/re-crawl` - 重新爬取任务（无论当前状态）
  - 重置任务为待处理状态
  - 清除错误信息和重试计数

- `POST /api/tasks/batch-retry` - 批量重试失败任务
  - 参数：`task_ids` (数组)
  - 返回成功重试的任务数量

### 任务响应增强
- 添加 `worker_id` 字段 - 显示执行该任务的 worker 标识
- 添加 `locked_at` 字段 - 显示任务锁定时间

### 使用示例
```bash
# 获取活跃的 workers
curl http://localhost:8000/api/tasks/workers

# 重新爬取任务
curl -X POST http://localhost:8000/api/tasks/123/re-crawl

# 批量重试任务
curl -X POST http://localhost:8000/api/tasks/batch-retry \
  -H "Content-Type: application/json" \
  -d '{"task_ids": [1, 2, 3]}'
```

---

## 2. 期刊数据管理增强

### 搜索功能
支持多字段模糊搜索：
- 期刊名称
- ISSN
- E-ISSN

### 分页响应
新增 `JournalListResponse` 包含：
- `total` - 总记录数
- `page` - 当前页码
- `size` - 每页大小
- `items` - 期刊列表

### 响应字段增强
`JournalResponse` 新增字段：
- `impact_factor_realtime` - 实时影响因子
- `self_citation_rate` - 自引率
- `cas_warning` - CAS预警信息
- `citescore` - CiteScore评分
- `category_id` - 分类ID

### 评论数据增强
`CommentResponse` 新增字段：
- `comment_time` - 评论时间
- `submit_experience` - 投稿经历

### 使用示例
```bash
# 搜索期刊（支持名称/ISSN）
curl "http://localhost:8000/api/data/journals?search=nature&page=1&size=20"

# 按分类筛选
curl "http://localhost:8000/api/data/journals?category_id=1&page=1"

# 获取期刊评论
curl "http://localhost:8000/api/data/journals/12345/comments?page=1&size=20"

# 导出期刊数据（CSV）
curl "http://localhost:8000/api/data/export/journals?format=csv" > journals.csv
```

---

## 3. 日志系统配置化

### 配置文件支持
- 创建 `logging.conf` 配置文件（预留，当前使用代码配置）
- 支持通过环境变量控制日志级别

### 新增环境变量
在 `.env` 文件中配置：

```bash
# 调试模式（生成DEBUG日志文件）
DEBUG=false

# 控制台日志级别（控制控制台输出的最低级别）
CONSOLE_LOG_LEVEL=WARNING  # 可选: DEBUG/INFO/WARNING/ERROR
```

### 日志级别说明

#### 文件日志（始终启用）
- `logs/info_YYYYMMDD.log` - INFO 及以上
- `logs/warning_YYYYMMDD.log` - WARNING 及以上
- `logs/error_YYYYMMDD.log` - ERROR 及以上
- `logs/debug_YYYYMMDD.log` - DEBUG 级别（仅当 DEBUG=true）

#### 控制台日志（可配置）
- `CONSOLE_LOG_LEVEL=DEBUG` - 显示所有日志（开发环境）
- `CONSOLE_LOG_LEVEL=INFO` - 显示 INFO 及以上（测试环境）
- `CONSOLE_LOG_LEVEL=WARNING` - 仅显示警告和错误（生产环境，默认）
- `CONSOLE_LOG_LEVEL=ERROR` - 仅显示错误

### 优势
1. **开发环境**：设置 `DEBUG=true` + `CONSOLE_LOG_LEVEL=INFO` 查看详细信息
2. **生产环境**：设置 `DEBUG=false` + `CONSOLE_LOG_LEVEL=WARNING` 减少干扰
3. **灵活性**：无需修改代码，仅通过环境变量调整

---

## 4. 数据库迁移

### 新增迁移脚本
- `migrations/002_add_journal_fields.sql` - 添加期刊新字段
- `migrations/003_fix_comment_rating_type.sql` - 修复评论评分类型
- `migrations/fix_all_types.sql` - 综合修复脚本（推荐使用）

### 运行迁移
```bash
# 方式1：使用 psql
psql -h 192.168.0.110 -U postgre -d letpub_crawler -f migrations/fix_all_types.sql

# 方式2：使用 Python 脚本（如果创建了）
python run_migrations.py
```

---

## 5. 前端集成建议

### 任务管理页面
```html
<!-- 显示 Worker 信息 -->
<div class="workers-panel">
  <h3>活跃Workers</h3>
  <div id="workers-list"></div>
</div>

<!-- 任务列表显示 worker_id -->
<td>{{ task.worker_id or '-' }}</td>
<td>{{ task.locked_at or '-' }}</td>

<!-- 批量操作 -->
<button onclick="batchRetry()">批量重试选中任务</button>
<button onclick="reCrawl(taskId)">重新爬取</button>
```

### 期刊列表页面
```html
<!-- 搜索框 -->
<input type="text" placeholder="搜索期刊名称或ISSN"
       onchange="searchJournals(this.value)">

<!-- 分类筛选 -->
<select onchange="filterByCategory(this.value)">
  <option value="">全部分类</option>
  <!-- 动态加载分类 -->
</select>

<!-- 分页控件 -->
<div class="pagination">
  <span>共 {{ total }} 条</span>
  <button onclick="goToPage(page - 1)">上一页</button>
  <span>第 {{ page }} 页</span>
  <button onclick="goToPage(page + 1)">下一页</button>
</div>

<!-- 显示新字段 -->
<td>{{ journal.impact_factor_realtime or '-' }}</td>
<td>{{ journal.citescore or '-' }}</td>
<td>{{ journal.cas_warning or '-' }}</td>
```

### JavaScript 示例
```javascript
// 获取活跃 workers
async function loadWorkers() {
  const response = await fetch('/api/tasks/workers');
  const data = await response.json();
  // 渲染 workers 列表
}

// 搜索期刊
async function searchJournals(keyword) {
  const response = await fetch(
    `/api/data/journals?search=${keyword}&page=1&size=20`
  );
  const data = await response.json();
  // 显示 data.items，更新分页信息（data.total, data.page）
}

// 批量重试
async function batchRetry() {
  const selectedIds = getSelectedTaskIds(); // 获取选中的任务ID
  await fetch('/api/tasks/batch-retry', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({task_ids: selectedIds})
  });
}
```

---

## 配置示例

### 开发环境 `.env`
```bash
DEBUG=true
CONSOLE_LOG_LEVEL=INFO
DATABASE_URL=postgresql://postgre:123456@localhost:5432/letpub_crawler
CRAWLER_AUTO_START=false
```

### 生产环境 `.env`
```bash
DEBUG=false
CONSOLE_LOG_LEVEL=WARNING
DATABASE_URL=postgresql://postgre:password@192.168.0.110:5432/letpub_crawler
CRAWLER_AUTO_START=true
WORKER_ID=server1-prod
```

---

## API 完整列表

### 任务管理
- `GET /api/tasks` - 任务列表（支持筛选）
- `GET /api/tasks/stats` - 任务统计
- `GET /api/tasks/workers` - 活跃 workers
- `POST /api/tasks/{task_id}/retry` - 重试单个任务
- `POST /api/tasks/{task_id}/re-crawl` - 重新爬取
- `POST /api/tasks/retry-all-failed` - 重试所有失败任务
- `POST /api/tasks/batch-retry` - 批量重试
- `DELETE /api/tasks/{task_id}` - 删除任务
- `DELETE /api/tasks/completed` - 清理已完成任务

### 数据管理
- `GET /api/data/stats` - 数据统计
- `GET /api/data/categories` - 分类列表
- `GET /api/data/journals` - 期刊列表（支持搜索、筛选、分页）
- `GET /api/data/journals/{journal_id}` - 期刊详情
- `GET /api/data/journals/{journal_id}/comments` - 期刊评论
- `GET /api/data/export/journals` - 导出期刊数据
