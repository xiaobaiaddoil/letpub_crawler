from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from app.crawler.base import ProxyUnavailableError
from app.crawler.detail_crawler import DataValidationError
from app.models.task import CrawlTask, TaskType
from app.services.comment_refresh_service import CommentMismatchError
from app.services.journal_id_resolver_service import JournalIdResolverService
from app.services.task_manager import TaskManager


class TaskErrorCode(str, Enum):
    PROXY_UNAVAILABLE = "proxy_unavailable"
    DETAIL_QUALITY_FAILED = "detail_quality_failed"
    COMMENT_MISMATCH = "comment_mismatch"
    DATA_VALIDATION_FAILED = "data_validation_failed"
    HTTP_REQUEST_FAILED = "http_request_failed"
    PARSE_ERROR = "parse_error"
    UNKNOWN_ERROR = "unknown_error"


class TaskErrorAction(str, Enum):
    RELEASE = "release"
    FAIL_RETRY = "fail_retry"
    FAIL_STOP = "fail_stop"
    RESOLVE_DETAIL_ID = "resolve_detail_id"


@dataclass(frozen=True)
class TaskErrorPolicy:
    code: TaskErrorCode
    action: TaskErrorAction
    retryable: bool = True
    report_resource_failure: bool = True


@dataclass
class TaskErrorHandlingResult:
    code: TaskErrorCode
    action: TaskErrorAction
    message: str
    task_updated: bool = False
    resolution: dict[str, Any] | None = None
    report_resource_failure: bool = True
    count_as_failed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskErrorPolicyService:
    """Classify task failures and apply the operation tied to the error code."""

    DETAIL_QUALITY_MARKERS = (
        "详情质量检查未通过",
        "detail_missing_identity",
        "detail_low_field_count",
        "detail_empty",
        "detail_not_object",
        "detail_login_placeholder",
    )

    DETAIL_QUALITY_PROBLEM_CODES = {
        "detail_missing_identity",
        "detail_low_field_count",
        "detail_empty",
        "detail_not_object",
        "detail_login_placeholder",
    }

    POLICIES = {
        TaskErrorCode.PROXY_UNAVAILABLE: TaskErrorPolicy(
            code=TaskErrorCode.PROXY_UNAVAILABLE,
            action=TaskErrorAction.RELEASE,
            retryable=False,
            report_resource_failure=False,
        ),
        TaskErrorCode.DETAIL_QUALITY_FAILED: TaskErrorPolicy(
            code=TaskErrorCode.DETAIL_QUALITY_FAILED,
            action=TaskErrorAction.RESOLVE_DETAIL_ID,
            retryable=False,
            report_resource_failure=False,
        ),
        TaskErrorCode.DATA_VALIDATION_FAILED: TaskErrorPolicy(
            code=TaskErrorCode.DATA_VALIDATION_FAILED,
            action=TaskErrorAction.FAIL_RETRY,
            retryable=True,
            report_resource_failure=False,
        ),
        TaskErrorCode.COMMENT_MISMATCH: TaskErrorPolicy(
            code=TaskErrorCode.COMMENT_MISMATCH,
            action=TaskErrorAction.FAIL_RETRY,
            retryable=True,
            report_resource_failure=False,
        ),
        TaskErrorCode.HTTP_REQUEST_FAILED: TaskErrorPolicy(
            code=TaskErrorCode.HTTP_REQUEST_FAILED,
            action=TaskErrorAction.FAIL_RETRY,
            retryable=True,
            report_resource_failure=True,
        ),
        TaskErrorCode.PARSE_ERROR: TaskErrorPolicy(
            code=TaskErrorCode.PARSE_ERROR,
            action=TaskErrorAction.FAIL_RETRY,
            retryable=True,
            report_resource_failure=False,
        ),
        TaskErrorCode.UNKNOWN_ERROR: TaskErrorPolicy(
            code=TaskErrorCode.UNKNOWN_ERROR,
            action=TaskErrorAction.FAIL_RETRY,
            retryable=True,
            report_resource_failure=True,
        ),
    }

    def __init__(self, db):
        self.db = db

    @classmethod
    def is_detail_quality_problem_code(cls, problem_code: str | None) -> bool:
        return (problem_code or "") in cls.DETAIL_QUALITY_PROBLEM_CODES

    @classmethod
    def is_detail_quality_error(cls, message: str | None) -> bool:
        text = message or ""
        return any(marker in text for marker in cls.DETAIL_QUALITY_MARKERS)

    @classmethod
    def classify_exception(cls, task_type: str, exc: Exception) -> TaskErrorPolicy:
        message = str(exc) or type(exc).__name__

        if isinstance(exc, ProxyUnavailableError):
            return cls.POLICIES[TaskErrorCode.PROXY_UNAVAILABLE]

        if (
            task_type == TaskType.DETAIL.value
            and isinstance(exc, DataValidationError)
            and cls.is_detail_quality_error(message)
        ):
            return cls.POLICIES[TaskErrorCode.DETAIL_QUALITY_FAILED]

        if isinstance(exc, DataValidationError):
            return cls.POLICIES[TaskErrorCode.DATA_VALIDATION_FAILED]

        if isinstance(exc, CommentMismatchError):
            return cls.POLICIES[TaskErrorCode.COMMENT_MISMATCH]

        if isinstance(exc, httpx.HTTPError) or "HTTP 请求失败" in message:
            return cls.POLICIES[TaskErrorCode.HTTP_REQUEST_FAILED]

        if isinstance(exc, (json.JSONDecodeError, ValueError, KeyError)):
            return cls.POLICIES[TaskErrorCode.PARSE_ERROR]

        return cls.POLICIES[TaskErrorCode.UNKNOWN_ERROR]

    async def handle_exception(
        self,
        task: CrawlTask,
        task_type: str,
        exc: Exception,
        task_manager: TaskManager,
        metadata: dict[str, Any] | None = None,
    ) -> TaskErrorHandlingResult:
        policy = self.classify_exception(task_type, exc)
        message = str(exc) or type(exc).__name__
        metadata = metadata or {}

        if policy.action == TaskErrorAction.RELEASE:
            updated = task_manager.release_task(
                task,
                message,
                error_code=policy.code.value,
                error_action=policy.action.value,
                metadata=metadata,
            )
            return TaskErrorHandlingResult(
                code=policy.code,
                action=policy.action,
                message=message,
                task_updated=updated,
                report_resource_failure=policy.report_resource_failure,
                count_as_failed=False,
                metadata=metadata,
            )

        if policy.action == TaskErrorAction.RESOLVE_DETAIL_ID:
            updated = task_manager.fail_task(
                task,
                message,
                error_code=policy.code.value,
                error_action=policy.action.value,
                retryable=False,
                metadata=metadata,
            )
            resolution: dict[str, Any] | None = None
            if updated:
                try:
                    resolution = await JournalIdResolverService(self.db).resolve_one(
                        int(task.target_id),
                        dry_run=False,
                    )
                except Exception as resolution_error:
                    self.db.rollback()
                    resolution = {
                        "journal_id": int(task.target_id),
                        "status": "resolver_failed",
                        "message": str(resolution_error) or type(resolution_error).__name__,
                    }
            resolved = bool(resolution and resolution.get("status") == "updated")
            return TaskErrorHandlingResult(
                code=policy.code,
                action=policy.action,
                message=message,
                task_updated=updated,
                resolution=resolution,
                report_resource_failure=policy.report_resource_failure,
                count_as_failed=not resolved,
                metadata=metadata,
            )

        updated = task_manager.fail_task(
            task,
            message,
            error_code=policy.code.value,
            error_action=policy.action.value,
            retryable=policy.retryable,
            metadata=metadata,
        )
        return TaskErrorHandlingResult(
            code=policy.code,
            action=policy.action,
            message=message,
            task_updated=updated,
            report_resource_failure=policy.report_resource_failure,
            count_as_failed=True,
            metadata=metadata,
        )
