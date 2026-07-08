from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.data import (
    JournalListResponse,
    _journal_to_response,
)
from app.database import get_db
from app.models.comment import Comment
from app.models.journal import Journal
from app.services.journal_search import (
    apply_journal_filters,
    apply_journal_sort,
)

router = APIRouter(prefix="/api/journals", tags=["journals"])


def _validate_range(field: str, min_value: Optional[float], max_value: Optional[float]) -> None:
    if min_value is not None and max_value is not None and min_value > max_value:
        raise HTTPException(status_code=400, detail=f"{field}_min 不能大于 {field}_max")


def _comment_counts(db: Session, journal_ids: list[int]) -> dict[int, int]:
    if not journal_ids:
        return {}
    return dict(
        db.query(Comment.journal_id, func.count(Comment.id))
        .filter(Comment.journal_id.in_(journal_ids))
        .group_by(Comment.journal_id)
        .all()
    )


@router.get("", response_model=JournalListResponse)
def list_journals(
    q: Optional[str] = Query(None, description="通用模糊检索：期刊名、ISSN、E-ISSN、期刊ID"),
    name: Optional[str] = Query(None, description="按期刊名模糊检索"),
    issn: Optional[str] = Query(None, description="按 ISSN 或 E-ISSN 模糊检索"),
    category_id: Optional[int] = Query(None, description="分类ID"),
    detail_crawled: Optional[bool] = Query(None, description="是否已爬取详情"),
    comments_crawled: Optional[bool] = Query(None, description="是否已爬取评论"),
    impact_factor_min: Optional[float] = Query(None, ge=0, description="影响因子下限"),
    impact_factor_max: Optional[float] = Query(None, ge=0, description="影响因子上限"),
    impact_factor_realtime_min: Optional[float] = Query(None, ge=0, description="实时影响因子下限"),
    impact_factor_realtime_max: Optional[float] = Query(None, ge=0, description="实时影响因子上限"),
    jcr_partition: Optional[str] = Query(None, description="JCR 分区文本检索"),
    cas_partition: Optional[str] = Query(None, description="中科院分区文本检索"),
    cas_warning: Optional[str] = Query(None, description="CAS 预警文本检索"),
    citescore: Optional[str] = Query(None, description="CiteScore 文本检索"),
    review_speed: Optional[str] = Query(None, description="审稿速度文本检索"),
    acceptance_rate: Optional[str] = Query(None, description="录用比例文本检索"),
    self_citation_rate: Optional[str] = Query(None, description="自引率文本检索"),
    detail_text: Optional[str] = Query(None, description="完整详情 JSON 文本检索"),
    detail_field: Optional[str] = Query(None, description="完整详情 JSON 顶层字段名"),
    detail_value: Optional[str] = Query(None, description="完整详情 JSON 字段值"),
    detail_op: str = Query("contains", pattern="^(contains|equals|exists)$", description="详情字段匹配方式"),
    typo_fuzzy: bool = Query(False, description="启用期刊名拼写错误容错查询"),
    fuzzy_threshold: float = Query(0.3, ge=0.1, le=1.0, description="拼写容错相似度阈值"),
    sort: str = Query("impact_factor", pattern="^(impact_factor|impact_factor_realtime|name|journal_id|created_at|updated_at)$"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    """高级期刊检索。"""
    _validate_range("impact_factor", impact_factor_min, impact_factor_max)
    _validate_range("impact_factor_realtime", impact_factor_realtime_min, impact_factor_realtime_max)

    query = apply_journal_filters(
        db.query(Journal),
        db,
        q=q,
        name=name,
        issn=issn,
        category_id=category_id,
        detail_crawled=detail_crawled,
        comments_crawled=comments_crawled,
        impact_factor_min=impact_factor_min,
        impact_factor_max=impact_factor_max,
        impact_factor_realtime_min=impact_factor_realtime_min,
        impact_factor_realtime_max=impact_factor_realtime_max,
        jcr_partition=jcr_partition,
        cas_partition=cas_partition,
        cas_warning=cas_warning,
        citescore=citescore,
        review_speed=review_speed,
        acceptance_rate=acceptance_rate,
        self_citation_rate=self_citation_rate,
        detail_text=detail_text,
        detail_field=detail_field,
        detail_value=detail_value,
        detail_op=detail_op,
        typo_fuzzy=typo_fuzzy,
        fuzzy_threshold=fuzzy_threshold,
    )

    total = query.count()
    offset = (page - 1) * size
    journals = apply_journal_sort(query, sort, order).offset(offset).limit(size).all()
    counts = _comment_counts(db, [journal.journal_id for journal in journals])

    return JournalListResponse(
        total=total,
        page=page,
        size=size,
        items=[
            _journal_to_response(journal, counts.get(journal.journal_id, 0))
            for journal in journals
        ],
    )
