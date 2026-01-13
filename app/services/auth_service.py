"""认证服务 - 用于LetPub账号登录和Cookie获取"""
import logging
import httpx
from datetime import datetime, timezone
from typing import Optional, Tuple
from sqlalchemy.orm import Session

from app.config import config
from app.models.account import Account
from app.models.cookie_pool import CookiePool
from app.services.crypto import encrypt_password, decrypt_password

logger = logging.getLogger(__name__)

# 登录API
LOGIN_URL = "https://www.letpub.com.cn/content/index.php?action=loginajax"

# Cookie失败阈值，超过此值触发重新登录
COOKIE_FAIL_THRESHOLD = 3


class AuthService:
    """认证服务"""

    def __init__(self, db: Session):
        self.db = db

    def add_account(self, email: str, password: str, remark: str = None) -> Account:
        """添加账号（密码加密存储）"""
        # 检查是否已存在
        existing = self.db.query(Account).filter(Account.email == email).first()
        if existing:
            # 更新密码
            existing.password_encrypted = encrypt_password(password)
            existing.remark = remark or existing.remark
            existing.is_active = True
            self.db.commit()
            logger.info(f"更新账号: {email}")
            return existing

        account = Account(
            email=email,
            password_encrypted=encrypt_password(password),
            remark=remark
        )
        self.db.add(account)
        self.db.commit()
        self.db.refresh(account)
        logger.info(f"添加账号: {email}")
        return account

    def get_active_account(self) -> Optional[Account]:
        """获取一个可用的账号"""
        return self.db.query(Account).filter(
            Account.is_active == True
        ).order_by(Account.last_login_at.asc().nullsfirst()).first()

    async def login(self, account: Account) -> Tuple[bool, Optional[str]]:
        """使用账号登录获取Cookie
        
        Returns:
            (success, cookie_value)
        """
        try:
            password = decrypt_password(account.password_encrypted)

            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                # 发送登录请求
                response = await client.post(
                    LOGIN_URL,
                    data={
                        "email": account.email,
                        "password": password
                    },
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": config.USER_AGENTS[0],
                        "Referer": "https://www.letpub.com.cn/index.php?page=login"
                    }
                )

                # 检查响应
                if "SUCCESS" in response.text:
                    # 提取Cookie
                    cookies = response.cookies
                    cookie_parts = []
                    for name, value in cookies.items():
                        cookie_parts.append(f"{name}={value}")

                    # 也检查Set-Cookie头
                    set_cookies = response.headers.get_list("set-cookie")
                    for sc in set_cookies:
                        # 提取cookie名值对
                        if "=" in sc:
                            cookie_part = sc.split(";")[0]
                            if cookie_part not in cookie_parts:
                                cookie_parts.append(cookie_part)

                    cookie_value = "; ".join(cookie_parts) if cookie_parts else None

                    # 更新账号状态
                    account.login_success_count += 1
                    account.last_login_at = datetime.now(timezone.utc)
                    self.db.commit()

                    logger.info(f"账号 {account.email} 登录成功")
                    return True, cookie_value
                else:
                    # 登录失败
                    account.login_fail_count += 1
                    self.db.commit()
                    logger.warning(f"账号 {account.email} 登录失败: {response.text[:100]}")
                    return False, None

        except Exception as e:
            logger.error(f"登录异常: {e}")
            account.login_fail_count += 1
            self.db.commit()
            return False, None

    async def refresh_cookie_for_account(self, account: Account) -> Optional[CookiePool]:
        """为账号刷新Cookie并添加到Cookie池"""
        success, cookie_value = await self.login(account)

        if success and cookie_value:
            # 查找该账号对应的Cookie池记录
            existing_cookie = self.db.query(CookiePool).filter(
                CookiePool.name == f"auto_{account.email}"
            ).first()

            now = datetime.now(timezone.utc)

            if existing_cookie:
                # 更新现有Cookie
                existing_cookie.cookie_value = cookie_value
                existing_cookie.is_active = True
                existing_cookie.fail_count = 0  # 重置失败计数
                existing_cookie.updated_at = now
                self.db.commit()
                logger.info(f"更新Cookie池: {account.email}")
                return existing_cookie
            else:
                # 创建新Cookie
                new_cookie = CookiePool(
                    name=f"auto_{account.email}",
                    cookie_value=cookie_value,
                    is_active=True,
                    remark=f"自动登录获取 - {account.email}"
                )
                self.db.add(new_cookie)
                self.db.commit()
                self.db.refresh(new_cookie)
                logger.info(f"新增Cookie池: {account.email}")
                return new_cookie

            # 更新账号的Cookie刷新时间
            account.last_cookie_refresh_at = now
            self.db.commit()

        return None

    async def check_and_refresh_failed_cookies(self) -> int:
        """检查失败次数过多的Cookie，尝试重新登录刷新
        
        Returns:
            刷新成功的Cookie数量
        """
        # 查找失败次数超过阈值的Cookie
        failed_cookies = self.db.query(CookiePool).filter(
            CookiePool.fail_count >= COOKIE_FAIL_THRESHOLD,
            CookiePool.name.like("auto_%")  # 只处理自动登录的Cookie
        ).all()

        if not failed_cookies:
            return 0

        refreshed_count = 0

        for cookie in failed_cookies:
            # 从Cookie名称提取邮箱
            email = cookie.name.replace("auto_", "")
            account = self.db.query(Account).filter(
                Account.email == email,
                Account.is_active == True
            ).first()

            if account:
                logger.info(f"Cookie {cookie.name} 失败次数过多，尝试重新登录...")
                result = await self.refresh_cookie_for_account(account)
                if result:
                    refreshed_count += 1

        return refreshed_count

    async def refresh_all_cookies(self) -> int:
        """为所有活跃账号刷新Cookie
        
        Returns:
            刷新成功的数量
        """
        accounts = self.db.query(Account).filter(Account.is_active == True).all()
        refreshed_count = 0

        for account in accounts:
            result = await self.refresh_cookie_for_account(account)
            if result:
                refreshed_count += 1

        return refreshed_count
