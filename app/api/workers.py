"""Worker管理API"""
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.models.worker import Worker, WorkerStatus
from app.models.task import CrawlTask, TaskStatus
from app.services.worker_status import is_worker_online

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workers", tags=["workers"])


class WorkerResponse(BaseModel):
    """Worker响应模型"""
    id: int
    worker_id: str
    hostname: Optional[str]
    ip_address: Optional[str]
    status: str
    current_task_count: int
    total_completed: int
    total_failed: int
    registered_at: Optional[datetime]
    last_heartbeat: Optional[datetime]
    started_at: Optional[datetime]
    version: Optional[str]
    is_online: bool  # 根据心跳判断是否在线

    class Config:
        from_attributes = True


class WorkerStatsResponse(BaseModel):
    """Worker统计响应"""
    total_workers: int
    online_workers: int
    offline_workers: int
    busy_workers: int
    total_completed: int
    total_failed: int
    workers: List[WorkerResponse]


class WorkerDetailResponse(BaseModel):
    """Worker详情响应"""
    worker: WorkerResponse
    running_tasks: List[dict]
    recent_completed: int
    recent_failed: int


@router.get("", response_model=WorkerStatsResponse)
def list_workers(
    status: Optional[str] = Query(None, description="过滤状态: online/offline/busy"),
    db: Session = Depends(get_db)
):
    """获取所有Worker列表"""
    query = db.query(Worker)

    workers = query.order_by(Worker.last_heartbeat.desc().nullslast()).all()

    # 构建响应并更新在线状态
    worker_responses = []
    online_count = 0
    offline_count = 0
    busy_count = 0
    total_completed = 0
    total_failed = 0

    for w in workers:
        is_online = is_worker_online(w)

        # 如果worker显示在线但心跳超时，更新状态
        if w.status != WorkerStatus.OFFLINE and not is_online:
            w.status = WorkerStatus.OFFLINE
            w.current_task_count = 0
            db.commit()

        # 统计
        if is_online:
            if w.current_task_count > 0:
                busy_count += 1
            else:
                online_count += 1
        else:
            offline_count += 1

        total_completed += w.total_completed or 0
        total_failed += w.total_failed or 0

        # 状态过滤
        if status:
            if status == "online" and not is_online:
                continue
            if status == "offline" and is_online:
                continue
            if status == "busy" and (not is_online or w.current_task_count == 0):
                continue

        worker_responses.append(WorkerResponse(
            id=w.id,
            worker_id=w.worker_id,
            hostname=w.hostname,
            ip_address=w.ip_address,
            status=w.status,
            current_task_count=w.current_task_count or 0,
            total_completed=w.total_completed or 0,
            total_failed=w.total_failed or 0,
            registered_at=w.registered_at,
            last_heartbeat=w.last_heartbeat,
            started_at=w.started_at,
            version=w.version,
            is_online=is_online
        ))

    return WorkerStatsResponse(
        total_workers=len(workers),
        online_workers=online_count + busy_count,
        offline_workers=offline_count,
        busy_workers=busy_count,
        total_completed=total_completed,
        total_failed=total_failed,
        workers=worker_responses
    )


@router.get("/{worker_id}", response_model=WorkerDetailResponse)
def get_worker(worker_id: str, db: Session = Depends(get_db)):
    """获取Worker详情"""
    worker = db.query(Worker).filter(Worker.worker_id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker不存在")

    is_online = is_worker_online(worker)

    # 获取该Worker正在运行的任务
    running_tasks = db.query(CrawlTask).filter(
        CrawlTask.worker_id == worker_id,
        CrawlTask.status == TaskStatus.RUNNING.value
    ).all()

    running_tasks_data = [{
        "id": t.id,
        "task_type": t.task_type,
        "target_id": t.target_id,
        "started_at": t.started_at.isoformat() if t.started_at else None
    } for t in running_tasks]

    # 最近1小时的统计
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    recent_completed = db.query(CrawlTask).filter(
        CrawlTask.worker_id == worker_id,
        CrawlTask.status == TaskStatus.COMPLETED.value,
        CrawlTask.completed_at >= one_hour_ago
    ).count()

    recent_failed = db.query(CrawlTask).filter(
        CrawlTask.worker_id == worker_id,
        CrawlTask.status == TaskStatus.FAILED.value,
        CrawlTask.completed_at >= one_hour_ago
    ).count()

    worker_response = WorkerResponse(
        id=worker.id,
        worker_id=worker.worker_id,
        hostname=worker.hostname,
        ip_address=worker.ip_address,
        status=worker.status,
        current_task_count=worker.current_task_count or 0,
        total_completed=worker.total_completed or 0,
        total_failed=worker.total_failed or 0,
        registered_at=worker.registered_at,
        last_heartbeat=worker.last_heartbeat,
        started_at=worker.started_at,
        version=worker.version,
        is_online=is_online
    )

    return WorkerDetailResponse(
        worker=worker_response,
        running_tasks=running_tasks_data,
        recent_completed=recent_completed,
        recent_failed=recent_failed
    )


@router.delete("/{worker_id}")
def delete_worker(worker_id: str, db: Session = Depends(get_db)):
    """删除Worker记录（仅删除离线的Worker）"""
    worker = db.query(Worker).filter(Worker.worker_id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker不存在")

    if is_worker_online(worker):
        raise HTTPException(status_code=400, detail="无法删除在线的Worker")

    # 检查是否有未完成的任务
    running_tasks = db.query(CrawlTask).filter(
        CrawlTask.worker_id == worker_id,
        CrawlTask.status == TaskStatus.RUNNING.value
    ).count()

    if running_tasks > 0:
        raise HTTPException(status_code=400, detail=f"Worker还有 {running_tasks} 个运行中的任务")

    db.delete(worker)
    db.commit()

    return {"message": f"Worker {worker_id} 已删除"}


@router.post("/{worker_id}/release-tasks")
def release_worker_tasks(worker_id: str, db: Session = Depends(get_db)):
    """释放Worker的所有运行中任务（用于Worker异常下线时）"""
    worker = db.query(Worker).filter(Worker.worker_id == worker_id).first()
    if not worker:
        raise HTTPException(status_code=404, detail="Worker不存在")

    # 释放该Worker的所有运行中任务
    count = db.query(CrawlTask).filter(
        CrawlTask.worker_id == worker_id,
        CrawlTask.status == TaskStatus.RUNNING.value
    ).update({
        CrawlTask.status: TaskStatus.PENDING.value,
        CrawlTask.worker_id: None,
        CrawlTask.locked_at: None,
        CrawlTask.started_at: None
    })

    db.commit()

    return {"message": f"已释放 {count} 个任务", "released_count": count}


@router.post("/cleanup-offline")
def cleanup_offline_workers(db: Session = Depends(get_db)):
    """清理离线Worker记录并释放其任务"""
    all_workers = db.query(Worker).all()
    offline_workers = [worker for worker in all_workers if not is_worker_online(worker)]

    released_tasks = 0
    deleted_workers = 0

    for worker in offline_workers:
        # 释放任务
        count = db.query(CrawlTask).filter(
            CrawlTask.worker_id == worker.worker_id,
            CrawlTask.status == TaskStatus.RUNNING.value
        ).update({
            CrawlTask.status: TaskStatus.PENDING.value,
            CrawlTask.worker_id: None,
            CrawlTask.locked_at: None,
            CrawlTask.started_at: None
        })
        released_tasks += count

        db.delete(worker)
        deleted_workers += 1

    db.commit()

    return {
        "message": f"已删除 {deleted_workers} 个离线Worker记录，释放 {released_tasks} 个任务",
        "offline_workers": deleted_workers,
        "released_tasks": released_tasks
    }


@router.get("/stats/summary")
def get_workers_summary(db: Session = Depends(get_db)):
    """获取Worker统计摘要"""
    workers = db.query(Worker).all()
    online_count = sum(1 for worker in workers if is_worker_online(worker))
    total_count = len(workers)

    # 运行中的任务数
    running_tasks = db.query(CrawlTask).filter(
        CrawlTask.status == TaskStatus.RUNNING.value
    ).count()

    # 按Worker分组的运行中任务
    tasks_by_worker = db.query(
        CrawlTask.worker_id,
        func.count(CrawlTask.id).label('count')
    ).filter(
        CrawlTask.status == TaskStatus.RUNNING.value,
        CrawlTask.worker_id.isnot(None)
    ).group_by(CrawlTask.worker_id).all()

    return {
        "online_workers": online_count,
        "total_workers": total_count,
        "running_tasks": running_tasks,
        "tasks_by_worker": {w: c for w, c in tasks_by_worker}
    }
