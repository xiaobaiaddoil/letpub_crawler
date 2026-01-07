from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from app.database import get_db
from app.services.task_manager import TaskManager
from app.models.task import CrawlTask, TaskStatus

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

class TaskResponse(BaseModel):
    id: int
    task_type: str
    target_id: str
    target_url: Optional[str]
    status: str
    retry_count: int
    error_message: Optional[str]
    worker_id: Optional[str]  # 执行该任务的worker标识
    locked_at: Optional[datetime]  # 任务锁定时间
    created_at: datetime
    started_at: Optional[datetime]
    completed_at: Optional[datetime]

    class Config:
        from_attributes = True

class TaskStatsResponse(BaseModel):
    by_status: dict
    by_type: dict
    total: int

@router.get("/", response_model=List[TaskResponse])
def list_tasks(
    status: Optional[str] = None,
    task_type: Optional[str] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取任务列表"""
    query = db.query(CrawlTask)

    if status:
        query = query.filter(CrawlTask.status == status)
    if task_type:
        query = query.filter(CrawlTask.task_type == task_type)

    offset = (page - 1) * size
    tasks = query.order_by(CrawlTask.created_at.desc()).offset(offset).limit(size).all()
    return tasks

@router.get("/stats", response_model=TaskStatsResponse)
def get_stats(db: Session = Depends(get_db)):
    """获取任务统计"""
    task_manager = TaskManager(db)
    return task_manager.get_stats()

@router.post("/{task_id}/retry")
def retry_task(task_id: int, db: Session = Depends(get_db)):
    """重试失败任务"""
    task = db.query(CrawlTask).filter(CrawlTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status != TaskStatus.FAILED.value:
        raise HTTPException(status_code=400, detail="只能重试失败的任务")

    task_manager = TaskManager(db)
    if task_manager.retry_task(task):
        return {"message": "任务已加入重试队列"}
    else:
        raise HTTPException(status_code=400, detail="任务已达到最大重试次数")

@router.post("/retry-all-failed")
def retry_all_failed(db: Session = Depends(get_db)):
    """重试所有失败任务"""
    task_manager = TaskManager(db)
    failed_tasks = task_manager.get_failed_tasks(limit=100)
    retried = 0
    for task in failed_tasks:
        if task_manager.retry_task(task):
            retried += 1
    return {"message": f"已重试 {retried} 个任务"}

@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """删除任务"""
    task = db.query(CrawlTask).filter(CrawlTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.status == TaskStatus.RUNNING.value:
        raise HTTPException(status_code=400, detail="无法删除运行中的任务")

    db.delete(task)
    db.commit()
    return {"message": "任务已删除"}

@router.delete("/completed")
def clear_completed_tasks(db: Session = Depends(get_db)):
    """清理已完成任务"""
    count = db.query(CrawlTask).filter(
        CrawlTask.status == TaskStatus.COMPLETED.value
    ).delete()
    db.commit()
    return {"message": f"已清理 {count} 个已完成任务"}

@router.post("/reset-detail/{journal_id}")
def reset_detail_task(journal_id: int, db: Session = Depends(get_db)):
    """重置单个期刊的详情任务，用于重新爬取"""
    task_manager = TaskManager(db)
    task_manager.reset_detail_task(journal_id)
    return {"message": f"期刊 {journal_id} 的详情任务已重置"}

@router.post("/reset-all-details")
def reset_all_detail_tasks(db: Session = Depends(get_db)):
    """重置所有详情任务，用于全量重新爬取期刊数据"""
    task_manager = TaskManager(db)
    count = task_manager.reset_all_detail_tasks()
    return {"message": f"已重置 {count} 个详情任务"}

@router.post("/{task_id}/re-crawl")
def re_crawl_task(task_id: int, db: Session = Depends(get_db)):
    """重新爬取任务（无论当前状态）"""
    task = db.query(CrawlTask).filter(CrawlTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 重置任务状态
    task.status = TaskStatus.PENDING.value
    task.retry_count = 0
    task.error_message = None
    task.worker_id = None
    task.locked_at = None
    task.started_at = None
    task.completed_at = None
    db.commit()

    return {"message": "任务已重置为待处理状态"}

@router.get("/workers")
def get_active_workers(db: Session = Depends(get_db)):
    """获取活跃的worker列表"""
    from sqlalchemy import func
    from datetime import datetime, timedelta, timezone

    # 获取最近5分钟内有活动的worker
    threshold = datetime.now(timezone.utc) - timedelta(minutes=5)

    workers = db.query(
        CrawlTask.worker_id,
        func.count(CrawlTask.id).label('task_count'),
        func.max(CrawlTask.locked_at).label('last_active')
    ).filter(
        CrawlTask.worker_id.isnot(None),
        CrawlTask.locked_at >= threshold
    ).group_by(
        CrawlTask.worker_id
    ).all()

    return {
        "workers": [
            {
                "worker_id": w.worker_id,
                "task_count": w.task_count,
                "last_active": w.last_active
            }
            for w in workers
        ]
    }

@router.post("/batch-retry")
def batch_retry_tasks(
    task_ids: List[int],
    db: Session = Depends(get_db)
):
    """批量重试任务"""
    task_manager = TaskManager(db)
    retried = 0

    for task_id in task_ids:
        task = db.query(CrawlTask).filter(CrawlTask.id == task_id).first()
        if task and task.status == TaskStatus.FAILED.value:
            if task_manager.retry_task(task):
                retried += 1

    return {"message": f"已重试 {retried}/{len(task_ids)} 个任务"}
