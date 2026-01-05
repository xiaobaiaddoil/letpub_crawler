"""
测试评论API - 验证Cookie是否可以获取评论

使用方法:
    python -m tests.test_comments_api --journal-id 8800 --cookie "PHPSESSID=q9lavd4u1gqcqdemq904cvdsh3"
"""

import asyncio
import argparse
import json
import logging
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def test_comments_api(journal_id: int, cookie: str):
    """测试评论API是否可以获取数据"""

    logger.info("=" * 60)
    logger.info(f"测试评论API - Journal ID: {journal_id}")
    logger.info("=" * 60)

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=False)  # 可视化模式

    try:
        # 创建浏览器上下文
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )

        # 设置Cookie
        if cookie:
            cookies = []
            for item in cookie.split(";"):
                item = item.strip()
                if "=" in item:
                    name, value = item.split("=", 1)
                    cookies.append({
                        "name": name.strip(),
                        "value": value.strip(),
                        "domain": ".letpub.com.cn",
                        "path": "/"
                    })

            await context.add_cookies(cookies)
            logger.info(f"✓ 已设置 {len(cookies)} 个Cookie")

        page = await context.new_page()

        # 先访问期刊详情页（建立session）
        detail_url = f"https://www.letpub.com.cn/index.php?journalid={journal_id}&page=journalapp&view=detail"
        logger.info(f"访问期刊详情页: {detail_url}")
        await page.goto(detail_url, wait_until="networkidle")
        await asyncio.sleep(2)

        # 测试API请求
        api_url = "https://www.letpub.com.cn/journalappAjax_comments_center.php"

        results = {
            "journal_id": journal_id,
            "cookie": cookie[:20] + "...",
            "pages_tested": [],
            "total_comments": 0,
            "api_works": False
        }

        # 测试多个页面
        for page_num in range(1, 4):  # 测试前3页
            logger.info(f"\n{'='*50}")
            logger.info(f"测试第 {page_num} 页评论")
            logger.info(f"{'='*50}")

            params = {
                "action": "getdetailscommentslistflow",
                "journalid": str(journal_id),
                "sorttype": "undefined",
                "page": str(page_num)
            }

            try:
                # 使用fetch API发起请求
                response_text = await page.evaluate('''async (args) => {
                    const url = new URL(args.url);
                    Object.keys(args.params).forEach(key =>
                        url.searchParams.append(key, args.params[key])
                    );

                    console.log("请求URL:", url.toString());

                    const response = await fetch(url.toString(), {
                        credentials: 'include',  // 包含cookie
                        headers: {
                            'Accept': 'application/json, text/javascript, */*; q=0.01',
                            'X-Requested-With': 'XMLHttpRequest'
                        }
                    });

                    const text = await response.text();
                    console.log("响应状态:", response.status);
                    console.log("响应文本:", text.substring(0, 200));

                    return text;
                }''', {"url": api_url, "params": params})

                logger.info(f"API响应长度: {len(response_text)} 字符")
                logger.info(f"响应前100字符: {response_text[:100]}")

                # 解析JSON
                try:
                    data = json.loads(response_text)

                    page_result = {
                        "page": page_num,
                        "code": data.get("code"),
                        "msg": data.get("msg"),
                        "count": data.get("count", 0),
                        "data_length": len(data.get("data", []))
                    }

                    results["pages_tested"].append(page_result)

                    logger.info(f"✓ 成功解析JSON")
                    logger.info(f"  Code: {page_result['code']}")
                    logger.info(f"  Message: {page_result['msg']}")
                    logger.info(f"  Total Count: {page_result['count']}")
                    logger.info(f"  Data Length: {page_result['data_length']}")

                    if page_result['code'] == 0:
                        results["api_works"] = True

                        # 显示评论详情
                        comment_data = data.get("data", [])
                        results["total_comments"] += len(comment_data)

                        if comment_data:
                            logger.info(f"\n评论列表 (前3条):")
                            for i, comment in enumerate(comment_data[:3], 1):
                                # 提取评论内容（去除HTML标签）
                                import re
                                content_html = comment.get("content", "")
                                content_text = re.sub(r'<[^>]+>', '', content_html).strip()

                                logger.info(f"\n  [{i}] 内容预览: {content_text[:100]}...")
                                logger.info(f"      完整数据: {json.dumps(comment, ensure_ascii=False)[:200]}...")

                        # 如果没有更多评论，停止
                        if not comment_data:
                            logger.info(f"第 {page_num} 页没有评论，停止测试")
                            break
                    else:
                        logger.warning(f"✗ API返回错误")
                        logger.warning(f"  错误信息: {page_result['msg']}")
                        break

                except json.JSONDecodeError as e:
                    logger.error(f"✗ JSON解析失败: {e}")
                    logger.error(f"  响应文本: {response_text[:500]}")
                    results["pages_tested"].append({
                        "page": page_num,
                        "error": "JSON解析失败",
                        "response_preview": response_text[:200]
                    })
                    break

            except Exception as e:
                logger.error(f"✗ 请求失败: {e}")
                import traceback
                traceback.print_exc()
                results["pages_tested"].append({
                    "page": page_num,
                    "error": str(e)
                })
                break

            # 延迟避免请求过快
            await asyncio.sleep(1)

        # 输出测试结果
        logger.info("\n" + "=" * 60)
        logger.info("测试结果汇总")
        logger.info("=" * 60)
        logger.info(f"API是否可用: {'✓ 是' if results['api_works'] else '✗ 否'}")
        logger.info(f"测试页数: {len(results['pages_tested'])}")
        logger.info(f"获取评论总数: {results['total_comments']}")

        # 保存结果
        output_file = f"test_comments_api_{journal_id}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"\n详细结果已保存到: {output_file}")

        # 等待用户查看
        logger.info("\n按 Enter 键关闭浏览器...")
        input()

        return results

    finally:
        await context.close()
        await browser.close()
        await playwright.stop()


def main():
    parser = argparse.ArgumentParser(description="测试评论API")
    parser.add_argument("--journal-id", type=int, required=True, help="期刊ID")
    parser.add_argument("--cookie", type=str, required=True, help="Cookie字符串 (如: PHPSESSID=xxx)")

    args = parser.parse_args()

    # 运行测试
    results = asyncio.run(test_comments_api(args.journal_id, args.cookie))

    # 输出简要结果
    print("\n" + "=" * 60)
    if results["api_works"]:
        print("✓ 测试成功! API可以正常获取评论")
        print(f"  获取到 {results['total_comments']} 条评论")
    else:
        print("✗ 测试失败! API无法获取评论")
        print("  可能的原因:")
        print("  1. Cookie已过期或无效")
        print("  2. 需要登录才能查看评论")
        print("  3. API接口变更")
    print("=" * 60)


if __name__ == "__main__":
    main()