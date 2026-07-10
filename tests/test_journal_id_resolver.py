import json

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.crawler.list_crawler import ListCrawler
from app.models.category import Category
from app.models.journal import Journal
from app.models.task import CrawlTask, TaskStatus, TaskType
from app.services.journal_id_resolver_service import JournalIdResolverService


@pytest.fixture
def resolver_db():
    original_type = Journal.__table__.c.detail_data.type
    Journal.__table__.c.detail_data.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Category.__table__.create(engine)
        Journal.__table__.create(engine)
        CrawlTask.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_type


def test_extract_journals_from_search_html_skips_empty_link_text():
    crawler = ListCrawler.__new__(ListCrawler)

    journals = crawler._extract_journals_from_html("""
        <table>
          <tr><td><a href="index.php?journalid=1&page=journalapp&view=detail"></a></td></tr>
          <tr><td><a href="index.php?journalid=2&page=journalapp&view=detail">Example Journal</a></td></tr>
        </table>
    """)

    assert journals == [{"journal_id": 2, "name": "Example Journal"}]


def test_choose_candidate_rejects_non_matching_search_result():
    selected = JournalIdResolverService.choose_candidate(
        old_journal_id=1,
        journal_name="Target Journal",
        candidates=[{"journal_id": 2, "name": "Different Journal"}],
    )

    assert selected is None


def test_choose_candidate_distinguishes_non_latin_names():
    selected = JournalIdResolverService.choose_candidate(
        old_journal_id=1,
        journal_name="目标期刊",
        candidates=[{"journal_id": 2, "name": "其他期刊"}],
    )

    assert selected is None


def test_choose_category_candidate_rejects_unrelated_new_journal():
    selected = JournalIdResolverService.choose_category_candidate(
        old_journal_id=1,
        journal_name="Target Journal",
        candidates=[
            {
                "journal_id": 2,
                "name": "Unrelated New Journal",
                "exists_in_db": False,
            }
        ],
    )

    assert selected is None


@pytest.mark.asyncio
async def test_search_by_name_posts_form_and_parses_candidates(monkeypatch):
    crawler = ListCrawler()
    captured = {}

    class Response:
        status_code = 200
        text = """
            <table>
              <tr><td><a href="index.php?journalid=999&page=journalapp&view=detail">Example Journal</a></td></tr>
            </table>
        """

    async def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["data"] = kwargs["data"]
        return Response()

    monkeypatch.setattr(crawler, "request_http", fake_request)

    candidates = await crawler.search_by_name("Example Journal")

    assert captured["method"] == "POST"
    assert captured["data"]["searchname"] == "Example Journal"
    assert candidates == [{"journal_id": 999, "name": "Example Journal"}]


def test_apply_resolution_keeps_old_target_and_sets_fetch_journal_id(resolver_db):
    journal = Journal(
        journal_id=3653,
        name="International Journal of Advanced Robotic Systems",
        detail_crawled=True,
    )
    task = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="3653",
        target_url="old",
        status=TaskStatus.FAILED.value,
        retry_count=3,
        max_retry=3,
        error_message="期刊 3653 详情质量检查未通过: detail_missing_identity",
        extra_data=json.dumps({"journal_id": 3653, "category_id": 5}),
    )
    resolver_db.add_all([journal, task])
    resolver_db.commit()

    result = JournalIdResolverService(resolver_db).apply_resolution(
        old_journal_id=3653,
        new_journal_id=9999,
        journal_name=journal.name,
        candidates=[{"journal_id": 9999, "name": journal.name}],
        dry_run=False,
    )

    resolver_db.refresh(journal)
    resolver_db.refresh(task)
    extra_data = json.loads(task.extra_data)

    assert result["status"] == "updated"
    assert journal.detail_crawled is False
    assert task.target_id == "3653"
    assert "journalid=9999" in task.target_url
    assert task.status == TaskStatus.PENDING.value
    assert task.retry_count == 0
    assert task.error_message is None
    assert extra_data["journal_id"] == 3653
    assert extra_data["fetch_journal_id"] == 9999
    assert extra_data["journal_id_resolution"]["method"] == "letpub_name_search"


@pytest.mark.asyncio
async def test_resolve_one_skips_non_quality_failed_detail_task(resolver_db, monkeypatch):
    resolver_db.add_all([
        Journal(journal_id=1, name="Example Journal"),
        CrawlTask(
            task_type=TaskType.DETAIL.value,
            target_id="1",
            status=TaskStatus.FAILED.value,
            error_message="network error",
        ),
    ])
    resolver_db.commit()

    async def fail_search(_name):
        raise AssertionError("non-quality failures must not trigger remote search")

    service = JournalIdResolverService(resolver_db)
    monkeypatch.setattr(service, "search_by_journal_name", fail_search)

    result = await service.resolve_one(1, dry_run=True)

    assert result["status"] == "not_detail_quality_failure"


@pytest.mark.asyncio
async def test_resolve_one_falls_back_to_category_scan(resolver_db, monkeypatch):
    category = Category(id=5, field_tag="4", name="Info", total_count=1)
    journal = Journal(
        journal_id=3653,
        name="International Journal of Advanced Robotic Systems",
        category_id=5,
        detail_crawled=True,
    )
    task = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="3653",
        status=TaskStatus.FAILED.value,
        retry_count=3,
        error_message="期刊 3653 详情质量检查未通过: detail_missing_identity",
    )
    resolver_db.add_all([category, journal, task])
    resolver_db.commit()

    service = JournalIdResolverService(resolver_db)

    async def empty_search(_name):
        return []

    async def category_scan(_category):
        return [
            {
                "journal_id": 10279,
                "name": "International Journal of Advanced Robotic Systems",
                "field_tag": "4",
                "page": 2,
                "position_no": 5,
            }
        ]

    monkeypatch.setattr(service, "search_by_journal_name", empty_search)
    monkeypatch.setattr(service, "crawl_category_journals", category_scan)

    result = await service.resolve_one(3653, dry_run=False)

    resolver_db.refresh(task)
    extra_data = json.loads(task.extra_data)
    assert result["status"] == "updated"
    assert result["selected_candidate"]["journal_id"] == 10279
    assert extra_data["fetch_journal_id"] == 10279
    assert extra_data["journal_id_resolution"]["method"] == "category_full_scan"


@pytest.mark.asyncio
async def test_resolve_one_keeps_unresolvable_quality_task(resolver_db, monkeypatch):
    category = Category(id=5, field_tag="4", name="Info", total_count=1)
    journal = Journal(
        journal_id=3653,
        name="Missing Journal",
        category_id=5,
        detail_crawled=False,
    )
    task = CrawlTask(
        task_type=TaskType.DETAIL.value,
        target_id="3653",
        status=TaskStatus.FAILED.value,
        error_message="期刊 3653 详情质量检查未通过: detail_missing_identity",
    )
    resolver_db.add_all([category, journal, task])
    resolver_db.commit()

    service = JournalIdResolverService(resolver_db)

    async def empty_search(_name):
        return []

    async def empty_category_scan(_category):
        return []

    monkeypatch.setattr(service, "search_by_journal_name", empty_search)
    monkeypatch.setattr(service, "crawl_category_journals", empty_category_scan)

    result = await service.resolve_one(3653, dry_run=False)

    assert result["status"] == "unresolved"
    assert len(result["failed_tasks"]) == 1
    assert resolver_db.query(CrawlTask).filter_by(id=task.id).first() is not None
    assert resolver_db.query(Journal).filter_by(journal_id=3653).first() is not None
