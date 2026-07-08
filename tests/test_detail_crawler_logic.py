"""Unit tests for DetailCrawler pure-logic methods (no browser required)."""
import pytest
from datetime import datetime

from app.crawler.detail_crawler import DetailCrawler, DataValidationError


@pytest.fixture
def crawler():
    return DetailCrawler.__new__(DetailCrawler)


# ── _normalize_key ────────────────────────────────────────────────────────────

def test_normalize_key_strips_trailing_colon(crawler):
    assert crawler._normalize_key("期刊ISSN：") == "期刊ISSN"


def test_normalize_key_strips_trailing_ascii_colon(crawler):
    assert crawler._normalize_key("ISSN:") == "ISSN"


def test_normalize_key_replaces_spaces_with_underscore(crawler):
    assert crawler._normalize_key("JCR 分区") == "JCR_分区"


def test_normalize_key_empty_returns_empty(crawler):
    assert crawler._normalize_key("") == ""


def test_normalize_key_strips_whitespace(crawler):
    assert crawler._normalize_key("  影响因子  ") == "影响因子"


# ── _normalize_info ───────────────────────────────────────────────────────────

def test_normalize_info_maps_issn(crawler):
    result = crawler._normalize_info({"期刊ISSN": "1234-5678"})
    assert result["issn"] == "1234-5678"


def test_normalize_info_extracts_impact_factor_float(crawler):
    result = crawler._normalize_info({"最新影响因子": "3.5 (2023)"})
    assert result["impact_factor"] == 3.5


def test_normalize_info_extracts_latest_if_float(crawler):
    result = crawler._normalize_info({"2025-2026最新IF（数据来源于网友提供）": "4.199"})
    assert result["impact_factor"] == 4.199


def test_normalize_info_impact_factor_no_number_becomes_none(crawler):
    result = crawler._normalize_info({"最新影响因子": "暂无"})
    assert result["impact_factor"] is None


def test_normalize_info_maps_jcr_partition(crawler):
    result = crawler._normalize_info({"JCR分区": "Q1"})
    assert result["jcr_partition"] == "Q1"


def test_normalize_info_preserves_original_keys(crawler):
    info = {"期刊ISSN": "0000-0001", "出版商": "Elsevier"}
    result = crawler._normalize_info(info)
    assert "期刊ISSN" in result
    assert "出版商" in result
    assert result["publisher"] == "Elsevier"


def test_normalize_info_maps_cas_partition(crawler):
    result = crawler._normalize_info({"中国科学院期刊分区（2025年3月最新升级版）": "1区"})
    assert result["cas_partition"] == "1区"


# ── _validate_basic_info ──────────────────────────────────────────────────────

def test_validate_empty_info_raises(crawler):
    with pytest.raises(DataValidationError) as exc_info:
        crawler._validate_basic_info({}, 1)
    assert exc_info.value.extracted_fields == 0


def test_validate_too_few_fields_raises(crawler):
    with pytest.raises(DataValidationError) as exc_info:
        crawler._validate_basic_info({"a": 1, "b": 2, "c": 3, "d": 4}, 1)
    assert exc_info.value.extracted_fields == 4


def test_validate_min_fields_passes(crawler):
    info = {"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}
    crawler._validate_basic_info(info, 1)  # should not raise


def test_validate_with_required_field_passes(crawler):
    info = {"期刊ISSN": "1234-5678", "b": 2, "c": 3, "d": 4, "e": 5}
    crawler._validate_basic_info(info, 1)  # should not raise


# ── HTTP HTML detail extraction ───────────────────────────────────────────────

SAMPLE_DETAIL_HTML = """
<html><body>
  <div id="yxyz_content">
    <table><tr><td>not target</td></tr></table>
    <table>
      <tr><td colspan="2">基本信息</td></tr>
      <tr><td>期刊名字</td><td>Example Journal<script>ignore()</script></td></tr>
      <tr><td>期刊ISSN：</td><td>1234-5678</td></tr>
      <tr><td>2025-2026最新IF（数据来源于网友提供）</td><td>4.199</td></tr>
      <tr><td>出版商</td><td>Example Publisher</td></tr>
      <tr><td>CiteScore</td><td><table>
        <tr><td>CiteScore</td><td>9.80</td></tr>
      </table></td></tr>
    </table>
  </div>
</body></html>
"""


def test_extract_basic_info_from_http_html(crawler):
    result = crawler._extract_basic_info_from_html(SAMPLE_DETAIL_HTML)
    assert result["期刊名字"] == "Example Journal"
    assert result["issn"] == "1234-5678"
    assert result["impact_factor"] == 4.199
    assert result["publisher"] == "Example Publisher"
    assert result["CiteScore"]["tables"][0]["CiteScore"] == "9.80"


# ── _parse_comment_from_api ───────────────────────────────────────────────────

SAMPLE_COMMENT_HTML = """<div>
  <span>#3楼</span>
  <a title="去查看Ta的个人主页">张三</a>
  <div>期刊评分</div><div></div><div>4.5</div>
  <strong>研究方向</strong>：材料科学
  <strong>投稿结果</strong>：录用
  <strong>投稿周期</strong>：3个月
  <strong>发表时间</strong>：2023-01-15 10:00:00
  <strong>最后更新</strong>：2023-02-01 12:00:00
  <blockquote class="layui-elem-quote">投稿经验：审稿很快，推荐投稿。</blockquote>
</div>"""


def test_parse_comment_floor(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert result["floor"] == "#3楼"


def test_parse_comment_author(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert result["author"] == "张三"


def test_parse_comment_content(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert "审稿很快" in result["content"]


def test_parse_comment_publish_time(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert result["publish_time"] == datetime(2023, 1, 15, 10, 0, 0)


def test_parse_comment_update_time(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert result["update_time"] == datetime(2023, 2, 1, 12, 0, 0)


def test_parse_comment_comment_id_is_uuid(crawler):
    result = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    import uuid
    uuid.UUID(result["comment_id"])  # raises if invalid


def test_parse_comment_empty_content_returns_none(crawler):
    result = crawler._parse_comment_from_api(1, {"content": ""})
    assert result is None


def test_parse_comment_missing_content_key_returns_none(crawler):
    result = crawler._parse_comment_from_api(1, {})
    assert result is None


def test_parse_comment_anonymous_when_no_author(crawler):
    html = """<div>
      <span>#1楼</span>
      <strong>投稿结果</strong>：录用
      <blockquote class="layui-elem-quote">投稿经验：不错。</blockquote>
    </div>"""
    result = crawler._parse_comment_from_api(1, {"content": html})
    assert result["author"] == "匿名"


def test_parse_comment_no_experience_but_has_result(crawler):
    html = """<div>
      <span>#2楼</span>
      <a title="去查看Ta的个人主页">李四</a>
      <strong>投稿结果</strong>：拒稿
    </div>"""
    result = crawler._parse_comment_from_api(1, {"content": html})
    assert result is not None
    assert result["submission_result"] is not None


def test_parse_comment_uuid_deterministic(crawler):
    r1 = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    r2 = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    assert r1["comment_id"] == r2["comment_id"]


def test_parse_comment_uuid_differs_by_journal(crawler):
    r1 = crawler._parse_comment_from_api(1, {"content": SAMPLE_COMMENT_HTML})
    r2 = crawler._parse_comment_from_api(2, {"content": SAMPLE_COMMENT_HTML})
    assert r1["comment_id"] != r2["comment_id"]


# ── _build_detail_url ─────────────────────────────────────────────────────────

def test_build_detail_url_contains_journal_id(crawler):
    url = crawler._build_detail_url(8800)
    assert "journalid=8800" in url
    assert "view=detail" in url
