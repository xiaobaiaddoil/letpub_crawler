"""问题任务API"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from app.database import get_db
from app.services.problem_service import ProblemService
from app.services.task_manager import TaskManager
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
def retry_all_problems(
    problem_code: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """重试所有未解决的问题任务"""
    service = ProblemService(db)
    task_manager = TaskManager(db)
    
    journal_ids = service.get_unresolved_journal_ids(problem_code)
    
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
