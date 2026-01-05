"""
测试详情爬虫 - 验证Cookie访问和评论滚动功能

使用方法:
    # 测试基本信息提取（不需要Cookie）
    python -m tests.test_detail_crawler --journal-id 123

    # 测试Cookie和评论功能
    python -m tests.test_detail_crawler --journal-id 123 --cookie "your_cookie_here"

    # 使用非headless模式观察
    python -m tests.test_detail_crawler --journal-id 123 --visible

    # 完整测试
    python -m tests.test_detail_crawler --journal-id 123 --cookie "your_cookie" --visible --save-html
"""

import asyncio
import argparse
import json
import logging
import sys
import os
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DetailCrawlerTester:
    """详情爬虫测试器"""

    def __init__(self, cookie: str = None, headless: bool = True):
        self.cookie = cookie
        self.headless = headless
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None

    async def init_browser(self):
        """初始化浏览器"""
        self._playwright = await async_playwright().start()
        self.browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ]
        )

        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )

        # 设置Cookie
        if self.cookie:
            cookies = self._parse_cookies(self.cookie)
            if cookies:
                await self.context.add_cookies(cookies)
                logger.info(f"已设置 {len(cookies)} 个Cookie")

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

    async def close(self):
        """关闭浏览器"""
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def test_page_access(self, journal_id: int) -> bool:
        """测试页面访问"""
        url = f"https://www.letpub.com.cn/index.php?journalid={journal_id}&page=journalapp&view=detail"

        logger.info(f"访问页面: {url}")

        try:
            response = await self.page.goto(url, wait_until="networkidle", timeout=60000)

            if response.status != 200:
                logger.error(f"页面返回状态码: {response.status}")
                return False

            logger.info("页面访问成功")
            return True

        except Exception as e:
            logger.error(f"访问页面失败: {e}")
            return False

    async def test_table_extraction(self) -> dict:
        """测试表格信息提取"""
        logger.info("=" * 50)
        logger.info("测试表格信息提取")
        logger.info("=" * 50)

        info = {}

        try:
            # 查找所有表格
            tables = self.page.locator('table')
            table_count = await tables.count()
            logger.info(f"找到 {table_count} 个表格")

            for table_idx in range(table_count):
                table = tables.nth(table_idx)
                rows = table.locator('tr')
                row_count = await rows.count()

                logger.info(f"\n表格 {table_idx + 1}: {row_count} 行")

                for row_idx in range(min(row_count, 20)):  # 限制输出行数
                    row = rows.nth(row_idx)
                    cells = row.locator('td, th')
                    cell_count = await cells.count()

                    if cell_count >= 2:
                        key_cell = cells.nth(0)
                        value_cell = cells.nth(1)

                        key = (await key_cell.inner_text()).strip()

                        # 检查是否有嵌套表格
                        nested_table = value_cell.locator('table')
                        has_nested = await nested_table.count() > 0

                        if has_nested:
                            # 分析嵌套表格结构
                            nested_info = await self._analyze_nested_table(nested_table.first)
                            value = f"[嵌套表格] {nested_info}"
                        else:
                            value = (await value_cell.inner_text()).strip()
                            if len(value) > 100:
                                value = value[:100] + "..."

                        if key:
                            info[key] = value
                            logger.info(f"  {key}: {value}")

        except Exception as e:
            logger.error(f"表格提取失败: {e}")
            import traceback
            traceback.print_exc()

        return info

    async def _analyze_nested_table(self, table) -> str:
        """分析嵌套表格结构"""
        try:
            rows = table.locator('tr')
            row_count = await rows.count()

            if row_count == 0:
                return "(空表格)"

            # 检查第一行
            first_row = rows.nth(0)
            th_cells = first_row.locator('th')
            th_count = await th_cells.count()
            td_cells = first_row.locator('td')
            td_count = await td_cells.count()

            total_cells = th_count if th_count > 0 else td_count

            structure_info = f"{row_count} 行 x {total_cells} 列"

            # 提取表头
            if th_count > 0:
                headers = []
                for i in range(min(th_count, 5)):  # 最多显示5列
                    header = await th_cells.nth(i).inner_text()
                    headers.append(header.strip())
                structure_info += f", 表头: {', '.join(headers)}"

            return structure_info

        except Exception as e:
            return f"(分析失败: {e})"

    async def test_cookie_validity(self) -> dict:
        """测试Cookie是否有效（检查登录状态）"""
        logger.info("=" * 50)
        logger.info("测试Cookie有效性")
        logger.info("=" * 50)

        result = {
            "has_cookie": bool(self.cookie),
            "is_logged_in": False,
            "username": None,
            "indicators": []
        }

        try:
            # 检查常见的登录状态指示器
            login_indicators = [
                ('a[href*="logout"]', '登出链接'),
                ('[class*="user-info"]', '用户信息区'),
                ('[class*="username"]', '用户名'),
                ('[class*="login-status"]', '登录状态'),
                ('a[href*="member"]', '会员链接'),
            ]

            for selector, desc in login_indicators:
                elem = self.page.locator(selector).first
                if await elem.count() > 0:
                    text = await elem.inner_text()
                    result["indicators"].append({
                        "type": desc,
                        "text": text.strip()[:50]
                    })
                    result["is_logged_in"] = True
                    logger.info(f"找到登录指示: {desc} - {text.strip()[:50]}")

            # 检查是否有"登录"按钮（说明未登录）
            login_btn = self.page.locator('a[href*="login"], button:has-text("登录")').first
            if await login_btn.count() > 0:
                result["has_login_button"] = True
                logger.info("页面存在登录按钮")
            else:
                result["has_login_button"] = False

        except Exception as e:
            logger.error(f"Cookie验证失败: {e}")

        if result["is_logged_in"]:
            logger.info("✓ Cookie有效，已登录")
        else:
            logger.warning("✗ 未检测到登录状态")

        return result

    async def test_comment_section(self) -> dict:
        """测试评论区域"""
        logger.info("=" * 50)
        logger.info("测试评论区域")
        logger.info("=" * 50)

        result = {
            "comment_section_found": False,
            "comments_count": 0,
            "requires_login": False,
            "sample_comments": []
        }

        try:
            # 查找评论区域
            comment_selectors = [
                '[id*="comment"]',
                '[class*="comment"]',
                '[id*="review"]',
                '[class*="review"]',
            ]

            for selector in comment_selectors:
                section = self.page.locator(selector).first
                if await section.count() > 0:
                    result["comment_section_found"] = True
                    text = await section.inner_text()
                    logger.info(f"找到评论区域 (选择器: {selector})")

                    # 检查是否需要登录
                    if "登录" in text and ("查看" in text or "评论" in text):
                        result["requires_login"] = True
                        logger.warning("评论区域可能需要登录才能查看")
                    break

            if not result["comment_section_found"]:
                logger.warning("未找到评论区域")
                return result

            # 滚动到评论区域
            comment_section = self.page.locator('[id*="comment"], [class*="comment"]').first
            if await comment_section.count() > 0:
                await comment_section.scroll_into_view_if_needed()
                await self.page.wait_for_timeout(1000)

        except Exception as e:
            logger.error(f"评论区域测试失败: {e}")

        return result

    async def test_scroll_loading(self, scroll_times: int = 5) -> dict:
        """测试滚动加载"""
        logger.info("=" * 50)
        logger.info(f"测试滚动加载 (滚动 {scroll_times} 次)")
        logger.info("=" * 50)

        result = {
            "initial_height": 0,
            "final_height": 0,
            "content_increased": False,
            "scroll_records": []
        }

        try:
            # 获取初始高度
            result["initial_height"] = await self.page.evaluate("document.body.scrollHeight")
            logger.info(f"初始页面高度: {result['initial_height']}px")

            for i in range(scroll_times):
                # 滚动到底部
                await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                # 获取当前高度
                current_height = await self.page.evaluate("document.body.scrollHeight")
                scroll_pos = await self.page.evaluate("window.scrollY")

                record = {
                    "scroll": i + 1,
                    "height": current_height,
                    "position": scroll_pos
                }
                result["scroll_records"].append(record)
                logger.info(f"滚动 {i + 1}: 高度={current_height}px, 位置={scroll_pos}px")

            result["final_height"] = await self.page.evaluate("document.body.scrollHeight")
            result["content_increased"] = result["final_height"] > result["initial_height"]

            if result["content_increased"]:
                increase = result["final_height"] - result["initial_height"]
                logger.info(f"✓ 检测到动态内容加载，高度增加: {increase}px")
            else:
                logger.info("✗ 未检测到动态内容加载")

        except Exception as e:
            logger.error(f"滚动测试失败: {e}")

        return result

    async def save_page_html(self, filename: str):
        """保存页面HTML"""
        try:
            html = await self.page.content()
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(html)
            logger.info(f"页面HTML已保存到: {filename}")
        except Exception as e:
            logger.error(f"保存HTML失败: {e}")

    async def take_screenshot(self, filename: str):
        """截图"""
        try:
            await self.page.screenshot(path=filename, full_page=True)
            logger.info(f"截图已保存到: {filename}")
        except Exception as e:
            logger.error(f"截图失败: {e}")


async def run_tests(args):
    """运行测试"""
    tester = DetailCrawlerTester(
        cookie=args.cookie,
        headless=not args.visible
    )

    results = {
        "journal_id": args.journal_id,
        "tests": {}
    }

    try:
        await tester.init_browser()

        # 1. 测试页面访问
        access_ok = await tester.test_page_access(args.journal_id)
        results["tests"]["page_access"] = {"success": access_ok}

        if not access_ok:
            logger.error("页面访问失败，终止测试")
            return results

        # 2. 测试表格提取
        table_info = await tester.test_table_extraction()
        results["tests"]["table_extraction"] = {
            "success": len(table_info) > 0,
            "fields_count": len(table_info),
            "sample_fields": list(table_info.keys())[:10]
        }

        # 3. 测试Cookie有效性
        cookie_result = await tester.test_cookie_validity()
        results["tests"]["cookie_validity"] = cookie_result

        # 4. 测试评论区域
        comment_result = await tester.test_comment_section()
        results["tests"]["comment_section"] = comment_result

        # 5. 测试滚动加载
        scroll_result = await tester.test_scroll_loading(scroll_times=args.scroll_times)
        results["tests"]["scroll_loading"] = scroll_result

        # 保存HTML和截图
        if args.save_html:
            await tester.save_page_html(f"test_output_{args.journal_id}.html")
            await tester.take_screenshot(f"test_output_{args.journal_id}.png")

        # 如果是可视模式，等待用户查看
        if args.visible and args.wait:
            logger.info("\n按 Enter 键关闭浏览器...")
            input()

    finally:
        await tester.close()

    return results


def main():
    parser = argparse.ArgumentParser(description="测试详情爬虫")
    parser.add_argument("--journal-id", type=int, required=True, help="期刊ID")
    parser.add_argument("--cookie", type=str, default=None, help="Cookie字符串")
    parser.add_argument("--visible", action="store_true", help="使用可视模式（非headless）")
    parser.add_argument("--wait", action="store_true", help="可视模式下等待用户确认")
    parser.add_argument("--save-html", action="store_true", help="保存页面HTML和截图")
    parser.add_argument("--scroll-times", type=int, default=5, help="滚动次数")
    parser.add_argument("--output", type=str, default=None, help="结果输出文件（JSON）")

    args = parser.parse_args()

    # 运行测试
    results = asyncio.run(run_tests(args))

    # 输出结果摘要
    print("\n" + "=" * 60)
    print("测试结果摘要")
    print("=" * 60)

    for test_name, test_result in results["tests"].items():
        status = "✓" if test_result.get("success", False) else "✗"
        print(f"{status} {test_name}")

    # 保存结果
    if args.output:
        with open(args.output, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\n详细结果已保存到: {args.output}")
    else:
        print("\n详细结果:")
        print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()