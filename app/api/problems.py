"""问题任务API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.services.problem_service import ProblemService
from app.services.task_manager import TaskManager
from app.services.journal_id_resolver_service import JournalIdResolverService
from app.services.task_error_policy import TaskErrorPolicyService
from app.models.journal import Journal
from app.models.problem_task import ProblemTask
from app.models.task import CrawlTask, TaskType, TaskStatus

router = APIRouter(prefix="/api/problems", tags=["problems"])


class ProblemResponse(BaseModel):
    id: int
    journal_id: int
    problem_type: str
    problem_code: Optional[str]
    message: Optional[str]
    expected_value: Optional[int]
    actual_value: Optional[int]
    resolved: int
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/")
def list_problems(
    problem_type: Optional[str] = None,
    problem_code: Optional[str] = None,
    resolved: Optional[int] = Query(0, ge=0, le=1),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取问题列表"""
    service = ProblemService(db)
    
    if resolved == 0:
        return service.get_unresolved(problem_type, problem_code, page, size)
    else:
        # 获取已解决的
        from app.models.problem_task import ProblemTask
        query = db.query(ProblemTask).filter(ProblemTask.resolved == 1)
        if problem_type:
            query = query.filter(ProblemTask.problem_type == problem_type)
        if problem_code:
            query = query.filter(ProblemTask.problem_code == problem_code)
        
        total = query.count()
        offset = (page - 1) * size
        items = query.order_by(ProblemTask.resolved_at.desc()).offset(offset).limit(size).all()
        
        return {"total": total, "page": page, "size": size, "items": items}


@router.get("/stats")
def get_stats(db: Session = Depends(get_db)):
    """获取问题统计"""
    service = ProblemService(db)
    return service.get_stats()


@router.post("/{problem_id}/resolve")
def resolve_problem(problem_id: int, db: Session = Depends(get_db)):
    """标记问题已解决"""
    service = ProblemService(db)
    if service.mark_resolved(problem_id):
        return {"message": "已标记为解决"}
    raise HTTPException(status_code=404, detail="问题不存在")


@router.post("/retry-all")
async def retry_all_problems(
    problem_code: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """重试所有未解决的问题任务"""
    service = ProblemService(db)
    task_manager = TaskManager(db)

    if problem_code is None:
        codes = [
            row[0]
            for row in db.query(ProblemTask.problem_code)
            .filter(ProblemTask.resolved == 0)
            .distinct()
            .all()
            if row[0]
        ]
        items = [
            await retry_all_problems(problem_code=code, db=db)
            for code in codes
        ]
        return {
            "message": f"已按 {len(codes)} 类错误码分别处理未解决问题",
            "action": "dispatch_by_problem_code",
            "items": items,
        }

    journal_ids = service.get_unresolved_journal_ids(problem_code)
    if problem_code == ProblemService.CODE_COMMENT_MISMATCH:
        created = 0
        for jid in journal_ids:
            journal = db.query(Journal).filter(Journal.journal_id == jid).first()
            if not journal:
                continue
            journal.comments_crawled = False
            task = task_manager.create_comment_task(
                jid,
                journal.category_id,
                refresh_completed=True,
            )
            if task:
                created += 1
        db.commit()
        return {
            "message": f"已按错误码 {problem_code} 创建 {created} 个评论重建任务",
            "action": "refresh_comments",
            "created": created,
            "requested": len(journal_ids),
        }

    if TaskErrorPolicyService.is_detail_quality_problem_code(problem_code):
        resolution = await JournalIdResolverService(db).resolve_many(
            journal_ids,
            dry_run=False,
        )
        resolved_count = 0
        for item in resolution["items"]:
            if item.get("status") == "updated":
                resolved_count += service.mark_resolved_by_journal(
                    item["journal_id"],
                    problem_code,
                )
        return {
            "message": f"已按错误码 {problem_code} 处理 {len(journal_ids)} 个详情质量问题",
            "action": "resolve_detail_id",
            "resolved_problem_count": resolved_count,
            "resolution": resolution,
        }

    created = 0
    for jid in journal_ids:
        # 重置任务
        task = db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == str(jid)
        ).first()
        
        if task:
            task.status = TaskStatus.PENDING.value
            task.retry_count = 0
            task.error_message = None
            task.worker_id = None
            task.locked_at = None
            task.started_at = None
            task.completed_at = None
            created += 1
        
        # 标记问题为已解决（重试后会重新检测）
        service.mark_resolved_by_journal(jid, problem_code)
    
    db.commit()
    return {"message": f"已创建 {created} 个重试任务"}


@router.delete("/resolved")
def delete_resolved(db: Session = Depends(get_db)):
    """删除已解决的问题记录"""
    service = ProblemService(db)
    count = service.delete_resolved()
    return {"message": f"已删除 {count} 条记录"}
