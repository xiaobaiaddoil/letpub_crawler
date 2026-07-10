# Task Error Policy

任务失败先归类为稳定错误码，再由错误码决定任务动作。错误码写入
`crawl_tasks.extra_data.last_error`，不新增数据库字段。

| 错误码 | 触发条件 | 动作 | 重试计数 |
| --- | --- | --- | --- |
| `proxy_unavailable` | 代理池无可用代理 | `release`，任务退回 `pending` | 不增加 |
| `detail_quality_failed` | detail 任务详情质量检查硬失败 | `resolve_detail_id`，按期刊名搜索新 LetPub ID，失败时按分类全页兜底 | 先冻结到最大重试，解析成功后重置；无法确认时保留失败任务 |
| `comment_mismatch` | comment 任务抓到的评论数少于页面/API 总数 | `fail_retry`，保留旧评论集合，不做残缺覆盖 | 增加 |
| `data_validation_failed` | 非详情质量类数据校验失败 | `fail_retry` | 增加 |
| `http_request_failed` | httpx 请求错误或 HTTP 请求失败 | `fail_retry` | 增加 |
| `parse_error` | JSON/字段解析类错误 | `fail_retry` | 增加 |
| `unknown_error` | 其他未分类异常 | `fail_retry` | 增加 |

错误报告批量处理同样走错误码动作。`detail_missing_identity`、
`detail_low_field_count`、`detail_empty`、`detail_not_object`、
`detail_login_placeholder` 会触发 `resolve_detail_id`，不会直接用旧 ID
重置详情任务。
