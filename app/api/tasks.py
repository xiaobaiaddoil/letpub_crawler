from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.services.task_manager import TaskManager
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.models.journal import Journal
from app.models.comment import Comment

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


# ========== 失败任务管理 ==========

@router.get("/failed-max-retry")
def get_failed_max_retry_tasks(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取重试达到上限的失败任务"""
    query = db.query(CrawlTask).filter(
        CrawlTask.status == TaskStatus.FAILED.value,
        CrawlTask.retry_count >= CrawlTask.max_retry
    )
    
    total = query.count()
    offset = (page - 1) * size
    tasks = query.order_by(CrawlTask.completed_at.desc()).offset(offset).limit(size).all()
    
    return {
        "total": total,
        "page": page,
        "size": size,
        "items": [TaskResponse.model_validate(t) for t in tasks]
    }


@router.post("/failed-max-retry/reset-all")
def reset_all_max_retry_tasks(db: Session = Depends(get_db)):
    """重置所有达到最大重试次数的失败任务"""
    count = db.query(CrawlTask).filter(
        CrawlTask.status == TaskStatus.FAILED.value,
        CrawlTask.retry_count >= CrawlTask.max_retry
    ).update({
        CrawlTask.status: TaskStatus.PENDING.value,
        CrawlTask.retry_count: 0,
        CrawlTask.error_message: None,
        CrawlTask.worker_id: None,
        CrawlTask.locked_at: None,
        CrawlTask.started_at: None,
        CrawlTask.completed_at: None
    })
    db.commit()
    return {"message": f"已重置 {count} 个失败任务"}


@router.post("/{task_id}/force-retry")
def force_retry_task(task_id: int, db: Session = Depends(get_db)):
    """强制重试任务（忽略重试次数限制）"""
    task = db.query(CrawlTask).filter(CrawlTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    task.status = TaskStatus.PENDING.value
    task.retry_count = 0
    task.error_message = None
    task.worker_id = None
    task.locked_at = None
    task.started_at = None
    task.completed_at = None
    db.commit()

    return {"message": "任务已强制重置"}


# ========== 增量更新检测 ==========

@router.get("/incremental-check")
def check_incremental_updates(db: Session = Depends(get_db)):
    """检测需要增量更新的期刊
    
    检测条件：
    1. 评论数量不匹配（数据库评论数 < 详情页显示的评论数）
    2. 爬取时间超过3个月
    """
    now = datetime.now(timezone.utc)
    three_months_ago = now - timedelta(days=90)
    
    # 获取所有已爬取详情的期刊
    journals = db.query(Journal).filter(Journal.detail_crawled == True).all()
    
    comment_mismatch = []  # 评论数量不匹配
    outdated = []  # 超过3个月
    
    for journal in journals:
        # 统计数据库中的评论数
        db_comment_count = db.query(Comment).filter(
            Comment.journal_id == journal.journal_id
        ).count()
        
        # 从 detail_data 获取页面显示的评论数（如果有）
        page_comment_count = None
        if journal.detail_data:
            page_comment_count = journal.detail_data.get("comment_count")
        
        # 检查评论数量是否匹配
        if page_comment_count is not None and db_comment_count < page_comment_count:
            comment_mismatch.append({
                "journal_id": journal.journal_id,
                "name": journal.name,
                "db_count": db_comment_count,
                "page_count": page_comment_count,
                "diff": page_comment_count - db_comment_count
            })
        
        # 检查是否超过3个月
        if journal.updated_at:
            updated_at = journal.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if updated_at < three_months_ago:
                outdated.append({
                    "journal_id": journal.journal_id,
                    "name": journal.name,
                    "last_updated": journal.updated_at.isoformat(),
                    "days_ago": (now - updated_at).days
                })
    
    return {
        "comment_mismatch": {
            "count": len(comment_mismatch),
            "items": comment_mismatch[:50]  # 限制返回数量
        },
        "outdated": {
            "count": len(outdated),
            "items": outdated[:50]
        }
    }


@router.post("/incremental-update/comments")
def create_comment_update_tasks(
    journal_ids: List[int] = None,
    db: Session = Depends(get_db)
):
    """为评论数量不匹配的期刊创建更新任务
    
    如果不传 journal_ids，则自动检测所有不匹配的期刊
    """
    task_manager = TaskManager(db)
    
    if journal_ids is None:
        # 自动检测
        journals = db.query(Journal).filter(Journal.detail_crawled == True).all()
        journal_ids = []
        
        for journal in journals:
            db_count = db.query(Comment).filter(
                Comment.journal_id == journal.journal_id
            ).count()
            
            page_count = None
            if journal.detail_data:
                page_count = journal.detail_data.get("comment_count")
            
            if page_count is not None and db_count < page_count:
                journal_ids.append(journal.journal_id)
    
    # 创建任务
    created = 0
    for jid in journal_ids:
        # 重置期刊的评论爬取状态
        journal = db.query(Journal).filter(Journal.journal_id == jid).first()
        if journal:
            journal.comments_crawled = False
            
            # 重置或创建详情任务
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
            else:
                task_manager.create_detail_task(jid)
            
            created += 1
    
    db.commit()
    return {"message": f"已创建 {created} 个评论更新任务"}


@router.post("/incremental-update/outdated")
def create_outdated_update_tasks(
    days: int = Query(90, ge=1, description="超过多少天视为过期"),
    db: Session = Depends(get_db)
):
    """为超过指定天数的期刊创建重新爬取任务"""
    now = datetime.now(timezone.utc)
    threshold = now - timedelta(days=days)
    
    # 查找过期期刊
    journals = db.query(Journal).filter(
        Journal.detail_crawled == True,
        Journal.updated_at < threshold
    ).all()
    
    created = 0
    for journal in journals:
        # 重置爬取状态
        journal.detail_crawled = False
        journal.comments_crawled = False
        
        # 重置任务
        task = db.query(CrawlTask).filter(
            CrawlTask.task_type == TaskType.DETAIL.value,
            CrawlTask.target_id == str(journal.journal_id)
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
    
    db.commit()
    return {"message": f"已创建 {created} 个过期期刊更新任务", "threshold_days": days}
