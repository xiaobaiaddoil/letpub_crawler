import re
import logging
import json
import math
import copy
import asyncio
from typing import Dict, List, Optional, Any
import uuid
import lxml.html
from app.crawler.base import BaseCrawler
from app.config import config

logger = logging.getLogger(__name__)

# 问题记录回调（由 worker 设置）
_problem_recorder = None

def set_problem_recorder(recorder):
    """设置问题记录器（由 worker 调用）"""
    global _problem_recorder
    _problem_recorder = recorder

def record_problem(journal_id: int, problem_type: str, problem_code: str, 
                   message: str = None, expected: int = None, actual: int = None):
    """记录问题（如果设置了记录器）"""
    if _problem_recorder:
        try:
            _problem_recorder(journal_id, problem_type, problem_code, message, expected, actual)
        except Exception as e:
            logger.warning(f"记录问题失败: {e}")


class DataValidationError(Exception):
    """数据校验失败异常"""
    def __init__(self, message: str, missing_fields: List[str] = None, extracted_fields: int = 0):
        super().__init__(message)
        self.missing_fields = missing_fields or []
        self.extracted_fields = extracted_fields


class DetailCrawler(BaseCrawler):
    """详情页爬虫；评论 API 由独立 comment 任务调用。"""

    # 必须存在的核心字段（至少需要其中一个）
    REQUIRED_FIELDS = ['期刊名字', 'issn', '期刊ISSN']

    # 期望存在的重要字段（用于评估数据完整性）
    IMPORTANT_FIELDS = [
        'issn', '期刊ISSN', 'impact_factor', '最新影响因子',
        'jcr_partition', 'JCR分区', 'cas_partition'
    ]

    # 最小期望字段数量（提取的字段数少于此值认为数据不完整）
    MIN_EXPECTED_FIELDS = 5

    def _build_detail_url(self, journal_id: int) -> str:
        """构建详情页URL"""
        return f"{config.BASE_URL}/index.php?journalid={journal_id}&page=journalapp&view=detail"

    @staticmethod
    def _clean_html_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    @staticmethod
    def _is_hidden_html_element(element) -> bool:
        style = (element.get("style") or "").replace(" ", "").lower()
        return "display:none" in style or "visibility:hidden" in style

    @staticmethod
    def _is_interactive_html_ancestor(element, stop_element) -> bool:
        current = element
        while current is not None and current is not stop_element:
            tag = (getattr(current, "tag", "") or "").lower()
            if tag in {"a", "button"}:
                return True
            if current.get("href") or current.get("onclick") or current.get("onmousedown") or current.get("onmouseup"):
                return True
            style = (current.get("style") or "").replace(" ", "").lower()
            if "cursor:pointer" in style:
                return True
            current = current.getparent()
        return False

    def _extract_html_element_text(self, element, exclude_tables: bool = True) -> str:
        """从 lxml 元素提取文本，尽量模拟浏览器版本的过滤规则。"""
        if element is None:
            return ""

        cloned = copy.deepcopy(element)
        for bad in cloned.xpath(".//script|.//style|.//noscript"):
            parent = bad.getparent()
            if parent is not None:
                parent.remove(bad)

        for hidden in cloned.xpath(".//*"):
            if self._is_hidden_html_element(hidden):
                parent = hidden.getparent()
                if parent is not None:
                    parent.remove(hidden)

        if exclude_tables:
            for table in cloned.xpath(".//table"):
                parent = table.getparent()
                if parent is not None:
                    parent.remove(table)

        normal_text = []
        interactive_text = []
        for text_node in cloned.xpath(".//text()"):
            text = self._clean_html_text(str(text_node))
            if not text:
                continue
            parent = text_node.getparent()
            if parent is not None and self._is_interactive_html_ancestor(parent, cloned):
                interactive_text.append(text)
            else:
                normal_text.append(text)

        selected = normal_text or interactive_text
        return self._clean_html_text(" ".join(selected))

    def _find_detail_main_table(self, doc):
        """定位详情页基本信息主表，避免依赖浏览器 DOM 下的固定表格序号。"""
        scope = doc.xpath('//*[@id="yxyz_content"]')
        tables = (scope[0].xpath(".//table") if scope else doc.xpath("//table"))

        best_table = None
        best_score = -1
        required_markers = ("期刊名字", "期刊ISSN")
        optional_markers = (
            "基本信息", "出版商", "出版国家", "出版周期", "平均审稿速度",
            "最新IF", "影响因子", "JCR", "期刊官方网站",
        )

        for table in tables:
            table_text = self._clean_html_text(table.text_content())
            if not all(marker in table_text for marker in required_markers):
                continue

            rows = table.xpath("./tbody/tr") or table.xpath("./tr")
            score = len(rows) + sum(10 for marker in optional_markers if marker in table_text)
            if score > best_score:
                best_score = score
                best_table = table

        return best_table

    def _extract_html_nested_table(self, table) -> Any:
        """提取 lxml 嵌套表格。"""
        try:
            rows = table.xpath("./tbody/tr") or table.xpath("./tr")
            if not rows:
                return {}

            first_cells = rows[0].xpath("./td|./th")
            has_header = bool(rows[0].xpath("./th"))

            if len(first_cells) > 2 or has_header:
                headers = []
                data_list = []
                for row_idx, row in enumerate(rows):
                    cells = row.xpath("./td|./th")
                    if row_idx == 0:
                        headers = [
                            self._extract_html_element_text(cell) or f"col_{i}"
                            for i, cell in enumerate(cells)
                        ]
                        continue

                    row_data = {}
                    for i, cell in enumerate(cells):
                        nested_tables = cell.xpath("./table")
                        if nested_tables:
                            nested_values = [self._extract_html_nested_table(t) for t in nested_tables]
                            value = nested_values[0] if len(nested_values) == 1 else nested_values
                        else:
                            value = self._extract_html_element_text(cell)
                        row_data[headers[i] if i < len(headers) else f"col_{i}"] = value
                    if row_data:
                        data_list.append(row_data)
                return data_list

            nested_data = {}
            for row_idx, row in enumerate(rows):
                cells = row.xpath("./td|./th")
                if len(cells) >= 2:
                    key = self._normalize_key(self._extract_html_element_text(cells[0]))
                    if not key:
                        continue
                    nested_tables = cells[1].xpath("./table")
                    if nested_tables:
                        nested_values = [self._extract_html_nested_table(t) for t in nested_tables]
                        value = nested_values[0] if len(nested_values) == 1 else nested_values
                    else:
                        value = self._extract_html_element_text(cells[1])
                    nested_data[key] = value
                elif len(cells) == 1:
                    text = self._extract_html_element_text(cells[0])
                    if text and row_idx == 0:
                        nested_data["_title"] = text
                    elif text:
                        nested_data[f"_row_{row_idx}"] = text
            return nested_data
        except Exception as e:
            logger.warning(f"提取HTML嵌套表格失败: {e}")
            return {}

    def _extract_basic_info_from_html(self, html: str) -> Dict:
        """从详情页 HTML 中提取基本信息。"""
        info = {}
        try:
            doc = lxml.html.fromstring(html)
            table = self._find_detail_main_table(doc)
            if table is None:
                logger.warning("未找到详情页基本信息主表")
                return info

            rows = table.xpath("./tbody/tr") or table.xpath("./tr")
            for row in rows:
                cells = row.xpath("./td")
                if len(cells) < 2:
                    continue

                key = self._normalize_key(self._extract_html_element_text(cells[0]))
                if not key:
                    continue

                value_cell = cells[1]
                nested_tables = value_cell.xpath("./table")
                value_cell_text = self._extract_html_element_text(value_cell)

                if nested_tables:
                    value_tables = [self._extract_html_nested_table(t) for t in nested_tables]
                    value = {
                        "text": value_cell_text,
                        "tables": value_tables,
                    }
                else:
                    value = value_cell_text

                if key and value:
                    info[key] = value

            info = self._normalize_info(info)
            logger.info(f"HTTP提取基本信息: {info.get('期刊名字', 'Unknown')}, 共 {len(info)} 个字段")
        except Exception as e:
            logger.error(f"HTTP提取基本信息失败: {e}")
            import traceback
            traceback.print_exc()

        return info

    def _basic_info_requires_login(self, info: Dict) -> bool:
        """判断详情字段是否仍是未登录占位文案。"""
        locked_markers = ("注册 或 登录 后", "注册或登录后", "登录 后，查看")
        for value in info.values():
            if isinstance(value, str) and any(marker in value for marker in locked_markers):
                return True
            if isinstance(value, dict):
                text = value.get("text")
                if isinstance(text, str) and any(marker in text for marker in locked_markers):
                    return True
        return False

    @staticmethod
    def _is_rate_limited_html(html: str) -> bool:
        return "您请求页面的速度过快" in (html or "")

    async def _fetch_detail_response(self, journal_id: int, url: str):
        """获取详情页 HTML；遇到 LetPub 页面限流时等待后重试。"""
        last_response = None
        max_attempts = 3
        reported_rate_limit_failure = False
        for attempt in range(1, max_attempts + 1):
            response = await self.request_http("GET", url, headers={
                "User-Agent": config.USER_AGENTS[0],
                "Referer": f"{config.BASE_URL}/index.php?page=journalapp",
            })
            last_response = response
            if not self._is_rate_limited_html(response.text):
                return response

            wait_seconds = max(config.CRAWL_DELAY_MAX * attempt, 10)
            logger.warning(
                f"[限流] 期刊 {journal_id} 详情页触发请求过快提示，"
                f"{wait_seconds}s 后重试 ({attempt}/{max_attempts}) "
                f"[{self.get_proxy_context_for_log()}]"
            )
            if not reported_rate_limit_failure:
                await self.report_proxy_result(success=False)
                reported_rate_limit_failure = True
            await asyncio.sleep(wait_seconds)

        return last_response

    async def crawl(self, journal_id: int, validate: bool = True) -> Dict:
        if config.CRAWLER_FETCH_MODE == "browser":
            return await self._crawl_with_browser(journal_id, validate=validate)
        return await self._crawl_with_http(journal_id, validate=validate)

    async def _crawl_with_http(self, journal_id: int, validate: bool = True) -> Dict:
        """爬取期刊详情

        流程：
        1. 建立登录会话
        2. 用 HTTP 获取详情页 HTML 并解析基本信息表格

        Args:
            journal_id: 期刊ID
            validate: 是否校验数据完整性，默认为True

        Returns:
            包含 basic_info 的字典，comments 保留为空列表以兼容旧调用方

        Raises:
            DataValidationError: 当validate=True且数据校验失败时抛出
        """
        url = self._build_detail_url(journal_id)

        # 先建立登录会话。详情页部分字段也依赖登录态。
        cookie_value = await self.get_cookie_for_http()

        response = await self._fetch_detail_response(journal_id, url)
        if response.status_code != 200:
            raise Exception(f"无法访问详情页: {url}, HTTP {response.status_code}")
        if self._is_rate_limited_html(response.text):
            logger.warning(
                f"[限流] 期刊 {journal_id} 详情页重试后仍被限制 "
                f"[{self.get_proxy_context_for_log()}]"
            )
            raise Exception(f"期刊 {journal_id} 详情页被 LetPub 限流，重试后仍未恢复")

        detail = {
            "journal_id": journal_id,
            "basic_info": {},
            "comments": []
        }

        # 提取基本信息（HTTP HTML）
        detail["basic_info"] = self._extract_basic_info_from_html(response.text)

        if self._basic_info_requires_login(detail["basic_info"]):
            logger.warning(f"[详情] 期刊 {journal_id} 详情字段仍为未登录占位，强制登录后重试")
            refreshed_cookie = await self.get_cookie_for_http(force_login=True)
            if refreshed_cookie:
                cookie_value = refreshed_cookie
                response = await self._fetch_detail_response(journal_id, url)
                if response.status_code == 200 and not self._is_rate_limited_html(response.text):
                    detail["basic_info"] = self._extract_basic_info_from_html(response.text)

        # 校验数据完整性
        if validate:
            self._validate_basic_info(detail["basic_info"], journal_id)

        return detail

    async def _crawl_with_browser(self, journal_id: int, validate: bool = True) -> Dict:
        """使用浏览器后端爬取期刊详情（兼容回退）。"""
        url = self._build_detail_url(journal_id)

        cookie_value = await self.get_cookie_for_http()
        if cookie_value and self.context:
            cookies = self._parse_cookies(cookie_value)
            if cookies:
                await self.context.add_cookies(cookies)

        success = await self.goto(url)
        if not success:
            raise Exception(f"无法访问详情页: {url}")

        detail = {
            "journal_id": journal_id,
            "basic_info": {},
            "comments": []
        }

        detail["basic_info"] = await self._extract_basic_info()

        if self._basic_info_requires_login(detail["basic_info"]):
            logger.warning(f"[详情] 期刊 {journal_id} 浏览器详情字段仍为未登录占位，强制登录后重试")
            refreshed_cookie = await self.get_cookie_for_http(force_login=True)
            if refreshed_cookie and self.context:
                cookies = self._parse_cookies(refreshed_cookie)
                if cookies:
                    await self.context.add_cookies(cookies)
                cookie_value = refreshed_cookie
                success = await self.goto(url)
                if success:
                    detail["basic_info"] = await self._extract_basic_info()

        if validate:
            self._validate_basic_info(detail["basic_info"], journal_id)

        return detail

    def _validate_basic_info(self, info: Dict, journal_id: int) -> None:
        """校验基本信息的完整性（放宽版本）

        Args:
            info: 提取的基本信息字典
            journal_id: 期刊ID（用于日志）

        Raises:
            DataValidationError: 仅当数据完全为空时抛出
        """
        extracted_fields = len(info)

        # 只检查是否完全为空
        if not info or extracted_fields == 0:
            raise DataValidationError(
                f"期刊 {journal_id} 详情数据为空，未提取到任何字段",
                missing_fields=self.REQUIRED_FIELDS,
                extracted_fields=0
            )

        # 只记录警告，不抛出异常
        has_required = any(
            field in info or field.lower() in [k.lower() for k in info.keys()]
            for field in self.REQUIRED_FIELDS
        )

        if not has_required:
            logger.warning(f"期刊 {journal_id} 缺少核心字段，提取了 {extracted_fields} 个字段")

        if extracted_fields < self.MIN_EXPECTED_FIELDS:
            logger.warning(f"期刊 {journal_id} 数据可能不完整，仅提取到 {extracted_fields} 个字段")
        else:
            logger.info(f"期刊 {journal_id} 数据校验通过，共 {extracted_fields} 个字段")
        # 检查字段数量是否达到最小期望
        if extracted_fields < self.MIN_EXPECTED_FIELDS:
            # 检查重要字段
            missing_important = [
                field for field in self.IMPORTANT_FIELDS
                if field not in info and field.lower() not in [k.lower() for k in info.keys()]
            ]

            raise DataValidationError(
                f"期刊 {journal_id} 数据不完整，仅提取到 {extracted_fields} 个字段（期望至少 {self.MIN_EXPECTED_FIELDS} 个）",
                missing_fields=missing_important,
                extracted_fields=extracted_fields
            )

        logger.info(f"期刊 {journal_id} 数据校验通过，共 {extracted_fields} 个字段")

    async def _extract_basic_info(self) -> Dict:
        """提取基本信息 - 处理表格结构（包括嵌套表格）"""
        info = {}

        try:
            # 提取期刊名称（通常在标题位置）
            # title_elem = self.page.locator('h1, .journal-title, [class*="title"]').first
            # if await title_elem.count() > 0:
            #     info["name"] = (await title_elem.inner_text()).strip()

            # 查找主信息表格 - 通常是包含期刊详情的表格
            # 根据 LetPub 网站结构，表格每行第一列是key，第二列是value
            table_main = self.page.locator('xpath=//*[@id="yxyz_content"]/table[3]')
            table_count = await table_main.count()

            for table_idx in range(table_count):
                table = table_main.nth(table_idx)
                rows = table.locator('xpath=./tbody/tr')
                row_count = await rows.count()

                for row_idx in range(row_count):
                    row = rows.nth(row_idx)
                    cells = row.locator('xpath=./td')
                    cell_count = await cells.count()

                    if cell_count >= 2:
                        # 第一列是key，第二列是value
                        key_cell = cells.nth(0)
                        value_cell = cells.nth(1)

                        key = await self._extract_cell_text(key_cell)
                        key = self._normalize_key(key)

                        if not key:
                            continue

                        # 检查value单元格是否包含嵌套表格
                        nested_table = value_cell.locator('xpath=./table')
                        nested_table_nums = await nested_table.count()
                        value_tables = []
                        for table_idx in range(nested_table_nums):
                            sub_table = nested_table.nth(table_idx)
                            value_tables.append(await self._extract_nested_table(sub_table))
                        # 获取值单元格内的非表格文本
                        value_cell_text = await self._extract_cell_text(value_cell)
                        value = {}
                        if len(value_tables):
                            value['text'] = value_cell_text
                            value['tables'] = value_tables
                        else:
                            value = value_cell_text
                        if key and value:
                            info[key] = value

            # 提取并规范化常用字段
            info = self._normalize_info(info)
            
            logger.info(f"提取基本信息: {info.get('期刊名字', 'Unknown')}, 共 {len(info)} 个字段")

        except Exception as e:
            logger.error(f"提取基本信息失败: {e}")
            import traceback
            traceback.print_exc()

        return info

    async def _extract_cell_text(self, cell) -> str:
        """提取单元格文本内容（去除嵌套表格的干扰）

        规则：
        1. 如果有嵌套表格，只提取非表格部分的文本
        2. 如果节点内既有普通文本又有可交互元素（链接/点击事件）的文本，只提取普通文本
        3. 如果节点内只有可交互元素的文本，则提取该文本
        4. 跳过隐藏元素（display:none）
        """
        try:
            # 使用 JavaScript 提取文本
            text = await cell.evaluate('''(element) => {
                let normalText = '';      // 普通文本（非可交互元素内）
                let interactiveText = ''; // 可交互元素内的文本

                // 检查元素是否隐藏
                function isHidden(el) {
                    if (el.nodeType !== 1) return false;
                    const style = window.getComputedStyle(el);
                    return style.display === 'none' || style.visibility === 'hidden';
                }

                // 检查元素是否是可交互元素（链接、有点击事件等）
                function isInteractive(el) {
                    if (el.nodeType !== 1) return false;
                    // a标签
                    if (el.tagName === 'A') return true;
                    // 带有href属性
                    if (el.hasAttribute('href')) return true;
                    // 带有onclick等事件属性
                    if (el.hasAttribute('onclick') ||
                        el.hasAttribute('onmousedown') ||
                        el.hasAttribute('onmouseup')) return true;
                    // 带有cursor:pointer样式（通常表示可点击）
                    const style = window.getComputedStyle(el);
                    if (style.cursor === 'pointer') return true;
                    // button标签
                    if (el.tagName === 'BUTTON') return true;
                    return false;
                }

                // 检查节点是否在可交互元素内
                function isInsideInteractive(node) {
                    let parent = node.parentNode;
                    while (parent && parent !== element) {
                        if (isInteractive(parent)) {
                            return true;
                        }
                        parent = parent.parentNode;
                    }
                    return false;
                }

                // 递归遍历节点
                function extractText(node) {
                    // 跳过表格元素
                    if (node.nodeType === 1 && node.tagName === 'TABLE') {
                        return;
                    }

                    // 跳过隐藏元素
                    if (node.nodeType === 1 && isHidden(node)) {
                        return;
                    }

                    // 文本节点
                    if (node.nodeType === 3) {
                        // 检查父元素是否隐藏
                        if (node.parentNode && node.parentNode.nodeType === 1 && isHidden(node.parentNode)) {
                            return;
                        }
                        const content = node.textContent.trim();
                        if (content) {
                            // 判断是否在可交互元素内
                            if (isInsideInteractive(node)) {
                                interactiveText += content + ' ';
                            } else {
                                normalText += content + ' ';
                            }
                        }
                    }

                    // 元素节点，递归处理子节点
                    if (node.nodeType === 1) {
                        for (let child of node.childNodes) {
                            extractText(child);
                        }
                    }
                }

                extractText(element);

                // 优先使用普通文本，如果没有则使用可交互元素的文本
                let result = normalText.trim() || interactiveText.trim();

                // 清理多余空格
                return result.replace(/\\s+/g, ' ');
            }''')

            return text.strip() if text else ""

        except Exception as e:
            logger.warning(f"提取单元格文本失败: {e}")
            return ""

    async def _extract_nested_table(self, table) -> Any:
        """提取嵌套表格内容

        支持两种表格结构：
        1. 两列key-value结构：返回字典
        2. 多列数据表（带表头）：返回数组
        """
        try:
            rows = table.locator('xpath=./tbody/tr')
            row_count = await rows.count()

            if row_count == 0:
                return {}

            # 获取第一行判断表格结构
            first_row = rows.nth(0)
            first_cells = first_row.locator('td, th')
            first_cell_count = await first_cells.count()

            # 检查是否是多列数据表（通常有表头行，列数>2）
            # 或者第一行包含th元素（表头）
            header_cells = first_row.locator('th')
            has_header = await header_cells.count() > 0

            if first_cell_count > 2 or has_header:
                # 多列数据表结构，提取为数组
                return await self._extract_data_table(table)
            else:
                # 两列key-value结构
                return await self._extract_kv_table(table)

        except Exception as e:
            logger.warning(f"提取嵌套表格失败: {e}")
            return {}

    async def _extract_data_table(self, table) -> List[Dict[str, str]]:
        """提取多列数据表（表头+数据行）为数组

        规则：
        - 第一行作为表头（无论是th还是td）
        - 后续行作为数据行
        """
        data_list = []
        headers = []

        try:
            rows = table.locator('xpath=./tbody/tr')
            row_count = await rows.count()

            # 如果没有tbody，直接从table查找tr
            if row_count == 0:
                rows = table.locator('xpath=./tr')
                row_count = await rows.count()

            for row_idx in range(row_count):
                row = rows.nth(row_idx)

                if row_idx == 0:
                    # 第一行作为表头，无论是th还是td
                    header_cells = row.locator('th, td')
                    header_count = await header_cells.count()

                    headers = []
                    for i in range(header_count):
                        header_text = await self._extract_cell_text(header_cells.nth(i))
                        # 保留原始表头，不做normalize
                        headers.append(header_text if header_text else f"col_{i}")
                    logger.debug(f"表头: {headers}")
                else:
                    # 数据行
                    td_cells = row.locator('xpath=./td')
                    td_count = await td_cells.count()

                    if td_count > 0:
                        row_data = {}
                        for i in range(td_count):
                            cell = td_cells.nth(i)

                            # 检查是否有更深层嵌套
                            deeper_tables = cell.locator('xpath=./table')
                            deeper_count = await deeper_tables.count()

                            if deeper_count > 0:
                                # 如果有多个嵌套表格，提取为数组
                                nested_values = []
                                for table_idx in range(deeper_count):
                                    nested_table = deeper_tables.nth(table_idx)
                                    nested_values.append(await self._extract_nested_table(nested_table))
                                # 如果只有一个表格，直接使用值；多个则用数组
                                value = nested_values[0] if deeper_count == 1 else nested_values
                            else:
                                value = await self._extract_cell_text(cell)

                            # 使用表头作为key
                            if i < len(headers) and headers[i]:
                                row_data[headers[i]] = value
                            else:
                                row_data[f"col_{i}"] = value

                        if row_data:
                            data_list.append(row_data)
                            logger.debug(f"数据行: {row_data}")

        except Exception as e:
            logger.warning(f"提取数据表失败: {e}")
            import traceback
            traceback.print_exc()

        return data_list

    async def _extract_kv_table(self, table) -> Dict[str, Any]:
        """提取两列key-value表格为字典"""
        nested_data = {}

        try:
            rows = table.locator('tr')
            row_count = await rows.count()

            for row_idx in range(row_count):
                row = rows.nth(row_idx)
                cells = row.locator('td, th')
                cell_count = await cells.count()

                if cell_count >= 2:
                    # 第一列是key，第二列是value
                    key = await self._extract_cell_text(cells.nth(0))
                    key = self._normalize_key(key)

                    if not key:
                        continue

                    # 检查是否还有更深层的嵌套
                    value_cell = cells.nth(1)
                    deeper_tables = value_cell.locator('table')
                    deeper_count = await deeper_tables.count()

                    if deeper_count > 0:
                        # 遍历所有嵌套表格
                        nested_values = []
                        for t_idx in range(deeper_count):
                            nested_table = deeper_tables.nth(t_idx)
                            nested_values.append(await self._extract_nested_table(nested_table))
                        # 如果只有一个表格，直接使用值；多个则用数组
                        value = nested_values[0] if deeper_count == 1 else nested_values
                    else:
                        value = await self._extract_cell_text(value_cell)

                    if key:
                        nested_data[key] = value
                elif cell_count == 1:
                    # 单列情况，可能是标题或单值
                    text = await self._extract_cell_text(cells.nth(0))
                    if text and row_idx == 0:
                        nested_data["_title"] = text
                    elif text:
                        nested_data[f"_row_{row_idx}"] = text

        except Exception as e:
            logger.warning(f"提取KV表格失败: {e}")

        return nested_data

    def _normalize_key(self, key: str) -> str:
        """规范化key名称"""
        if not key:
            return ""

        # 去除空白和特殊字符
        key = key.strip()
        key = re.sub(r'[\s:：]+$', '', key)  # 去除尾部冒号和空白
        key = re.sub(r'\s+', '_', key)  # 空格替换为下划线

        return key

    def _normalize_info(self, info: Dict) -> Dict:
        """规范化提取的信息，提取常用字段"""
        normalized = dict(info)  # 保留原始数据

        # 定义字段映射（原始key -> 规范化key）
        field_mappings = {
            # ISSN相关
            r'期刊ISSN|ISSN': 'issn',
            r'E-ISSN|EISSN|电子ISSN': 'eissn',

            # 影响因子相关
            r'最新影响因子|最新IF': 'impact_factor',
            r'实时影响因子|即时影响因子': 'impact_factor_realtime',
            r'5年影响因子|五年影响因子|五年IF': 'impact_factor_5year',
            r'自引率': 'self_citation_rate',

            # 分区相关
            r'JCR分区|JCR_分区': 'jcr_partition',
            r'中国科学院期刊分区.*2025年3月最新升级版.*|CAS分区': 'cas_partition',
            r'CAS警告|国际期刊预警名单|预警': 'cas_warning',

            # 审稿相关
            r'审稿速度|平均审稿速度': 'review_speed',
            r'录用比例|接受率': 'acceptance_rate',

            # 出版信息
            r'出版商|Publisher': 'publisher',
            r'国家|地区|Country': 'country',
            r'出版周期|Frequency': 'frequency',

            # CiteScore相关
            r'CiteScore': 'citescore',

            # 其他
            r'创刊年份|创刊时间': 'founded_year',
            r'官方网站|期刊网站': 'website',
            r'投稿网址': 'submission_url',
            r'开放获取|OA|Open_Access': 'open_access',
        }

        for key in list(info.keys()):
            for pattern, normalized_key in field_mappings.items():
                if re.search(pattern, key, re.IGNORECASE):
                    value = info[key]

                    # 对某些字段进行特殊处理
                    if normalized_key in ['impact_factor', 'impact_factor_realtime'] and isinstance(value, str):
                        # 尝试提取数字
                        match = re.search(r'\d+\.\d+', value)
                        if match:
                            try:
                                value = float(match.group(0))
                            except:
                                pass
                        else:
                            value = None         
                    normalized[normalized_key] = value
                    break

        return normalized

    async def _fetch_comments_from_api(self, journal_id: int, cookie_value: str) -> tuple:
        """通过AJAX API获取评论数据。

        每个评论 API HTTP 请求都会在 BaseCrawler.request_http 中重新选择代理。
        Cookie 由调用方传入；检测到未登录时通过接口登录刷新 Cookie 后重试一次。

        Returns:
            tuple: (comments列表, comment_info字典)
            comment_info包含: total_count(总评论数), total_pages(总页数), crawled_count(爬取数量)
        """
        comments = []
        cookie_refresh_attempted = False
        page_size = 10

        # 评论信息
        comment_info = {
            "total_count": 0,
            "total_pages": 0,
            "crawled_count": 0
        }

        try:
            # 评论API URL
            api_url = f"{config.BASE_URL}/journalappAjax_comments_center.php"

            page = 1
            max_pages = None

            # Cookie 由调用方传入，始终用 httpx。

            while max_pages is None or page <= max_pages:
                # 构建请求参数
                params = {
                    "action": "getdetailscommentslistflow",
                    "journalid": str(journal_id),
                    "sorttype": "undefined",
                    "page": str(page)
                }

                logger.info(f"获取评论第 {page} 页 (journal_id={journal_id})")

                try:
                    response_text = await self._fetch_with_httpx(api_url, params, cookie_value)

                    if not response_text:
                        logger.warning("获取评论响应为空")
                        if page == 1 and not cookie_refresh_attempted:
                            logger.warning("[Cookie] 第1页响应为空，Cookie可能失效，尝试刷新")
                            await self.report_cookie_result(success=False)
                            new_cookie = await self.get_cookie_for_http(force_login=True)
                            cookie_refresh_attempted = True
                            if new_cookie:
                                logger.info("[Cookie] 已获取新Cookie，重新开始获取评论")
                                cookie_value = new_cookie
                                comments.clear()
                                page = 1
                                continue
                        break

                    # 解析JSON响应
                    data = json.loads(response_text)

                    # 检查响应状态
                    if data.get("code") != 0:
                        logger.warning(f"API返回错误: {data.get('msg', 'Unknown error')}")
                        if page == 1 and not cookie_refresh_attempted:
                            logger.warning("[Cookie] 第1页API返回错误，Cookie可能失效，尝试刷新")
                            await self.report_cookie_result(success=False)
                            new_cookie = await self.get_cookie_for_http(force_login=True)
                            cookie_refresh_attempted = True
                            if new_cookie:
                                logger.info("[Cookie] 已获取新Cookie，重新开始获取评论")
                                cookie_value = new_cookie
                                comments.clear()
                                page = 1
                                continue
                        break

                    api_total_count = int(data.get("count") or 0)
                    api_total_pages = int(data.get("pages") or 0)
                    if page == 1:
                        if api_total_pages <= 0 and api_total_count > 0:
                            api_total_pages = math.ceil(api_total_count / page_size)
                        max_pages = max(api_total_pages, 1)
                        comment_info["total_count"] = api_total_count
                        comment_info["total_pages"] = max_pages
                        logger.info(f"期刊 {journal_id} API显示 {api_total_count} 条评论，{max_pages} 页")

                    # 提取评论数据
                    comment_data = data.get("data", [])
                    first_content = ""
                    if comment_data and isinstance(comment_data[0], dict):
                        first_content = comment_data[0].get("content") or ""
                    not_login = data.get("current_keke_app_status") == "NotLogin"
                    has_overlay = "force_download_install" in first_content

                    if not comment_data:
                        logger.info(f"第 {page} 页无评论数据，停止获取")
                        if page == 1 and api_total_count > 0 and not cookie_refresh_attempted:
                            logger.warning("[Cookie] 第1页无评论数据，Cookie可能失效，尝试刷新")
                            await self.report_cookie_result(success=False)
                            new_cookie = await self.get_cookie_for_http(force_login=True)
                            cookie_refresh_attempted = True
                            if new_cookie:
                                logger.info("[Cookie] 已获取新Cookie，重新开始获取评论")
                                cookie_value = new_cookie
                                comments.clear()
                                page = 1
                                continue
                        break

                    if (not_login or has_overlay) and not cookie_refresh_attempted:
                        logger.warning(
                            "[Cookie] API显示未登录或返回遮罩数据，尝试刷新 Cookie "
                            f"(status={data.get('current_keke_app_status')}, overlay={has_overlay})"
                        )
                        await self.report_cookie_result(success=False)
                        new_cookie = await self.get_cookie_for_http(force_login=True)
                        cookie_refresh_attempted = True
                        if new_cookie:
                            cookie_value = new_cookie
                            comments.clear()
                            page = 1
                            max_pages = None
                            comment_info = {
                                "total_count": 0,
                                "total_pages": 0,
                                "crawled_count": 0
                            }
                            continue

                    # 解析每条评论
                    page_comments = []
                    for item in comment_data:
                        try:
                            comment = self._parse_comment_from_api(journal_id, item)
                            if comment:
                                page_comments.append(comment)
                                comments.append(comment)
                        except Exception as e:
                            logger.warning(f"解析API评论数据失败: {e}")
                            continue

                    logger.info(f"第 {page} 页: API返回 {len(comment_data)} 条, 有效 {len(page_comments)} 条")

                    # 兼容旧失效特征：多页但第1页只有1条有效数据
                    if len(page_comments) == 1 and (max_pages or 1) > 1 and page == 1 and not cookie_refresh_attempted:
                        logger.warning(f"[Cookie] 检测到异常：多页({max_pages}页)但第1页只有1条数据，Cookie可能失效")

                        # 报告当前Cookie失败
                        await self.report_cookie_result(success=False)

                        # 尝试获取新Cookie
                        new_cookie = await self.get_cookie_for_http(force_login=True)
                        if new_cookie:
                            logger.info("[Cookie] 已获取新Cookie，重新开始获取评论")
                            cookie_value = new_cookie
                            cookie_refresh_attempted = True
                            comments.clear()
                            page = 1
                            max_pages = None
                            continue
                        else:
                            logger.warning("[Cookie] 无法获取新Cookie，继续使用当前Cookie")
                            cookie_refresh_attempted = True

                    # 检查是否还有更多评论
                    if len(comments) >= api_total_count:
                        logger.info(f"已获取全部 {api_total_count} 条评论")
                        break

                    page += 1
                    await self.random_comment_delay()

                except json.JSONDecodeError as e:
                    logger.error(f"解析JSON失败: {e}")
                    if page == 1 and not cookie_refresh_attempted:
                        await self.report_cookie_result(success=False)
                        new_cookie = await self.get_cookie_for_http(force_login=True)
                        cookie_refresh_attempted = True
                        if new_cookie:
                            cookie_value = new_cookie
                            comments.clear()
                            page = 1
                            max_pages = None
                            continue
                    break

            comment_info["crawled_count"] = len(comments)
            
            # 验证爬取数量：至少要大于 (页数-1) * 10
            # 例如：3页至少要有 20+ 条评论（最后一页可能不满10条）
            total_comments = comment_info.get("total_count", 0)
            total_pages = comment_info.get("total_pages", 0)
            min_expected = (total_pages - 1) * page_size if total_pages > 1 else 0
            if len(comments) < min_expected and total_pages > 1:
                msg = (f"API显示 {total_comments} 条/{total_pages} 页, "
                       f"实际爬取 {len(comments)} 条 (期望至少 {min_expected} 条)")
                logger.warning(f"[评论] 期刊 {journal_id} 评论数量异常: {msg}")
                # 记录问题
                record_problem(
                    journal_id=journal_id,
                    problem_type="warning",
                    problem_code="comment_mismatch",
                    message=msg,
                    expected=total_comments,
                    actual=len(comments)
                )
            else:
                await self.report_cookie_result(success=True)

            logger.info(f"通过API共获取 {len(comments)} 条评论 (API显示: {total_comments} 条)")

        except Exception as e:
            logger.error(f"API获取评论失败: {e}")
            record_problem(
                journal_id=journal_id,
                problem_type="error",
                problem_code="comment_fetch_error",
                message=str(e)
            )
            comment_info["crawled_count"] = 0
            return [], comment_info

        return comments, comment_info

    async def _fetch_with_httpx(self, api_url: str, params: Dict, cookie_value: str) -> Optional[str]:
        """使用持久 httpx client 发起POST请求（带自定义Cookie）。"""
        proxy_info = self.get_proxy_display()

        # 构建Cookie字符串
        if "=" in cookie_value:
            cookie_header = cookie_value
        else:
            cookie_header = f"PHPSESSID={cookie_value}"

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Cookie": cookie_header,
            "Origin": "https://www.letpub.com.cn",
            "Referer": f"{config.BASE_URL}/index.php?journalid={params.get('journalid')}&page=journalapp&view=detail",
            "User-Agent": config.USER_AGENTS[0],
        }

        try:
            response = await self.request_http(
                "POST",
                api_url,
                params=params,
                headers=headers,
                content="",
                throttle=False,
            )
            logger.debug(f"[API请求] {response.url} [代理: {proxy_info}] -> {response.status_code}")
            return response.text

        except Exception as e:
            logger.error(
                f"[API请求] httpx请求失败 [代理: {proxy_info}]: "
                f"{self._format_exception(e)}"
            )
            return None

    async def random_comment_delay(self):
        delay = config.COMMENT_DELAY_MIN
        if config.COMMENT_DELAY_MAX > config.COMMENT_DELAY_MIN:
            delay = config.COMMENT_DELAY_MIN + (
                config.COMMENT_DELAY_MAX - config.COMMENT_DELAY_MIN
            ) * (uuid.uuid4().int % 10_000) / 10_000
        if delay > 0:
            logger.debug(f"[评论限速] 等待 {delay:.2f}s 后继续请求评论 API")
            await asyncio.sleep(delay)

    def _parse_comment_from_api(self, journal_id:str,item: Dict) -> Optional[Dict]:
        """解析API返回的单条评论数据"""
        try:
            # API返回的评论数据是HTML字符串
            content_html = item.get("content", "")

            if not content_html:
                return None

            # 使用BeautifulSoup解析HTML
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(content_html, 'html.parser')
            
            # 提取楼层号
            floor_span = soup.find('span', string=re.compile(r'#\d+楼'))
            floor = floor_span.get_text().strip() if floor_span else None

            # 提取作者昵称
            author = None
            author_link = soup.find('a', title=re.compile(r'去查看Ta的个人主页'))
            if author_link:
                author = author_link.get_text().strip()

            # 提取期刊评分
            rating = "暂无评分"  # 默认值
            rating_div = soup.find('div', string=re.compile(r'期刊评分'))
            if rating_div:
                # 评分在后面的div中
                rating_value_div = rating_div.find_next_sibling('div').find_next_sibling('div')
                if rating_value_div:
                    rating_text = rating_value_div.get_text().strip()
                    if rating_text:
                        rating = rating_text  # 保持字符串格式
            # 提取研究方向
            research_fields = None
            research_span = soup.find('strong', string=re.compile(r'研究方向'))
            if research_span:
                research_fields = research_span.find_parent().get_text().strip()
            
            # 提取投稿结果
            submission_result = None
            result_span = soup.find('strong', string=re.compile(r'投稿结果'))
            if result_span:
                submission_result = result_span.find_parent().get_text().strip()

            # 提取投稿周期
            submission_period = None
            period_span = soup.find('strong', string=re.compile(r'投稿周期'))
            if period_span:
                submission_period = period_span.find_parent().get_text().strip()

            # 提取发表时间
            publish_time = None
            publish_span = soup.find('strong', string=re.compile(r'发表时间'))
            if publish_span:
                from bs4 import NavigableString as _NS
                next_sib = publish_span.next_sibling
                time_str = str(next_sib).lstrip('：:').strip() if isinstance(next_sib, _NS) else ""
                if time_str:
                    try:
                        from datetime import datetime
                        publish_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        logger.warning(f"无法解析发表时间: {time_str}")
                        publish_time = None

            # 提取最后更新时间
            update_time = None
            update_span = soup.find('strong', string=re.compile(r'最后更新'))
            if update_span:
                from bs4 import NavigableString as _NS
                next_sib = update_span.next_sibling
                time_str = str(next_sib).lstrip('：:').strip() if isinstance(next_sib, _NS) else ""
                if time_str:
                    try:
                        from datetime import datetime
                        update_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        logger.warning(f"无法解析更新时间: {time_str}")
                        update_time = None

            # 提取投稿经验（主要内容）
            experience = None
            experience_quote = soup.find('blockquote', class_='layui-elem-quote')
            if experience_quote:
                # 提取投稿经验文本
                exp_text = experience_quote.get_text()
                # 清理文本
                exp_text = exp_text.replace('投稿经验：', '').strip()
                # 移除回复部分
                if '其他用户回复：' in exp_text:
                    exp_text = exp_text.split('其他用户回复：')[0].strip()
                experience = exp_text

            # 构建评论对象（即使没有投稿经验也保留）
            comment = {
                "floor": floor,
                "author": author or "匿名",
                "rating": rating,
                "research_fields": research_fields,
                "submission_result": submission_result,
                "submission_period": submission_period,
                "publish_time": publish_time,  # datetime 对象
                "update_time": update_time,    # datetime 对象
                "content": experience or "",
                "comment_time": publish_time or update_time,  # 优先使用 publish_time
            }

            # 如果没有任何有效内容，跳过
            if not experience and not submission_result and not rating:
                logger.debug(f"评论 {floor} 无有效内容，跳过")
                return None


            author_name = comment.get("author", "anonymous")
            time_str = comment.get("comment_time", "")
            # 生成唯一ID
            namespace = uuid.NAMESPACE_DNS
            secure_name_uuid = str(uuid.uuid5(namespace, f"{journal_id}_{floor}_{author_name}"))
            
            comment["comment_id"] = secure_name_uuid

            return comment

        except Exception as e:
            logger.warning(f"解析API评论失败: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def _extract_comments(self) -> List[Dict]:
        """提取评论数据"""
        comments = []

        try:
            # 先尝试滚动到评论区域
            comment_section = self.page.locator('[id*="comment"], [class*="comment"], [id*="review"]').first
            if await comment_section.count() > 0:
                await comment_section.scroll_into_view_if_needed()
                await self.page.wait_for_timeout(1000)

            # 滚动页面加载更多评论
            await self.scroll_to_load(scroll_times=5, delay=2)

            # 尝试多种选择器定位评论
            comment_selectors = [
                '.comment-item',
                '.review-item',
                '[class*="comment-list"] > div',
                '[class*="comment-list"] > li',
                '[id*="comment"] .item',
                'div[class*="comment"]:not([class*="comment-list"]):not([class*="comment-section"])',
            ]

            for selector in comment_selectors:
                comment_elements = self.page.locator(selector)
                count = await comment_elements.count()

                if count > 0:
                    logger.info(f"使用选择器 '{selector}' 找到 {count} 条评论")
                    for i in range(count):
                        try:
                            elem = comment_elements.nth(i)
                            comment = await self._parse_comment(elem)
                            if comment:
                                comments.append(comment)
                        except Exception as e:
                            logger.warning(f"解析评论 {i} 失败: {e}")
                            continue
                    break  # 找到评论后停止尝试其他选择器

            # 如果没找到评论，尝试通用方法
            if not comments:
                comments = await self._extract_comments_fallback()

            logger.info(f"提取到 {len(comments)} 条评论")

        except Exception as e:
            logger.error(f"提取评论失败: {e}")

        return comments

    async def _extract_comments_fallback(self) -> List[Dict]:
        """备用评论提取方法"""
        comments = []

        try:
            # 查找所有可能是评论的区块
            possible_comments = self.page.locator('[class*="comment"], [class*="review"]')
            count = await possible_comments.count()

            for i in range(count):
                elem = possible_comments.nth(i)

                # 检查是否是容器元素（跳过）
                class_name = await elem.get_attribute('class') or ''
                if any(x in class_name.lower() for x in ['list', 'container', 'section', 'wrapper']):
                    continue

                comment = await self._parse_comment(elem)
                if comment:
                    comments.append(comment)

        except Exception as e:
            logger.warning(f"备用评论提取失败: {e}")

        return comments

    async def _parse_comment(self, elem) -> Optional[Dict]:
        """解析单条评论"""
        comment = {}

        try:
            # 获取整个评论区块的文本作为备用
            full_text = await elem.inner_text()
            if not full_text or len(full_text.strip()) < 10:
                return None

            # 评论内容 - 尝试多种选择器
            content_selectors = [
                '[class*="content"]',
                '[class*="text"]',
                '[class*="body"]',
                'p',
            ]

            for selector in content_selectors:
                content_elem = elem.locator(selector).first
                if await content_elem.count() > 0:
                    comment["content"] = (await content_elem.inner_text()).strip()
                    break

            if not comment.get("content"):
                # 使用完整文本作为内容
                comment["content"] = full_text.strip()

            # 作者
            author_selectors = [
                '[class*="author"]',
                '[class*="user"]',
                '[class*="name"]',
                '.nickname',
            ]

            for selector in author_selectors:
                author_elem = elem.locator(selector).first
                if await author_elem.count() > 0:
                    comment["author"] = (await author_elem.inner_text()).strip()
                    break

            # 时间
            time_selectors = [
                '[class*="time"]',
                '[class*="date"]',
                'time',
                '[class*="meta"]',
            ]

            for selector in time_selectors:
                time_elem = elem.locator(selector).first
                if await time_elem.count() > 0:
                    comment["comment_time"] = (await time_elem.inner_text()).strip()
                    break

            # 评分
            rating_selectors = [
                '[class*="rating"]',
                '[class*="score"]',
                '[class*="star"]',
            ]

            for selector in rating_selectors:
                rating_elem = elem.locator(selector).first
                if await rating_elem.count() > 0:
                    rating_text = (await rating_elem.inner_text()).strip()
                    # 尝试提取数字评分
                    rating_match = re.search(r'(\d+(?:\.\d+)?)', rating_text)
                    if rating_match:
                        comment["rating"] = float(rating_match.group(1))
                    else:
                        comment["rating"] = rating_text
                    break

            # 生成唯一ID用于去重
            if comment.get("content"):
                content_preview = comment["content"][:100]
                author = comment.get("author", "anonymous")
                time_str = comment.get("comment_time", "")
                comment["comment_id"] = str(hash(f"{content_preview}_{author}_{time_str}"))
                return comment

        except Exception as e:
            logger.warning(f"解析评论元素失败: {e}")

        return None

    async def crawl_comments_only(self, journal_id: int) -> tuple[List[Dict], Dict]:
        """只爬取评论（用于独立评论任务）。"""
        cookie_value = await self.get_cookie_for_http()
        if not cookie_value:
            logger.warning(f"[评论] 期刊 {journal_id} 无可用 Cookie，跳过评论爬取")
            raise Exception(f"期刊 {journal_id} 评论爬取无可用 Cookie")

        comments, comment_info = await self._fetch_comments_from_api(journal_id, cookie_value)
        if (
            not comments
            and not comment_info.get("total_count")
            and not comment_info.get("total_pages")
            and not comment_info.get("crawled_count")
        ):
            raise Exception(f"期刊 {journal_id} 评论 API 未返回有效数据")
        return comments, comment_info
