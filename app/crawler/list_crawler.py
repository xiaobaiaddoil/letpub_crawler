import re
import logging
from typing import List, Dict, Optional, Tuple
import lxml.html
from app.crawler.base import BaseCrawler
from app.config import config

logger = logging.getLogger(__name__)

class ListCrawler(BaseCrawler):
    """列表页爬虫 - 爬取分类下的期刊列表"""

    @staticmethod
    def _html_text(element) -> str:
        return re.sub(r"\s+", " ", element.text_content()).strip()

    @staticmethod
    def _journal_link_candidate(href: str, text: str) -> Optional[Dict]:
        match = re.search(r'journalid=(\d+)', href or "")
        journal_name = (text or "").strip()
        if not match or not journal_name:
            return None
        return {
            "journal_id": int(match.group(1)),
            "name": journal_name,
        }

    def _build_list_url(self, field_tag: str, page: int = 1) -> str:
        """构建列表页URL"""
        return f"{config.BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag={field_tag}&firstletter=&currentpage={page}#journallisttable"

    def _build_search_url(self) -> str:
        return f"{config.BASE_URL}/index.php?page=journalapp&view=search"

    def _extract_journals_from_html(self, html: str) -> List[Dict]:
        journals = []
        doc = lxml.html.fromstring(html)

        for row in doc.xpath('//table//tr'):
            try:
                candidates = []
                for link in row.xpath('.//a[contains(@href, "journalid=")]'):
                    candidate = self._journal_link_candidate(
                        link.get("href", ""),
                        self._html_text(link),
                    )
                    if candidate:
                        candidates.append(candidate)

                if not candidates:
                    continue

                journal_data = candidates[0]
                journals.append(journal_data)
                logger.debug(f"发现期刊: {journal_data['name']} (id={journal_data['journal_id']})")

            except Exception as e:
                logger.warning(f"解析期刊行失败: {e}")
                continue

        return journals

    async def get_total_pages(self, field_tag: str) -> int:
        if config.CRAWLER_FETCH_MODE == "browser":
            return await self._get_total_pages_with_browser(field_tag)
        return await self._get_total_pages_with_http(field_tag)

    async def _get_total_pages_with_http(self, field_tag: str) -> int:
        """获取分类的总页数"""
        url = self._build_list_url(field_tag, 1)
        response = await self.request_http("GET", url)
        if response.status_code != 200:
            return 0

        doc = lxml.html.fromstring(response.text)

        # 查找分页信息
        try:
            # 尝试多种方式获取总页数
            # 方式1: 查找 "共X页" 文本
            match = re.search(r'共\s*(\d+)\s*页', self._html_text(doc))
            if match:
                return int(match.group(1))

            # 方式2: 查找最后一个分页按钮
            max_page = 1
            for link in doc.xpath('//a[contains(@href, "currentpage=")]'):
                href = link.get("href", "")
                match = re.search(r'currentpage=(\d+)', href)
                if match:
                    page_num = int(match.group(1))
                    max_page = max(max_page, page_num)
            return max_page

        except Exception as e:
            logger.warning(f"获取总页数失败: {e}")
            return 1

    async def _get_total_pages_with_browser(self, field_tag: str) -> int:
        """使用浏览器后端获取分类总页数（兼容回退）。"""
        url = self._build_list_url(field_tag, 1)
        success = await self.goto(url)
        if not success:
            return 0

        try:
            page_info = await self.page.query_selector('text=/共\\s*\\d+\\s*页/')
            if page_info:
                text = await page_info.inner_text()
                match = re.search(r'共\s*(\d+)\s*页', text)
                if match:
                    return int(match.group(1))

            page_links = await self.page.query_selector_all('a[href*="currentpage="]')
            max_page = 1
            for link in page_links:
                href = await link.get_attribute("href")
                match = re.search(r'currentpage=(\d+)', href or "")
                if match:
                    page_num = int(match.group(1))
                    max_page = max(max_page, page_num)
            return max_page
        except Exception as e:
            logger.warning(f"获取总页数失败: {e}")
            return 1

    async def crawl(self, field_tag: str, page: int = 1) -> Tuple[List[Dict], int]:
        if config.CRAWLER_FETCH_MODE == "browser":
            return await self._crawl_with_browser(field_tag, page)
        return await self._crawl_with_http(field_tag, page)

    async def _crawl_with_http(self, field_tag: str, page: int = 1) -> Tuple[List[Dict], int]:
        """
        爬取指定分类和页码的期刊列表
        返回: (期刊列表, 总页数)
        """
        journals = []
        url = self._build_list_url(field_tag, page)

        response = await self.request_http("GET", url)
        if response.status_code != 200:
            raise Exception(f"无法访问列表页: {url}, HTTP {response.status_code}")

        journals = self._extract_journals_from_html(response.text)

        logger.info(f"分类 {field_tag} 第 {page} 页: 发现 {len(journals)} 个期刊")
        return journals

    async def search_by_name(self, journal_name: str) -> List[Dict]:
        """按期刊名搜索 LetPub，并复用列表解析逻辑提取候选期刊。"""
        search_url = self._build_search_url()
        response = await self.request_http(
            "POST",
            search_url,
            data={
                "searchname": journal_name,
                "searchissn": "",
                "searchfield": "",
                "searchimpactlow": "",
                "searchimpacthigh": "",
                "searchscitype": "",
                "view": "search",
                "searchcategory1": "",
                "searchcategory2": "",
                "searchjcrkind": "",
                "searchopenaccess": "",
                "searchsort": "relevance",
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": config.BASE_URL,
                "Referer": search_url,
                "User-Agent": config.USER_AGENTS[0],
            },
        )
        if response.status_code != 200:
            raise Exception(f"无法访问期刊搜索页: {search_url}, HTTP {response.status_code}")

        journals = self._extract_journals_from_html(response.text)
        logger.info(f"按期刊名搜索: {journal_name}, 发现 {len(journals)} 个候选")
        return journals

    async def _crawl_with_browser(self, field_tag: str, page: int = 1) -> Tuple[List[Dict], int]:
        """使用浏览器后端爬取列表页（兼容回退）。"""
        journals = []
        url = self._build_list_url(field_tag, page)

        success = await self.goto(url)
        if not success:
            raise Exception(f"无法访问列表页: {url}")

        rows = await self.page.query_selector_all('table tr')
        for row in rows:
            try:
                link = await row.query_selector('a[href*="journalid="]')
                if not link:
                    continue

                href = await link.get_attribute("href")
                name = await link.inner_text()

                match = re.search(r'journalid=(\d+)', href or "")
                if not match:
                    continue

                journal_id = int(match.group(1))
                journal_name = name.strip()

                journals.append({
                    "journal_id": journal_id,
                    "name": journal_name,
                })
                logger.debug(f"发现期刊: {journal_name} (id={journal_id})")
            except Exception as e:
                logger.warning(f"解析期刊行失败: {e}")
                continue

        logger.info(f"分类 {field_tag} 第 {page} 页: 发现 {len(journals)} 个期刊")
        return journals
