import hashlib
import json
import math
from datetime import datetime
from typing import Sequence

from sqlalchemy.orm import Session

from app.models.category import Category
from app.models.journal_index import CategoryIndexState, CategoryJournalIndex, CategoryPageIndex


JOURNALS_PER_PAGE = 10


def _hash_journal_ids(journal_ids: Sequence[int]) -> str:
    payload = ",".join(str(journal_id) for journal_id in journal_ids)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class IndexService:
    """维护分类索引检测状态和分类-期刊关系."""

    def __init__(self, db: Session):
        self.db = db

    def update_category_state(self, category: Category, remote_total_count: int) -> CategoryIndexState:
        local_index_count = self.db.query(CategoryJournalIndex).filter(
            CategoryJournalIndex.category_id == category.id,
            CategoryJournalIndex.active == True,
        ).count()

        total_pages = math.ceil(remote_total_count / JOURNALS_PER_PAGE) if remote_total_count > 0 else 0
        last_page_count = remote_total_count % JOURNALS_PER_PAGE if total_pages > 0 else 0
        if total_pages > 0 and last_page_count == 0:
            last_page_count = JOURNALS_PER_PAGE

        estimated_new_count = max(remote_total_count - local_index_count, 0)
        if local_index_count == 0 and remote_total_count > 0:
            status = "missing_index"
        elif remote_total_count != local_index_count:
            status = "changed"
        else:
            status = "in_sync"

        state = self.db.query(CategoryIndexState).filter(
            CategoryIndexState.category_id == category.id
        ).first()
        if not state:
            state = CategoryIndexState(category_id=category.id, field_tag=category.field_tag)
            self.db.add(state)

        state.field_tag = category.field_tag
        state.remote_total_count = remote_total_count
        state.local_index_count = local_index_count
        state.total_pages = total_pages
        state.last_page_count = last_page_count
        state.estimated_new_count = estimated_new_count
        state.status = status
        state.last_checked_at = datetime.utcnow()
        self.db.commit()
        self.db.refresh(state)
        return state

    def record_list_page(self, category: Category, page_no: int, journals: list[dict]) -> int:
        now = datetime.utcnow()
        journal_ids = [int(item["journal_id"]) for item in journals if item.get("journal_id")]

        page_index = self.db.query(CategoryPageIndex).filter(
            CategoryPageIndex.category_id == category.id,
            CategoryPageIndex.page_no == page_no,
        ).first()
        if not page_index:
            page_index = CategoryPageIndex(category_id=category.id, page_no=page_no)
            self.db.add(page_index)

        page_index.item_count = len(journal_ids)
        page_index.first_journal_id = journal_ids[0] if journal_ids else None
        page_index.last_journal_id = journal_ids[-1] if journal_ids else None
        page_index.journal_ids_hash = _hash_journal_ids(journal_ids)
        page_index.updated_at = now

        new_links = 0
        for position_no, journal_id in enumerate(journal_ids, start=1):
            link = self.db.query(CategoryJournalIndex).filter(
                CategoryJournalIndex.category_id == category.id,
                CategoryJournalIndex.journal_id == journal_id,
            ).first()
            if not link:
                link = CategoryJournalIndex(
                    category_id=category.id,
                    journal_id=journal_id,
                    first_seen_at=now,
                )
                self.db.add(link)
                new_links += 1

            link.page_no = page_no
            link.position_no = position_no
            link.active = True
            link.last_seen_at = now

        state = self.db.query(CategoryIndexState).filter(
            CategoryIndexState.category_id == category.id
        ).first()
        if state:
            local_index_count = self.db.query(CategoryJournalIndex).filter(
                CategoryJournalIndex.category_id == category.id,
                CategoryJournalIndex.active == True,
            ).count()
            state.local_index_count = local_index_count
            state.estimated_new_count = max((state.remote_total_count or 0) - local_index_count, 0)
            state.status = "in_sync" if state.estimated_new_count == 0 else "changed"
            if state.status == "in_sync":
                state.last_full_scanned_at = now

        self.db.commit()
        return new_links
