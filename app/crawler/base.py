import asyncio
import random
import logging
from abc import ABC, abstractmethod
from typing import Optional, Dict
import httpx
from playwright.async_api import async_playwright, Browser, Page, BrowserContext
from app.config import config

logger = logging.getLogger(__name__)


class BaseCrawler(ABC):
    """爬虫基类"""

    def __init__(self):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._playwright = None
        # Cookie 池相关
        self._current_cookie_info: Optional[Dict] = None  # 当前使用的 Cookie 信息

    async def init_browser(self):
        """初始化浏览器"""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
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
        )

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

    async def random_delay(self):
        """随机延迟"""
        delay = random.uniform(config.CRAWL_DELAY_MIN, config.CRAWL_DELAY_MAX)
        logger.debug(f"等待 {delay:.2f} 秒")
        await asyncio.sleep(delay)

    async def goto(self, url: str, wait_until: str = "networkidle") -> bool:
        """访问页面"""
        try:
            logger.info(f"访问: {url}")
            await self.page.goto(url, wait_until=wait_until, timeout=60000)
            await self.random_delay()
            return True
        except Exception as e:
            logger.error(f"访问页面失败: {url}, 错误: {e}")
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
