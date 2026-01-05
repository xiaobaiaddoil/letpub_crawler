import asyncio
import random
import logging
from abc import ABC, abstractmethod
from typing import Optional
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

    async def _create_context(self):
        """创建浏览器上下文"""
        user_agent = random.choice(config.USER_AGENTS)
        self.context = await self.browser.new_context(
            user_agent=user_agent,
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )

        # 设置Cookie
        if config.LETPUB_COOKIE:
            cookies = self._parse_cookies(config.LETPUB_COOKIE)
            if cookies:
                await self.context.add_cookies(cookies)

        self.page = await self.context.new_page()
        # 通过浏览器启动参数隐藏自动化特征

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
        """关闭浏览器"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    @abstractmethod
    async def crawl(self, *args, **kwargs):
        """执行爬取，子类实现"""
        pass

    async def __aenter__(self):
        await self.init_browser()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
