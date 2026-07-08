import re
import logging
from typing import List, Dict
import lxml.html
from app.crawler.base import BaseCrawler
from app.config import config

logger = logging.getLogger(__name__)

class CategoryCrawler(BaseCrawler):
    """分类爬虫 - 爬取所有学科分类"""

    @staticmethod
    def _html_text(element) -> str:
        return re.sub(r"\s+", " ", element.text_content()).strip()

    async def crawl(self) -> List[Dict]:
        if config.CRAWLER_FETCH_MODE == "browser":
            return await self._crawl_with_browser()
        return await self._crawl_with_http()

    async def _crawl_with_http(self) -> List[Dict]:
        """爬取分类列表"""
        categories = []

        response = await self.request_http("GET", config.ENTRY_URL)
        if response.status_code != 200:
            raise Exception(f"无法访问入口页面: HTTP {response.status_code}")

        doc = lxml.html.fromstring(response.text)

        # 解析分类列表
        # 分类通常在页面左侧的列表中，格式类似于 fieldtag=3 这样的链接
        category_links = doc.xpath('//a[contains(@href, "fieldtag=")]')
        for link in category_links:
            try:
                href = link.get("href", "")
                link_text = self._html_text(link)
                parent = link.getparent()
                parent_text = self._html_text(parent) if parent is not None else link_text
                
                target_text = parent_text
                pattern = r'[（\(](\d+(?:\.\d+)?)[\)）]'
                

                if not href or "fieldtag=all" in href:
                    continue

                # 提取fieldtag值
                match = re.search(r'fieldtag=(\d+)', href)
                if match:
                    field_tag = match.group(1)
                    name = link_text.strip()

                    # 提取期刊数量（如果有的话）
                    count_match = re.search(pattern, target_text)
                    count = int(count_match.group(1)) if count_match else 0
                    name = re.sub(r'\s*\(\d+\)\s*', '', name)
                    
                    if name:
                        categories.append({
                            "field_tag": field_tag,
                            "name": name,
                            "total_count": count
                        })
                        logger.info(f"发现分类: {name} (fieldtag={field_tag}, count={count})")
                    count = None
                    name = None
            except Exception as e:
                logger.warning(f"解析分类失败: {e}")
                continue

        # 去重
        seen = set()
        unique_categories = []
        for cat in categories:
            if cat["field_tag"] not in seen:
                seen.add(cat["field_tag"])
                unique_categories.append(cat)

        logger.info(f"共发现 {len(unique_categories)} 个分类")
        return unique_categories

    async def _crawl_with_browser(self) -> List[Dict]:
        """使用浏览器后端爬取分类列表（兼容回退）。"""
        categories = []

        success = await self.goto(config.ENTRY_URL)
        if not success:
            raise Exception("无法访问入口页面")

        category_links_locator = self.page.locator('a[href*="fieldtag="]')
        links_count = await category_links_locator.count()
        for i in range(links_count):
            try:
                link_locator = category_links_locator.nth(i)
                href = await link_locator.get_attribute("href")
                link_text = await link_locator.inner_text()

                parent_locator = link_locator.locator("xpath=..")
                parent_text = await parent_locator.inner_text()
                pattern = r'[（\(](\d+(?:\.\d+)?)[\)）]'

                if not href or "fieldtag=all" in href:
                    continue

                match = re.search(r'fieldtag=(\d+)', href)
                if match:
                    field_tag = match.group(1)
                    name = link_text.strip()
                    count_match = re.search(pattern, parent_text)
                    count = int(count_match.group(1)) if count_match else 0
                    name = re.sub(r'\s*\(\d+\)\s*', '', name)

                    if name:
                        categories.append({
                            "field_tag": field_tag,
                            "name": name,
                            "total_count": count
                        })
                        logger.info(f"发现分类: {name} (fieldtag={field_tag}, count={count})")
            except Exception as e:
                logger.warning(f"解析分类失败: {e}")
                continue

        seen = set()
        unique_categories = []
        for cat in categories:
            if cat["field_tag"] not in seen:
                seen.add(cat["field_tag"])
                unique_categories.append(cat)

        logger.info(f"共发现 {len(unique_categories)} 个分类")
        return unique_categories
