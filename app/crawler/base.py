import asyncio
import random
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional, Dict, TYPE_CHECKING
from urllib.parse import urlparse
import httpx
from app.config import config
from app.services.proxy_runtime import (
    httpx_proxy_url,
    is_loopback_proxy_host,
    playwright_proxy_server,
    proxy_display,
    runtime_proxy_host,
)

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, BrowserContext

logger = logging.getLogger(__name__)

_LETPUB_PAGE_REQUEST_LOCK = asyncio.Lock()
_LAST_LETPUB_PAGE_REQUEST_AT = 0.0


# 反检测注入脚本：消除 Playwright/Chromium 自动化特征，使指纹接近真实浏览器
_STEALTH_INIT_SCRIPT = """
// navigator.webdriver
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// chrome runtime（headless Chromium 默认无此对象）
window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};

// plugins
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5].map(() => ({
        name: 'Plugin', filename: 'plugin.dll', description: 'Plugin', length: 1
    }))
});

// languages
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});

// permissions.query 修正（headless 下 Notification.permission 异常）
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (p) => (
        p.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : _origQuery(p)
    );
}

// WebGL vendor / renderer
const _getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris Pro';
    return _getParameter.call(this, parameter);
};

// 硬件指标
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
"""


class BaseCrawler(ABC):
    """爬虫基类"""

    def __init__(self):
        self.browser: Optional["Browser"] = None
        self.context: Optional["BrowserContext"] = None
        self.page: Optional["Page"] = None
        self._playwright = None
        self.http_client: Optional[httpx.AsyncClient] = None
        # Cookie 池相关
        self._current_cookie_info: Optional[Dict] = None  # 当前使用的 Cookie 信息
        # 代理相关
        self._current_proxy_info: Optional[Dict] = None  # 当前使用的代理信息
        self._proxy_exclude_ids: set[int] = set()
        self._using_direct: bool = False  # 是否使用直连（无代理）

    def set_proxy_exclude_ids(self, proxy_ids):
        """设置本 crawler 获取代理时应排除的代理 ID。"""
        self._proxy_exclude_ids = {int(pid) for pid in (proxy_ids or []) if pid}

    async def init_http(self, use_proxy: bool = True):
        """初始化纯 HTTP 客户端。

        默认爬取链路使用 httpx，不再启动浏览器。代理选择逻辑与浏览器链路保持一致。
        """
        if self.http_client:
            return

        proxy_info = self._current_proxy_info
        self._using_direct = False

        if proxy_info:
            logger.info(f"[HTTP代理] 复用当前代理: {proxy_display(proxy_info)}")
        elif use_proxy:
            proxy_info = await self._get_proxy_from_pool()
            if proxy_info and not await self._probe_proxy(proxy_info):
                await self.report_proxy_result(success=False)
                self._current_proxy_info = None
                proxy_info = None

            if proxy_info:
                logger.info(f"[HTTP代理] 使用: {proxy_display(proxy_info)}")
            else:
                self._using_direct = True
                logger.warning("[HTTP代理] 无可用代理，使用直连")
        else:
            self._using_direct = True
            logger.info("[HTTP代理] 已禁用代理，使用直连")

        self.http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0),
            follow_redirects=True,
            proxy=httpx_proxy_url(proxy_info),
            headers={
                "User-Agent": config.USER_AGENTS[0],
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            trust_env=False,
        )

    async def ensure_http_client(self):
        """确保 HTTP 客户端可用。"""
        if not self.http_client:
            await self.init_http(use_proxy=not self._using_direct)

    async def close_http(self):
        """关闭 HTTP 客户端。"""
        if self.http_client:
            try:
                await self.http_client.aclose()
            except Exception as e:
                logger.warning(f"关闭 HTTP 客户端失败: {e}")
            finally:
                self.http_client = None

    @staticmethod
    def _should_throttle_url(url: str) -> bool:
        host = urlparse(str(url)).hostname or ""
        return host.endswith("letpub.com.cn")

    async def _throttle_letpub_page_request(self):
        """对 LetPub 页面请求做进程内节流，避免 HTTP 模式启动时并发打爆页面限流。"""
        global _LAST_LETPUB_PAGE_REQUEST_AT

        min_delay = max(0, config.CRAWL_DELAY_MIN)
        max_delay = max(min_delay, config.CRAWL_DELAY_MAX)
        delay = random.uniform(min_delay, max_delay)

        async with _LETPUB_PAGE_REQUEST_LOCK:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait_time = max(0.0, _LAST_LETPUB_PAGE_REQUEST_AT + delay - now)
            if wait_time > 0:
                logger.debug(f"[HTTP限速] 等待 {wait_time:.2f}s 后请求 LetPub 页面")
                await asyncio.sleep(wait_time)
            _LAST_LETPUB_PAGE_REQUEST_AT = loop.time()

    @staticmethod
    def _format_exception(exc: Exception) -> str:
        detail = str(exc).strip()
        if detail:
            return f"{type(exc).__name__}: {detail}"
        return type(exc).__name__

    async def request_http(self, method: str, url: str, throttle: bool = True, **kwargs) -> httpx.Response:
        """发起 HTTP 请求；代理失败时自动切换其他代理重试。"""
        if throttle and self._should_throttle_url(url):
            await self._throttle_letpub_page_request()

        last_error: Exception | None = None
        max_attempts = 4

        for attempt in range(1, max_attempts + 1):
            await self.ensure_http_client()
            proxy_info = self.get_proxy_display()
            try:
                response = await self.http_client.request(method, url, **kwargs)
                logger.debug(
                    f"[HTTP] {method.upper()} {url} [代理: {proxy_info}] -> {response.status_code}"
                )
                return response
            except Exception as exc:
                last_error = exc
                error_text = self._format_exception(exc)
                if self._current_proxy_info and not self._using_direct and attempt < max_attempts:
                    failed_proxy_id = self._current_proxy_info.get("id")
                    logger.warning(
                        f"[HTTP代理] 请求失败，准备切换代理重试 "
                        f"({attempt}/{max_attempts}) {method.upper()} {url} "
                        f"[代理: {proxy_info}] 错误: {error_text}"
                    )
                    await self.report_proxy_result(success=False)
                    if failed_proxy_id:
                        self._proxy_exclude_ids.add(int(failed_proxy_id))
                    await self.close_http()
                    self._current_proxy_info = None
                    await self.init_http(use_proxy=True)
                    if self._using_direct:
                        logger.warning("[HTTP代理] 代理池无可用代理，后续请求将使用直连")
                    continue

                logger.error(
                    f"[HTTP] 请求失败: {method.upper()} {url} [代理: {proxy_info}], "
                    f"尝试={attempt}/{max_attempts}, 错误: {error_text}"
                )
                raise

        if last_error:
            raise last_error
        raise RuntimeError(f"HTTP 请求失败: {method.upper()} {url}")

    def _cookie_value_from_client(self) -> Optional[str]:
        """从当前 HTTP 客户端提取 Cookie 字符串。"""
        if not self.http_client:
            return None
        parts = [f"{name}={value}" for name, value in self.http_client.cookies.items()]
        return "; ".join(parts) if parts else None

    def _cookie_value_from_response(self, response: httpx.Response) -> Optional[str]:
        """从响应和客户端 CookieJar 中提取 Cookie 字符串。"""
        cookie_parts = []
        for name, value in response.cookies.items():
            cookie_parts.append(f"{name}={value}")

        for sc in response.headers.get_list("set-cookie"):
            if "=" in sc:
                cookie_part = sc.split(";", 1)[0]
                if cookie_part not in cookie_parts:
                    cookie_parts.append(cookie_part)

        client_cookie = self._cookie_value_from_client()
        if client_cookie:
            for part in client_cookie.split(";"):
                part = part.strip()
                if part and part not in cookie_parts:
                    cookie_parts.append(part)

        return "; ".join(cookie_parts) if cookie_parts else None

    def set_http_cookie(self, cookie_value: str):
        """把 Cookie 字符串写入 HTTP 客户端 CookieJar。"""
        if not self.http_client or not cookie_value:
            return
        for item in cookie_value.split(";"):
            item = item.strip()
            if "=" not in item:
                continue
            name, value = item.split("=", 1)
            self.http_client.cookies.set(
                name.strip(),
                value.strip(),
                domain="www.letpub.com.cn",
                path="/",
            )

    async def _login_with_local_account(self) -> Optional[str]:
        """使用本地数据库中的账号登录并返回 Cookie。

        分布式 worker 优先从 Master/Cookie 配置拿 Cookie；standalone 模式没有 Master 时，
        这里直接用账号接口登录，确保纯 HTTP 爬取链路有可用会话。
        """
        try:
            from cryptography.fernet import InvalidToken
            from sqlalchemy import true
            from app.database import SessionLocal
            from app.models.account import Account
            from app.models.cookie_pool import CookiePool
            from app.services.crypto import decrypt_password
            from app.services.auth_service import LOGIN_URL
        except Exception as e:
            logger.warning(f"[登录] 本地账号登录依赖不可用: {e}")
            return None

        db = SessionLocal()
        account = None
        try:
            account = db.query(Account).filter(
                Account.is_active == true()
            ).order_by(Account.last_login_at.asc().nullsfirst()).first()
            if not account:
                logger.warning("[登录] 无可用本地账号")
                return None

            try:
                password = decrypt_password(account.password_encrypted)
            except InvalidToken:
                account.login_fail_count = (account.login_fail_count or 0) + 1
                db.commit()
                logger.error("[登录] 账号密码无法解密：ENCRYPTION_KEY 不匹配或密文损坏")
                return None

            await self.ensure_http_client()
            response = await self.request_http(
                "POST",
                LOGIN_URL,
                data={"email": account.email, "password": password},
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": config.USER_AGENTS[0],
                    "Referer": f"{config.BASE_URL}/index.php?page=login",
                },
            )

            if "SUCCESS" not in response.text:
                account.login_fail_count = (account.login_fail_count or 0) + 1
                db.commit()
                logger.warning("[登录] LetPub 登录失败: HTTP %s", response.status_code)
                return None

            cookie_value = self._cookie_value_from_response(response)
            if not cookie_value:
                account.login_fail_count = (account.login_fail_count or 0) + 1
                db.commit()
                logger.warning("[登录] 登录成功但未获得 Cookie")
                return None

            now = datetime.now(timezone.utc)
            account.login_success_count = (account.login_success_count or 0) + 1
            account.last_login_at = now
            account.last_cookie_refresh_at = now

            cookie_name = f"auto_{account.email}"
            cookie_row = db.query(CookiePool).filter(CookiePool.name == cookie_name).first()
            if cookie_row:
                cookie_row.cookie_value = cookie_value
                cookie_row.is_active = True
                cookie_row.fail_count = 0
                cookie_row.updated_at = now
            else:
                cookie_row = CookiePool(
                    name=cookie_name,
                    cookie_value=cookie_value,
                    is_active=True,
                    remark=f"自动登录获取 - {account.email}",
                )
                db.add(cookie_row)

            db.commit()
            db.refresh(cookie_row)
            self._current_cookie_info = {
                "id": cookie_row.id,
                "cookie_value": cookie_value,
                "source": "local",
            }
            self.set_http_cookie(cookie_value)
            logger.info("[登录] 本地账号登录成功，已刷新 Cookie")
            return cookie_value
        except Exception as e:
            db.rollback()
            if account is not None:
                try:
                    account.login_fail_count = (account.login_fail_count or 0) + 1
                    db.commit()
                except Exception:
                    db.rollback()
            logger.warning(f"[登录] 本地账号登录失败: {e}")
            return None
        finally:
            db.close()

    async def get_cookie_for_http(self, force_login: bool = False) -> Optional[str]:
        """获取 HTTP/API 请求使用的 Cookie。"""
        await self.ensure_http_client()
        cookie_value = None

        if force_login:
            cookie_value = await self._login_with_local_account()
            if cookie_value:
                return cookie_value

            cookie_value = await self._get_cookie_from_pool()
            if cookie_value:
                self.set_http_cookie(cookie_value)
                return cookie_value

            cookie_value = await self._get_cookie_from_local_pool()
            if cookie_value:
                self.set_http_cookie(cookie_value)
                return cookie_value

            cookie_value = config.LETPUB_COOKIE or None
            if cookie_value:
                logger.info("[Cookie] 强制刷新失败，回退到本地配置 Cookie")
                self.set_http_cookie(cookie_value)
                return cookie_value

            return None

        cookie_value = await self._get_cookie_from_pool()
        if not cookie_value:
            cookie_value = await self._get_cookie_from_local_pool()
        if not cookie_value:
            cookie_value = config.LETPUB_COOKIE or None
            if cookie_value:
                logger.info("[Cookie] 使用本地配置 Cookie")

        if cookie_value:
            self.set_http_cookie(cookie_value)
            return cookie_value

        return await self._login_with_local_account()

    async def _get_proxy_from_pool(self) -> Optional[Dict]:
        """从 Master 服务器的代理池获取代理"""
        if not config.MASTER_URL:
            logger.debug("[代理] 未配置 MASTER_URL，跳过代理池")
            return None

        try:
            params = {}
            if self._proxy_exclude_ids:
                params["exclude_ids"] = ",".join(str(pid) for pid in sorted(self._proxy_exclude_ids))

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{config.MASTER_URL}/api/proxies/random", params=params)
                if response.status_code == 200:
                    data = response.json()
                    if data.get("id"):
                        self._current_proxy_info = data
                        proxy_type = data.get("proxy_type", "unknown")
                        logger.info(f"[代理] 从代理池获取: {data.get('ip')}:{data.get('port')} (类型: {proxy_type})")
                        return data
                    else:
                        # 返回空对象说明代理池为空
                        logger.warning("[代理] 代理池为空，请检查代理配置或手动添加代理")
                else:
                    logger.warning(f"[代理] 获取代理失败: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"[代理] 从代理池获取代理失败: {e}")

        return None

    async def _probe_proxy(self, proxy_info: Dict) -> bool:
        """Check local/clash proxy reachability before giving it to browser."""
        host = proxy_info.get("ip")
        source = proxy_info.get("source")
        if source != "clash" and not is_loopback_proxy_host(host):
            return True

        runtime_host = runtime_proxy_host(host)
        port = int(proxy_info.get("port") or 0)
        display = proxy_display(proxy_info)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(runtime_host, port),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception as exc:
            logger.warning(f"[代理] {display} 不可连接，跳过代理改用直连: {exc}")
            return False

    async def report_proxy_result(self, success: bool):
        """向 Master 服务器报告代理使用结果"""
        if not config.MASTER_URL or not self._current_proxy_info:
            return

        proxy_id = self._current_proxy_info.get("id")
        proxy_addr = self.get_proxy_display()
        if not proxy_id:
            return

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(
                    f"{config.MASTER_URL}/api/proxies/{proxy_id}/report",
                    params={"success": str(success).lower()}
                )
                if success:
                    logger.debug(f"[代理] 报告成功: {proxy_addr}")
                else:
                    logger.warning(f"[代理] 报告失败: {proxy_addr}")
        except Exception as e:
            logger.warning(f"[代理] 报告结果失败: {e}")

    async def init_browser(self, use_proxy: bool = True, use_cookie: bool = True):
        """初始化浏览器

        Args:
            use_proxy: 是否使用代理，默认为 True
            use_cookie: 是否注入 Cookie，默认为 True。基本信息爬取传 False。
        """
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError(
                "当前环境未安装 Playwright 依赖。请使用 worker 镜像，或安装 crawler 额外依赖：uv sync --extra crawler。"
            ) from exc

        self._playwright = await async_playwright().start()

        # 浏览器启动参数（消除 Playwright/Chromium 自动化特征）
        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-features=IsolateOrigins,site-per-process",
            "--disable-site-isolation-trials",
            "--disable-web-security",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-infobars",
            "--start-maximized",
        ]

        # 获取代理
        proxy_config = None
        self._using_direct = False

        if use_proxy:
            proxy_info = await self._get_proxy_from_pool()
            if proxy_info and not await self._probe_proxy(proxy_info):
                await self.report_proxy_result(success=False)
                self._current_proxy_info = None
                proxy_info = None

            if proxy_info:
                server = playwright_proxy_server(proxy_info)
                proxy_config = {"server": server}
                display = proxy_display(proxy_info)

                # 如果有用户名密码，添加认证
                if proxy_info.get("username") and proxy_info.get("password"):
                    proxy_config["username"] = proxy_info["username"]
                    proxy_config["password"] = proxy_info["password"]
                    logger.info(f"[代理] 使用代理(密码认证): {display}")
                else:
                    # 白名单模式
                    logger.info(f"[代理] 使用代理(白名单): {display}")
            else:
                # 无可用代理，使用直连
                self._using_direct = True
                logger.warning("[代理] 无可用代理，使用直连")
        else:
            self._using_direct = True
            logger.info("[代理] 已禁用代理，使用直连")

        launch_options = {
            "headless": True,
            "args": launch_args,
            "proxy": proxy_config,
        }
        if config.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH:
            launch_options["executable_path"] = config.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH

        self.browser = await self._playwright.chromium.launch(**launch_options)
        await self._create_context(use_cookie=use_cookie)

    async def _get_cookie_from_pool(self) -> Optional[str]:
        """从 Master 服务器的 Cookie 池获取随机 Cookie"""
        if not config.MASTER_URL:
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{config.MASTER_URL}/api/cookies/random")
                if response.status_code == 200:
                    data = response.json()
                    self._current_cookie_info = {
                        "id": data.get("id"),
                        "cookie_value": data.get("cookie_value"),
                        "source": "master",
                    }
                    logger.info(f"从 Cookie 池获取 Cookie (ID: {data.get('id')})")
                    return data.get("cookie_value")
                elif response.status_code == 404:
                    logger.debug("Cookie 池为空")
                else:
                    logger.warning(f"获取 Cookie 失败: HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"从 Cookie 池获取 Cookie 失败: {e}")

        return None

    async def _get_cookie_from_local_pool(self) -> Optional[str]:
        """standalone 模式下从本地数据库 Cookie 池获取 Cookie。"""
        try:
            from sqlalchemy import true
            from app.database import SessionLocal
            from app.models.cookie_pool import CookiePool
        except Exception as e:
            logger.debug(f"[Cookie] 本地 Cookie 池依赖不可用: {e}")
            return None

        db = SessionLocal()
        try:
            cookie = db.query(CookiePool).filter(
                CookiePool.is_active == true()
            ).order_by(
                CookiePool.fail_count.asc(),
                CookiePool.updated_at.desc().nullslast(),
            ).first()
            if not cookie:
                logger.debug("[Cookie] 本地 Cookie 池为空")
                return None

            now = datetime.now(timezone.utc)
            cookie.last_used_at = now
            db.commit()
            self._current_cookie_info = {
                "id": cookie.id,
                "cookie_value": cookie.cookie_value,
                "source": "local",
            }
            logger.info(f"[Cookie] 从本地 Cookie 池获取 Cookie (ID: {cookie.id})")
            return cookie.cookie_value
        except Exception as e:
            db.rollback()
            logger.warning(f"[Cookie] 从本地 Cookie 池获取失败: {e}")
            return None
        finally:
            db.close()

    async def report_cookie_result(self, success: bool):
        """向 Master 服务器报告 Cookie 使用结果"""
        if not self._current_cookie_info:
            return

        cookie_id = self._current_cookie_info.get("id")
        if not cookie_id:
            return

        if not config.MASTER_URL or self._current_cookie_info.get("source") == "local":
            try:
                from app.database import SessionLocal
                from app.models.cookie_pool import CookiePool
            except Exception as e:
                logger.debug(f"本地 Cookie 结果报告依赖不可用: {e}")
                return

            db = SessionLocal()
            try:
                cookie = db.query(CookiePool).filter(CookiePool.id == cookie_id).first()
                if not cookie:
                    return
                now = datetime.now(timezone.utc)
                if success:
                    cookie.success_count = (cookie.success_count or 0) + 1
                    cookie.last_success_at = now
                    cookie.fail_count = 0
                else:
                    cookie.fail_count = (cookie.fail_count or 0) + 1
                    cookie.last_fail_at = now
                db.commit()
                return
            except Exception as e:
                db.rollback()
                logger.warning(f"报告本地 Cookie 结果失败: {e}")
                return
            finally:
                db.close()

        try:
            endpoint = "report-success" if success else "report-fail"
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{config.MASTER_URL}/api/cookies/{cookie_id}/{endpoint}")
                logger.debug(f"报告 Cookie {cookie_id} {'成功' if success else '失败'}")
        except Exception as e:
            logger.warning(f"报告 Cookie 结果失败: {e}")

    async def _create_context(self, use_cookie: bool = True):
        """创建浏览器上下文

        Args:
            use_cookie: 是否注入 Cookie。基本信息爬取传 False，评论爬取传 True。
        """
        user_agent = random.choice(config.USER_AGENTS)
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        # 指纹强化：在每个页面加载前注入反检测脚本
        await self.context.add_init_script(_STEALTH_INIT_SCRIPT)

        if use_cookie:
            # 获取 Cookie：优先从 Cookie 池获取，否则使用本地配置
            cookie_value = await self._get_cookie_from_pool()
            if not cookie_value:
                cookie_value = config.LETPUB_COOKIE
                if cookie_value:
                    logger.info("使用本地配置 Cookie")

            if cookie_value:
                cookies = self._parse_cookies(cookie_value)
                if cookies:
                    await self.context.add_cookies(cookies)
        else:
            logger.debug("[Cookie] 基本信息爬取，跳过 Cookie 注入")

        self.page = await self.context.new_page()

    def _parse_cookies(self, cookie_str: str) -> list:
        """解析Cookie字符串"""
        cookies = []
        if not cookie_str:
            return cookies

        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".letpub.com.cn",
                    "path": "/"
                })
        return cookies

    def get_current_cookie_value(self) -> Optional[str]:
        """获取当前使用的 Cookie 值（供子类使用）"""
        if self._current_cookie_info:
            return self._current_cookie_info.get("cookie_value")
        return config.LETPUB_COOKIE or None

    def get_current_proxy_info(self) -> Optional[Dict]:
        """获取当前使用的代理信息"""
        return self._current_proxy_info

    def get_proxy_display(self) -> str:
        """获取代理显示字符串（用于日志）"""
        if self._current_proxy_info:
            return proxy_display(self._current_proxy_info)
        return "直连"

    def get_proxy_context_for_log(self) -> str:
        """获取当前代理诊断信息，避免在限流日志里丢失上下文。"""
        if not self._current_proxy_info or self._using_direct:
            return "use_proxy=false proxy=直连"

        proxy_info = self._current_proxy_info
        parts = [
            "use_proxy=true",
            f"proxy={proxy_display(proxy_info)}",
        ]
        for key in ("id", "source", "proxy_type", "area"):
            value = proxy_info.get(key)
            if value:
                parts.append(f"{key}={value}")
        remark = str(proxy_info.get("remark") or "").strip()
        if remark:
            parts.append(f"remark={remark[:120]}")
        return " ".join(parts)

    def get_httpx_proxy_url(self) -> Optional[str]:
        """获取 httpx 使用的当前代理 URL。"""
        return httpx_proxy_url(self._current_proxy_info)

    def is_using_direct(self) -> bool:
        """是否使用直连（无代理）"""
        return self._using_direct

    async def reset_context(self, use_cookie: bool = True):
        """複用 browser，只重建 context + page（換 cookie/UA）。"""
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
            self.context = None
            self.page = None
        self._current_cookie_info = None
        await self._create_context(use_cookie=use_cookie)

    async def random_delay(self):
        """随机延迟"""
        delay = random.uniform(config.CRAWL_DELAY_MIN, config.CRAWL_DELAY_MAX)
        logger.debug(f"等待 {delay:.2f} 秒")
        await asyncio.sleep(delay)

    async def goto(self, url: str, wait_until: str = "domcontentloaded") -> bool:
        """访问页面，代理失败自动切换直连重试"""
        proxy_info = self.get_proxy_display()
        try:
            logger.info(f"访问: {url} [代理: {proxy_info}]")
            await self.page.goto(url, wait_until=wait_until, timeout=60000)
            await self.random_delay()
            return True
        except Exception as e:
            logger.error(f"访问页面失败: {url} [代理: {proxy_info}], 错误: {e}")

            # 如果使用代理失败，尝试切换直连重试
            if self._current_proxy_info and not self._using_direct:
                logger.warning(f"[代理] {proxy_info} 失败，切换直连重试...")
                await self.report_proxy_result(success=False)

                # 关闭当前浏览器，用直连重新初始化（保留 use_cookie 状态）
                use_cookie = bool(self._current_cookie_info or config.LETPUB_COOKIE)
                await self.close()
                await self.init_browser(use_proxy=False, use_cookie=use_cookie)

                # 直连重试
                try:
                    logger.info(f"访问: {url} [直连重试]")
                    await self.page.goto(url, wait_until=wait_until, timeout=60000)
                    await self.random_delay()
                    return True
                except Exception as e2:
                    logger.error(f"直连重试失败: {url}, 错误: {e2}")
                    return False

            return False

    async def scroll_to_load(self, scroll_times: int = 3, delay: float = 1.5):
        """滚动页面加载动态内容"""
        for i in range(scroll_times):
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(delay)
            logger.debug(f"滚动 {i + 1}/{scroll_times}")

    async def close(self):
        """关闭浏览器（带异常处理）"""
        await self.close_http()

        try:
            if self.context:
                try:
                    await self.context.close()
                except Exception as e:
                    logger.warning(f"关闭context失败: {e}")
                self.context = None
        except Exception:
            pass

        try:
            if self.browser:
                try:
                    await self.browser.close()
                except Exception as e:
                    logger.warning(f"关闭browser失败: {e}")
                self.browser = None
        except Exception:
            pass

        try:
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception as e:
                    logger.warning(f"停止playwright失败: {e}")
                self._playwright = None
        except Exception:
            pass

    async def is_healthy(self) -> bool:
        """检查浏览器是否健康"""
        try:
            if not self.browser or not self.page:
                return False
            # 尝试执行简单操作检查连接
            await self.page.evaluate("1 + 1")
            return True
        except Exception:
            return False

    async def ensure_browser(self):
        """确保浏览器可用，如果不可用则重新初始化"""
        if not await self.is_healthy():
            logger.info("浏览器不可用，重新初始化...")
            await self.close()
            await self.init_browser()

    @abstractmethod
    async def crawl(self, *args, **kwargs):
        """执行爬取，子类实现"""
        pass

    async def __aenter__(self):
        if config.CRAWLER_FETCH_MODE == "browser":
            await self.init_browser()
        else:
            await self.init_http()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
