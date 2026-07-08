"""IndexService behavior."""

import pytest
from sqlalchemy import JSON, create_engine
from sqlalchemy.orm import sessionmaker

from app.models.category import Category
from app.models.journal import Journal
from app.models.journal_index import CategoryIndexState, CategoryJournalIndex, CategoryPageIndex
from app.services.index_service import IndexService


@pytest.fixture
def index_db():
    original_type = Journal.__table__.c.detail_data.type
    Journal.__table__.c.detail_data.type = JSON()
    engine = create_engine("sqlite:///:memory:")
    try:
        Category.__table__.create(engine)
        Journal.__table__.create(engine)
        CategoryIndexState.__table__.create(engine)
        CategoryPageIndex.__table__.create(engine)
        CategoryJournalIndex.__table__.create(engine)
        SessionLocal = sessionmaker(bind=engine)
        session = SessionLocal()
        yield session
        session.close()
    finally:
        Journal.__table__.c.detail_data.type = original_type


def test_record_list_page_creates_missing_journals(index_db):
    category = Category(field_tag="10", name="Category A", total_count=2)
    index_db.add(category)
    index_db.commit()
    index_db.refresh(category)

    new_links = IndexService(index_db).record_list_page(
        category,
        page_no=1,
        journals=[
            {"journal_id": 101, "name": "Journal A"},
            {"journal_id": 102, "name": "Journal B"},
        ],
    )

    assert new_links == 2
    assert index_db.query(Journal).filter(Journal.journal_id.in_([101, 102])).count() == 2
    assert index_db.query(CategoryJournalIndex).filter_by(category_id=category.id).count() == 2
