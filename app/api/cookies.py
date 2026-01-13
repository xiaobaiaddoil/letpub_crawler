"""Cookie池管理API"""
import logging
import random
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.cookie_pool import CookiePool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cookies", tags=["cookies"])


class CookieCreateRequest(BaseModel):
    """创建Cookie请求"""
    name: str
    cookie_value: str
    remark: Optional[str] = None


class CookieUpdateRequest(BaseModel):
    """更新Cookie请求"""
    name: Optional[str] = None
    cookie_value: Optional[str] = None
    is_active: Optional[bool] = None
    remark: Optional[str] = None


class CookieResponse(BaseModel):
    """Cookie响应模型"""
    id: int
    name: str
    cookie_value: str
    is_active: bool
    success_count: int
    fail_count: int
    last_used_at: Optional[datetime]
    last_success_at: Optional[datetime]
    last_fail_at: Optional[datetime]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    remark: Optional[str]

    class Config:
        from_attributes = True


class CookieListResponse(BaseModel):
    """Cookie列表响应"""
    total: int
    active_count: int
    cookies: List[CookieResponse]


class RandomCookieResponse(BaseModel):
    """随机Cookie响应（供Worker使用）"""
    id: int
    cookie_value: str


@router.get("", response_model=CookieListResponse)
def list_cookies(
    is_active: Optional[bool] = Query(None, description="过滤是否启用"),
    db: Session = Depends(get_db)
):
    """获取所有Cookie列表"""
    query = db.query(CookiePool)

    if is_active is not None:
        query = query.filter(CookiePool.is_active == is_active)

    cookies = query.order_by(CookiePool.created_at.desc()).all()

    active_count = db.query(CookiePool).filter(CookiePool.is_active == True).count()

    return CookieListResponse(
        total=len(cookies),
        active_count=active_count,
        cookies=[CookieResponse(
            id=c.id,
            name=c.name,
            cookie_value=c.cookie_value,
            is_active=c.is_active,
            success_count=c.success_count or 0,
            fail_count=c.fail_count or 0,
            last_used_at=c.last_used_at,
            last_success_at=c.last_success_at,
            last_fail_at=c.last_fail_at,
            created_at=c.created_at,
            updated_at=c.updated_at,
            remark=c.remark
        ) for c in cookies]
    )


@router.post("", response_model=CookieResponse)
def create_cookie(request: CookieCreateRequest, db: Session = Depends(get_db)):
    """创建新Cookie"""
    cookie = CookiePool(
        name=request.name,
        cookie_value=request.cookie_value,
        remark=request.remark,
        is_active=True
    )
    db.add(cookie)
    db.commit()
    db.refresh(cookie)

    logger.info(f"创建Cookie: {cookie.name} (ID: {cookie.id})")

    return CookieResponse(
        id=cookie.id,
        name=cookie.name,
        cookie_value=cookie.cookie_value,
        is_active=cookie.is_active,
        success_count=cookie.success_count or 0,
        fail_count=cookie.fail_count or 0,
        last_used_at=cookie.last_used_at,
        last_success_at=cookie.last_success_at,
        last_fail_at=cookie.last_fail_at,
        created_at=cookie.created_at,
        updated_at=cookie.updated_at,
        remark=cookie.remark
    )


@router.get("/random", response_model=RandomCookieResponse)
def get_random_cookie(db: Session = Depends(get_db)):
    """获取随机可用Cookie（供Worker调用）"""
    active_cookies = db.query(CookiePool).filter(
        CookiePool.is_active == True
    ).all()

    if not active_cookies:
        raise HTTPException(status_code=404, detail="没有可用的Cookie")

    # 随机选择一个
    cookie = random.choice(active_cookies)

    # 更新使用时间
    cookie.last_used_at = datetime.now(timezone.utc)
    db.commit()

    return RandomCookieResponse(
        id=cookie.id,
        cookie_value=cookie.cookie_value
    )


@router.post("/{cookie_id}/report-success")
def report_cookie_success(cookie_id: int, db: Session = Depends(get_db)):
    """报告Cookie使用成功（供Worker调用）"""
    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    cookie.success_count = (cookie.success_count or 0) + 1
    cookie.last_success_at = datetime.now(timezone.utc)
    db.commit()

    return {"message": "已记录成功"}


@router.post("/{cookie_id}/report-fail")
async def report_cookie_fail(cookie_id: int, db: Session = Depends(get_db)):
    """报告Cookie使用失败（供Worker调用）
    
    当失败次数达到阈值时，自动尝试重新登录刷新Cookie
    """
    from app.services.auth_service import AuthService, COOKIE_FAIL_THRESHOLD
    from app.models.account import Account

    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    cookie.fail_count = (cookie.fail_count or 0) + 1
    cookie.last_fail_at = datetime.now(timezone.utc)
    db.commit()

    # 检查是否需要自动刷新
    need_refresh = cookie.fail_count >= COOKIE_FAIL_THRESHOLD and cookie.name.startswith("auto_")
    refreshed = False

    if need_refresh:
        # 从Cookie名称提取邮箱
        email = cookie.name.replace("auto_", "")
        account = db.query(Account).filter(
            Account.email == email,
            Account.is_active == True
        ).first()

        if account:
            logger.info(f"Cookie {cookie.name} 失败次数达到阈值，尝试重新登录...")
            auth_service = AuthService(db)
            result = await auth_service.refresh_cookie_for_account(account)
            refreshed = result is not None

    return {
        "message": "已记录失败",
        "fail_count": cookie.fail_count,
        "auto_refreshed": refreshed
    }


@router.get("/{cookie_id}", response_model=CookieResponse)
def get_cookie(cookie_id: int, db: Session = Depends(get_db)):
    """获取Cookie详情"""
    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    return CookieResponse(
        id=cookie.id,
        name=cookie.name,
        cookie_value=cookie.cookie_value,
        is_active=cookie.is_active,
        success_count=cookie.success_count or 0,
        fail_count=cookie.fail_count or 0,
        last_used_at=cookie.last_used_at,
        last_success_at=cookie.last_success_at,
        last_fail_at=cookie.last_fail_at,
        created_at=cookie.created_at,
        updated_at=cookie.updated_at,
        remark=cookie.remark
    )


@router.put("/{cookie_id}", response_model=CookieResponse)
def update_cookie(cookie_id: int, request: CookieUpdateRequest, db: Session = Depends(get_db)):
    """更新Cookie"""
    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    if request.name is not None:
        cookie.name = request.name
    if request.cookie_value is not None:
        cookie.cookie_value = request.cookie_value
    if request.is_active is not None:
        cookie.is_active = request.is_active
    if request.remark is not None:
        cookie.remark = request.remark

    cookie.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cookie)

    logger.info(f"更新Cookie: {cookie.name} (ID: {cookie.id})")

    return CookieResponse(
        id=cookie.id,
        name=cookie.name,
        cookie_value=cookie.cookie_value,
        is_active=cookie.is_active,
        success_count=cookie.success_count or 0,
        fail_count=cookie.fail_count or 0,
        last_used_at=cookie.last_used_at,
        last_success_at=cookie.last_success_at,
        last_fail_at=cookie.last_fail_at,
        created_at=cookie.created_at,
        updated_at=cookie.updated_at,
        remark=cookie.remark
    )


@router.delete("/{cookie_id}")
def delete_cookie(cookie_id: int, db: Session = Depends(get_db)):
    """删除Cookie"""
    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    db.delete(cookie)
    db.commit()

    logger.info(f"删除Cookie: {cookie.name} (ID: {cookie_id})")

    return {"message": f"Cookie {cookie.name} 已删除"}


@router.post("/{cookie_id}/toggle")
def toggle_cookie(cookie_id: int, db: Session = Depends(get_db)):
    """切换Cookie启用状态"""
    cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
    if not cookie:
        raise HTTPException(status_code=404, detail="Cookie不存在")

    cookie.is_active = not cookie.is_active
    cookie.updated_at = datetime.now(timezone.utc)
    db.commit()

    status = "启用" if cookie.is_active else "禁用"
    logger.info(f"Cookie {cookie.name} 已{status}")

    return {"message": f"Cookie已{status}", "is_active": cookie.is_active}
