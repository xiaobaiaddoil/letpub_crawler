from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.journals import router
from app.database import get_db
from app.models.category import Category
from app.models.comment import Comment
from app.models.journal import Journal


@pytest.fixture
def journal_db():
    original_type = Journal.__table__.c.detail_data.type
    Journal.__table__.c.detail_data.type = JSON()
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        Category.__table__.create(engine)
        Journal.__table__.create(engine)
        Comment.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()

        category = Category(field_tag="bio", name="Biology", total_count=3)
        session.add(category)
        session.commit()
        session.refresh(category)

        session.add_all(
            [
                Journal(
                    journal_id=1001,
                    name="Nature Cell Biology",
                    issn="1465-7392",
                    eissn="1476-4679",
                    impact_factor=28.2,
                    impact_factor_realtime=29.1,
                    jcr_partition="Q1 Biology",
                    cas_partition="一区 生物学",
                    review_speed="2 weeks",
                    acceptance_rate="10%",
                    category_id=category.id,
                    detail_crawled=True,
                    comments_crawled=True,
                    detail_data={
                        "publisher": "Nature Portfolio",
                        "review_speed": "2 weeks",
                        "scope": "cell biology",
                    },
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 2),
                ),
                Journal(
                    journal_id=1002,
                    name="Journal of Slow Tests",
                    issn="0000-0001",
                    impact_factor=3.5,
                    impact_factor_realtime=3.2,
                    jcr_partition="Q3 Testing",
                    cas_partition="三区 测试",
                    review_speed="8 months",
                    acceptance_rate="50%",
                    category_id=category.id,
                    detail_crawled=True,
                    comments_crawled=False,
                    detail_data={
                        "publisher": "Example Press",
                        "review_speed": "8 months",
                    },
                    created_at=datetime(2026, 1, 3),
                    updated_at=datetime(2026, 1, 4),
                ),
                Journal(
                    journal_id=1003,
                    name="Cell Reports Medicine",
                    issn="2666-3791",
                    impact_factor=15.0,
                    impact_factor_realtime=15.5,
                    jcr_partition="Q1 Medicine",
                    cas_partition="二区 医学",
                    review_speed="4 weeks",
                    acceptance_rate="20%",
                    category_id=category.id,
                    detail_crawled=False,
                    comments_crawled=False,
                    detail_data=None,
                    created_at=datetime(2026, 1, 5),
                    updated_at=datetime(2026, 1, 6),
                ),
            ]
        )
        session.add_all(
            [
                Comment(journal_id=1001, comment_id="c1", content="good"),
                Comment(journal_id=1001, comment_id="c2", content="fast"),
            ]
        )
        session.commit()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_type


@pytest.fixture
def client(journal_db):
    app = FastAPI()
    app.include_router(router)

    def override_get_db():
        yield journal_db

    app.dependency_overrides[get_db] = override_get_db
    return TestClient(app)


def test_searches_by_journal_name_and_returns_comment_count(client):
    response = client.get("/api/journals?name=nature")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["journal_id"] == 1001
    assert body["items"][0]["comment_count"] == 2
    assert body["items"][0]["updated_at"].startswith("2026-01-02T00:00:00")


def test_filters_by_impact_factor_range(client):
    response = client.get(
        "/api/journals",
        params={
            "impact_factor_min": 10,
            "impact_factor_max": 20,
            "sort": "impact_factor",
            "order": "asc",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert [item["journal_id"] for item in body["items"]] == [1003]


def test_searches_detail_json_field(client):
    response = client.get(
        "/api/journals",
        params={
            "detail_field": "publisher",
            "detail_value": "nature",
            "detail_op": "contains",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["journal_id"] == 1001


def test_rejects_invalid_impact_factor_range(client):
    response = client.get("/api/journals?impact_factor_min=20&impact_factor_max=10")

    assert response.status_code == 400
    assert response.json()["detail"] == "impact_factor_min 不能大于 impact_factor_max"


def test_only_journal_list_query_is_exposed(client):
    assert client.get("/api/journals/search-fields").status_code == 404
    assert client.get("/api/journals/1001").status_code == 404


def test_detail_field_without_value_acts_as_exists_query(client):
    response = client.get(
        "/api/journals",
        params={
            "detail_field": "publisher",
            "detail_op": "exists",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {item["journal_id"] for item in body["items"]} == {1001, 1002}


def test_typo_fuzzy_option_keeps_regular_name_matching(client):
    response = client.get(
        "/api/journals",
        params={
            "name": "nature",
            "typo_fuzzy": "true",
            "fuzzy_threshold": 0.25,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["journal_id"] == 1001
