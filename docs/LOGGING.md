# 日志系统说明

## 日志文件位置

所有日志文件存储在 `logs/` 目录下，自动创建。

## 日志文件分类

日志系统按级别和日期分别存储：

```
logs/
├── info_20260106.log       # INFO级别及以上的所有日志
├── warning_20260106.log    # WARNING级别及以上的日志
├── error_20260106.log      # ERROR级别及以上的日志（包含完整堆栈）
└── debug_20260106.log      # DEBUG级别的详细日志（仅在DEBUG=true时生成）
```

## 日志级别说明

### 控制台输出
- **彩色显示**：不同级别使用不同颜色
- **级别**：INFO 及以上
- **格式**：`时间 - 模块名 - 级别 - 消息`

### 文件输出

1. **INFO 日志** (`info_*.log`)
   - 记录所有正常运行信息
   - 包含爬虫进度、任务状态等
   - 最大 10MB，保留 5 个备份

2. **WARNING 日志** (`warning_*.log`)
   - 记录警告信息（非致命错误）
   - 如：解析失败、重试等
   - 最大 10MB，保留 5 个备份

3. **ERROR 日志** (`error_*.log`)
   - 记录错误和异常
   - 包含完整堆栈跟踪
   - 最大 10MB，保留 10 个备份

4. **DEBUG 日志** (`debug_*.log`)
   - 仅在 `DEBUG=true` 时生成
   - 包含详细的调试信息
   - 最大 20MB，保留 3 个备份

## 配置方法

在 `.env` 文件中设置：

```bash
# 启用调试模式（生成DEBUG日志）
DEBUG=true

# 生产环境（仅INFO及以上）
DEBUG=false
```

## 日志轮转

- **自动轮转**：文件达到大小限制时自动创建新文件
- **自动清理**：启动时自动删除 7 天前的日志文件
- **备份命名**：`info_20260106.log.1`, `info_20260106.log.2` ...

## 使用示例

### 在代码中使用日志

```python
import logging

logger = logging.getLogger(__name__)

# 不同级别的日志
logger.debug("调试信息：变量值 = %s", value)
logger.info("任务已完成，耗时 %d 秒", duration)
logger.warning("解析失败，将重试: %s", url)
logger.error("数据库连接失败: %s", error)

# 记录异常（包含完整堆栈）
try:
    risky_operation()
except Exception as e:
    logger.exception("操作失败")
```

### 查看日志

```bash
# 查看最新的INFO日志
tail -f logs/info_20260106.log

# 查看ERROR日志
tail -f logs/error_20260106.log

# 搜索特定关键词
grep "期刊名称" logs/info_20260106.log

# 查看所有错误
cat logs/error_*.log
```

## 日志格式

### 控制台格式
```
14:30:25 - app.crawler - INFO - 爬取任务开始
```

### 文件格式
```
2026-01-06 14:30:25 - app.crawler.detail_crawler - INFO - [detail_crawler.py:100] - 提取基本信息: Journal Name, 共 15 个字段
```

## 注意事项

1. **日志文件大小**：单个文件最大 10-20MB，超过自动轮转
2. **保留策略**：
   - 旧备份文件自动删除（保留最近5-10个）
   - 旧日期文件自动删除（保留最近7天）
3. **性能影响**：DEBUG日志会显著增加日志量，生产环境建议关闭
4. **敏感信息**：不要在日志中记录密码、Cookie等敏感信息

## 自定义配置

如需修改日志配置，编辑 `app/logging_config.py`：

```python
# 修改日志保留天数
clean_old_logs(days=30)  # 保留30天

# 修改文件大小限制
info_handler = RotatingFileHandler(
    info_file,
    maxBytes=50 * 1024 * 1024,  # 改为50MB
    backupCount=10,             # 保留10个备份
)
```

## 故障排查

### 日志文件未生成
- 检查 `logs/` 目录权限
- 确认应用有写入权限

### 日志文件过大
- 增加轮转频率（减小 maxBytes）
- 减少 backupCount
- 缩短日志保留天数

### 找不到日志
- 检查日志文件命名（按日期命名）
- 确认是否启用了对应级别的日志
