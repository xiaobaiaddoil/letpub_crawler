import re
import logging
import json
import math
import httpx
from typing import Dict, List, Optional, Any
import uuid
import lxml.etree
from app.crawler.base import BaseCrawler
from app.config import config

logger = logging.getLogger(__name__)


class DataValidationError(Exception):
    """数据校验失败异常"""
    def __init__(self, message: str, missing_fields: List[str] = None, extracted_fields: int = 0):
        super().__init__(message)
        self.missing_fields = missing_fields or []
        self.extracted_fields = extracted_fields


class DetailCrawler(BaseCrawler):
    """详情页爬虫 - 爬取期刊详情和评论"""

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

    async def crawl(self, journal_id: int, validate: bool = True) -> Dict:
        """爬取期刊详情

        Args:
            journal_id: 期刊ID
            validate: 是否校验数据完整性，默认为True

        Returns:
            包含basic_info和comments的字典

        Raises:
            DataValidationError: 当validate=True且数据校验失败时抛出
        """
        url = self._build_detail_url(journal_id)

        success = await self.goto(url)
        if not success:
            raise Exception(f"无法访问详情页: {url}")

        detail = {
            "journal_id": journal_id,
            "basic_info": {},
            "comments": []
        }

        # 提取基本信息（表格结构）
        detail["basic_info"] = await self._extract_basic_info()

        # 校验数据完整性
        if validate:
            self._validate_basic_info(detail["basic_info"], journal_id)

        # 通过API提取评论（更高效）
        detail["comments"] = await self._fetch_comments_from_api(journal_id)

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
            table_main = self.page.locator('xpath=//*[@id="yxyz_content"]/table[4]')
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
            r'最新影响因子': 'impact_factor',
            r'实时影响因子|即时影响因子': 'impact_factor_realtime',
            r'5年影响因子|五年影响因子': 'impact_factor_5year',
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

    async def _fetch_comments_from_api(self, journal_id: int) -> List[Dict]:
        """通过AJAX API获取评论数据（更高效的方式）

        使用浏览器初始化时获取的 Cookie（来自 Cookie 池或本地配置）
        """
        comments = []

        try:
            comment_ele = self.page.locator("xpath=//td/h2/font")
            comment_nums = await comment_ele.inner_text()
            page_nums = 10
            pages = int(comment_nums)
            page_nums = math.ceil(pages / page_nums)

            # 评论API URL
            api_url = f"{config.BASE_URL}/journalappAjax_comments_center.php"

            page = 1
            max_pages = page_nums  # 限制最大页数，防止无限循环

            # 使用浏览器初始化时获取的 Cookie（已在基类中处理）
            cookie_value = self.get_current_cookie_value()

            # 决定请求方式：有cookie时用httpx，否则用浏览器
            use_httpx = bool(cookie_value)

            while page <= max_pages:
                # 构建请求参数
                params = {
                    "action": "getdetailscommentslistflow",
                    "journalid": str(journal_id),
                    "sorttype": "undefined",  # 或 "time", "rating" 等
                    "page": str(page)
                }

                logger.info(f"获取评论第 {page} 页 (journal_id={journal_id})")

                try:
                    response_text = None

                    if use_httpx:
                        # 使用httpx发起请求（带自定义Cookie）
                        response_text = await self._fetch_with_httpx(api_url, params, cookie_value)
                    else:
                        # 使用浏览器发起请求（使用浏览器Cookie）
                        response_text = await self._fetch_with_browser(api_url, params)

                    if not response_text:
                        logger.warning("获取评论响应为空")
                        break

                    # 解析JSON响应
                    data = json.loads(response_text)

                    # 检查响应状态
                    if data.get("code") != 0:
                        logger.warning(f"API返回错误: {data.get('msg', 'Unknown error')}")
                        break

                    # 提取评论数据
                    comment_data = data.get("data", [])

                    if not comment_data:
                        logger.info(f"第 {page} 页无评论数据，停止获取")
                        break

                    # 解析每条评论  每条评论是一个div字符串需要处理
                    for item in comment_data:
                        try:
                            comment = self._parse_comment_from_api(journal_id, item)
                            if comment:
                                comments.append(comment)
                        except Exception as e:
                            logger.warning(f"解析API评论数据失败: {e}")
                            continue

                    logger.info(f"第 {page} 页获取 {len(comment_data)} 条评论")

                    # 检查是否还有更多评论
                    total_count = data.get("count", 0)
                    if len(comments) >= total_count:
                        logger.info(f"已获取全部 {total_count} 条评论")
                        break

                    page += 1

                    # 评论API请求不需要延时

                except json.JSONDecodeError as e:
                    logger.error(f"解析JSON失败: {e}")
                    # 如果API不可用，降级使用页面爬取
                    logger.info("API获取失败，尝试从页面提取评论")
                    return await self._extract_comments()

            logger.info(f"通过API共获取 {len(comments)} 条评论")

        except Exception as e:
            logger.error(f"API获取评论失败: {e}")
            # 降级使用页面爬取
            logger.info("降级使用页面爬取方式")
            return await self._extract_comments()

        return comments

    async def _fetch_with_httpx(self, api_url: str, params: Dict, cookie_value: str) -> Optional[str]:
        """使用httpx发起请求（带自定义Cookie）"""
        try:
            # 构建Cookie字符串
            # cookie_value 格式可能是 "PHPSESSID=xxx" 或纯值 "xxx"
            if "=" in cookie_value:
                cookie_header = cookie_value
            else:
                cookie_header = f"PHPSESSID={cookie_value}"

            headers = {
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": cookie_header,
                "Referer": config.BASE_URL,
                "User-Agent": config.USER_AGENTS[0]
            }

            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(api_url, params=params, headers=headers)
                return response.text

        except Exception as e:
            logger.error(f"httpx请求失败: {e}")
            return None

    async def _fetch_with_browser(self, api_url: str, params: Dict) -> Optional[str]:
        """使用浏览器发起请求（使用浏览器Cookie）"""
        try:
            response = await self.page.evaluate('''async (args) => {
                const url = new URL(args.url);
                Object.keys(args.params).forEach(key =>
                    url.searchParams.append(key, args.params[key])
                );

                const response = await fetch(url.toString(), {
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                        'X-Requested-With': 'XMLHttpRequest'
                    }
                });
                const text = await response.text();
                return text;
            }''', {"url": api_url, "params": params})
            return response
        except Exception as e:
            logger.error(f"浏览器请求失败: {e}")
            return None

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
                parent_text = publish_span.find_parent().get_text().strip()
                # 移除 "发表时间：" 前缀
                time_str = parent_text.replace('发表时间：', '').replace('发表时间', '').strip()
                if time_str:
                    try:
                        from datetime import datetime
                        publish_time = datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S')
                    except ValueError:
                        # 如果解析失败，记录日志但继续
                        logger.warning(f"无法解析发表时间: {time_str}")
                        publish_time = None

            # 提取最后更新时间
            update_time = None
            update_span = soup.find('strong', string=re.compile(r'最后更新'))
            if update_span:
                parent_text = update_span.find_parent().get_text().strip()
                # 移除 "最后更新：" 前缀
                time_str = parent_text.replace('最后更新：', '').replace('最后更新', '').strip()
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

            # 如果没有投稿经验，返回None
            if not experience or len(experience) < 10:
                return None

            comment = {
                "floor": floor,
                "author": author or "匿名",
                "rating": rating,
                "research_fields": research_fields,
                "submission_result": submission_result,
                "submission_period": submission_period,
                "publish_time": publish_time,  # datetime 对象
                "update_time": update_time,    # datetime 对象
                "content": experience,
                "comment_time": publish_time or update_time,  # 优先使用 publish_time
            }


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

    async def crawl_comments_only(self, journal_id: int) -> List[Dict]:
        """只爬取评论（用于增量更新）"""
        url = self._build_detail_url(journal_id)

        success = await self.goto(url)
        if not success:
            return []

        return await self._extract_comments()