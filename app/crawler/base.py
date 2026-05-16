import asyncio
import random
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict
import httpx
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from app.config import config

logger = logging.getLogger(__name__)


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
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
        # Cookie 池相关
        self._current_cookie_info: Optional[Dict] = None  # 当前使用的 Cookie 信息
        # 代理相关
        self._current_proxy_info: Optional[Dict] = None  # 当前使用的代理信息
        self._using_direct: bool = False  # 是否使用直连（无代理）

    async def _get_proxy_from_pool(self) -> Optional[Dict]:
        """从 Master 服务器的代理池获取代理"""
        if not config.MASTER_URL:
            logger.debug("[代理] 未配置 MASTER_URL，跳过代理池")
            return None

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{config.MASTER_URL}/api/proxies/random")
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

    async def init_browser(self, use_proxy: bool = True):
        """初始化浏览器
        
        Args:
            use_proxy: 是否使用代理，默认为 True
        """
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
            if proxy_info:
                server = f"{proxy_info.get('protocol', 'http')}://{proxy_info['ip']}:{proxy_info['port']}"
                proxy_config = {"server": server}
                
                # 如果有用户名密码，添加认证
                if proxy_info.get("username") and proxy_info.get("password"):
                    proxy_config["username"] = proxy_info["username"]
                    proxy_config["password"] = proxy_info["password"]
                    logger.info(f"[代理] 使用代理(密码认证): {proxy_info['ip']}:{proxy_info['port']}")
                else:
                    # 白名单模式
                    logger.info(f"[代理] 使用代理(白名单): {proxy_info['ip']}:{proxy_info['port']}")
            else:
                # 无可用代理，使用直连
                self._using_direct = True
                logger.warning("[代理] 无可用代理，使用直连")
        else:
            self._using_direct = True
            logger.info("[代理] 已禁用代理，使用直连")
        
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=launch_args,
            proxy=proxy_config
        )
        await self._create_context()

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
                        "cookie_value": data.get("cookie_value")
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

    async def report_cookie_result(self, success: bool):
        """向 Master 服务器报告 Cookie 使用结果"""
        if not config.MASTER_URL or not self._current_cookie_info:
            return

        cookie_id = self._current_cookie_info.get("id")
        if not cookie_id:
            return

        try:
            endpoint = "report-success" if success else "report-fail"
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(f"{config.MASTER_URL}/api/cookies/{cookie_id}/{endpoint}")
                logger.debug(f"报告 Cookie {cookie_id} {'成功' if success else '失败'}")
        except Exception as e:
            logger.warning(f"报告 Cookie 结果失败: {e}")

    async def _create_context(self):
        """创建浏览器上下文"""
        user_agent = random.choice(config.USER_AGENTS)
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        # 指纹强化：在每个页面加载前注入反检测脚本
        await self.context.add_init_script(_STEALTH_INIT_SCRIPT)

        # 获取 Cookie：优先从 Cookie 池获取，否则使用本地配置
        cookie_value = await self._get_cookie_from_pool()
        if not cookie_value:
            cookie_value = config.LETPUB_COOKIE
            if cookie_value:
                logger.info("使用本地配置 Cookie")

        # 设置 Cookie
        if cookie_value:
            cookies = self._parse_cookies(cookie_value)
            if cookies:
                await self.context.add_cookies(cookies)

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
            return f"{self._current_proxy_info.get('ip')}:{self._current_proxy_info.get('port')}"
        return "直连"

    def is_using_direct(self) -> bool:
        """是否使用直连（无代理）"""
        return self._using_direct

    async def random_delay(self):
        """随机延迟"""
        delay = random.uniform(config.CRAWL_DELAY_MIN, config.CRAWL_DELAY_MAX)
        logger.debug(f"等待 {delay:.2f} 秒")
        await asyncio.sleep(delay)

    async def goto(self, url: str, wait_until: str = "networkidle") -> bool:
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
                
                # 关闭当前浏览器，用直连重新初始化
                await self.close()
                await self.init_browser(use_proxy=False)
                
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
        await self.init_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
