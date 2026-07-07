"""账号管理API"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from dataclasses import asdict

from app.database import get_db
from app.models.account import Account
from app.services.auth_service import AuthService
from app.services.crypto import encrypt_password

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountCreate(BaseModel):
    email: str
    password: str
    remark: Optional[str] = None


class AccountResponse(BaseModel):
    id: int
    email: str
    is_active: bool
    login_success_count: int
    login_fail_count: int
    last_login_at: Optional[datetime]
    last_cookie_refresh_at: Optional[datetime]
    created_at: datetime
    remark: Optional[str]

    class Config:
        from_attributes = True


@router.get("", response_model=List[AccountResponse])
def list_accounts(db: Session = Depends(get_db)):
    """获取所有账号列表"""
    accounts = db.query(Account).order_by(Account.created_at.desc()).all()
    return accounts


@router.post("", response_model=AccountResponse)
def add_account(data: AccountCreate, db: Session = Depends(get_db)):
    """添加账号（密码加密存储）"""
    auth_service = AuthService(db)
    account = auth_service.add_account(data.email, data.password, data.remark)
    return account


@router.delete("/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    """删除账号"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    db.delete(account)
    db.commit()
    return {"message": "账号已删除"}


@router.post("/{account_id}/toggle")
def toggle_account(account_id: int, db: Session = Depends(get_db)):
    """启用/禁用账号"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    account.is_active = not account.is_active
    db.commit()
    return {"message": f"账号已{'启用' if account.is_active else '禁用'}", "is_active": account.is_active}


@router.post("/{account_id}/login")
async def login_account(account_id: int, db: Session = Depends(get_db)):
    """手动触发账号登录获取Cookie"""
    account = db.query(Account).filter(Account.id == account_id).first()
    if not account:
        raise HTTPException(status_code=404, detail="账号不存在")

    auth_service = AuthService(db)
    cookie = await auth_service.refresh_cookie_for_account(account)

    if cookie:
        return {
            "message": "登录成功，Cookie已更新",
            "cookie_id": cookie.id,
            "cookie_name": cookie.name
        }
    else:
        raise HTTPException(status_code=400, detail=auth_service.last_error or "登录失败")


@router.post("/refresh-all")
async def refresh_all_cookies(db: Session = Depends(get_db)):
    """为所有活跃账号刷新Cookie"""
    auth_service = AuthService(db)
    count, failures = await auth_service.refresh_all_cookies()
    failed_count = len(failures)
    message = f"已刷新 {count} 个Cookie"
    if failed_count:
        message += f"，{failed_count} 个账号失败"
    return {
        "message": message,
        "refreshed_count": count,
        "failed_count": failed_count,
        "failures": [asdict(failure) for failure in failures],
    }


@router.post("/check-failed")
async def check_failed_cookies(db: Session = Depends(get_db)):
    """检查并刷新失败次数过多的Cookie"""
    auth_service = AuthService(db)
    count = await auth_service.check_and_refresh_failed_cookies()
    return {"message": f"已刷新 {count} 个失败Cookie", "refreshed_count": count}
