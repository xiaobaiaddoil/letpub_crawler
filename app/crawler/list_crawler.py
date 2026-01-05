import re
import logging
from typing import List, Dict, Tuple
from app.crawler.base import BaseCrawler
from app.config import config

logger = logging.getLogger(__name__)

class ListCrawler(BaseCrawler):
    """列表页爬虫 - 爬取分类下的期刊列表"""

    def _build_list_url(self, field_tag: str, page: int = 1) -> str:
        """构建列表页URL"""
        return f"{config.BASE_URL}/index.php?page=journalapp&view=researchfield&fieldtag={field_tag}&firstletter=&currentpage={page}#journallisttable"

    async def get_total_pages(self, field_tag: str) -> int:
        """获取分类的总页数"""
        url = self._build_list_url(field_tag, 1)
        success = await self.goto(url)
        if not success:
            return 0

        # 查找分页信息
        try:
            # 尝试多种方式获取总页数
            # 方式1: 查找 "共X页" 文本
            page_info = await self.page.query_selector('text=/共\\s*\\d+\\s*页/')
            if page_info:
                text = await page_info.inner_text()
                match = re.search(r'共\s*(\d+)\s*页', text)
                if match:
                    return int(match.group(1))

            # 方式2: 查找最后一个分页按钮
            page_links = await self.page.query_selector_all('a[href*="currentpage="]')
            max_page = 1
            for link in page_links:
                href = await link.get_attribute("href")
                match = re.search(r'currentpage=(\d+)', href)
                if match:
                    page_num = int(match.group(1))
                    max_page = max(max_page, page_num)
            return max_page

        except Exception as e:
            logger.warning(f"获取总页数失败: {e}")
            return 1

    async def crawl(self, field_tag: str, page: int = 1) -> Tuple[List[Dict], int]:
        """
        爬取指定分类和页码的期刊列表
        返回: (期刊列表, 总页数)
        """
        journals = []
        url = self._build_list_url(field_tag, page)

        success = await self.goto(url)
        if not success:
            raise Exception(f"无法访问列表页: {url}")


        # 解析期刊列表
        # 期刊列表通常在表格中，包含期刊名称和链接
        rows = await self.page.query_selector_all('table tr')

        for row in rows:
            try:
                # 查找期刊链接
                link = await row.query_selector('a[href*="journalid="]')
                if not link:
                    continue

                href = await link.get_attribute("href")
                name = await link.inner_text()

                # 提取journal_id
                match = re.search(r'journalid=(\d+)', href)
                if not match:
                    continue

                journal_id = int(match.group(1))
                journal_name = name.strip()

                # 尝试提取其他信息（ISSN、影响因子等）
                journal_data = {
                    "journal_id": journal_id,
                    "name": journal_name,
                }

                journals.append(journal_data)
                logger.debug(f"发现期刊: {journal_name} (id={journal_id})")

            except Exception as e:
                logger.warning(f"解析期刊行失败: {e}")
                continue

        logger.info(f"分类 {field_tag} 第 {page} 页: 发现 {len(journals)} 个期刊")
        return journals

