from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime
from app.database import get_db
from app.models.category import Category
from app.models.journal import Journal
from app.models.comment import Comment

router = APIRouter(prefix="/api/data", tags=["data"])

class CategoryResponse(BaseModel):
    id: int
    field_tag: str
    name: str
    total_count: int
    created_at: datetime

    class Config:
        from_attributes = True

class JournalResponse(BaseModel):
    id: int
    journal_id: int
    name: str
    issn: Optional[str]
    eissn: Optional[str]
    impact_factor: Optional[float]
    impact_factor_realtime: Optional[float]
    self_citation_rate: Optional[str]
    jcr_partition: Optional[str]
    cas_partition: Optional[str]
    cas_warning: Optional[str]
    citescore: Optional[str]
    review_speed: Optional[str]
    acceptance_rate: Optional[str]
    detail_crawled: bool
    comments_crawled: bool
    category_id: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True

class JournalDetailResponse(JournalResponse):
    """期刊详情响应（包含评论数量）"""
    comment_count: int = 0

class CommentResponse(BaseModel):
    id: int
    journal_id: int
    comment_id: str
    content: Optional[str]
    author: Optional[str]
    rating: Optional[str]
    comment_time: Optional[datetime]
    submit_experience: Optional[str]
    crawled_at: datetime

    class Config:
        from_attributes = True

class JournalListResponse(BaseModel):
    """期刊列表响应（包含分页信息）"""
    total: int
    page: int
    size: int
    items: List[JournalResponse]

class DataStatsResponse(BaseModel):
    categories: int
    journals: int
    journals_with_detail: int
    comments: int

@router.get("/stats", response_model=DataStatsResponse)
def get_data_stats(db: Session = Depends(get_db)):
    """获取数据统计"""
    categories = db.query(Category).count()
    journals = db.query(Journal).count()
    journals_with_detail = db.query(Journal).filter(Journal.detail_crawled == True).count()
    comments = db.query(Comment).count()

    return DataStatsResponse(
        categories=categories,
        journals=journals,
        journals_with_detail=journals_with_detail,
        comments=comments
    )

@router.get("/categories", response_model=List[CategoryResponse])
def list_categories(db: Session = Depends(get_db)):
    """获取分类列表"""
    return db.query(Category).order_by(Category.name).all()

@router.get("/journals", response_model=JournalListResponse)
def list_journals(
    category_id: Optional[int] = None,
    search: Optional[str] = None,  # 搜索期刊名称或ISSN
    detail_crawled: Optional[bool] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取期刊列表（支持名称/ISSN模糊搜索）"""
    from sqlalchemy import or_

    query = db.query(Journal)

    if category_id:
        query = query.filter(Journal.category_id == category_id)

    if search:
        # 支持期刊名称和ISSN的模糊搜索
        search_pattern = f"%{search}%"
        query = query.filter(
            or_(
                Journal.name.ilike(search_pattern),
                Journal.issn.ilike(search_pattern),
                Journal.eissn.ilike(search_pattern)
            )
        )

    if detail_crawled is not None:
        query = query.filter(Journal.detail_crawled == detail_crawled)

    # 获取总数
    total = query.count()

    # 分页查询
    offset = (page - 1) * size
    journals = query.order_by(
        Journal.impact_factor.desc().nullslast()
    ).offset(offset).limit(size).all()

    return JournalListResponse(
        total=total,
        page=page,
        size=size,
        items=journals
    )

@router.get("/journals/{journal_id}", response_model=JournalResponse)
def get_journal(journal_id: int, db: Session = Depends(get_db)):
    """获取期刊详情"""
    journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
    if not journal:
        raise HTTPException(status_code=404, detail="期刊不存在")
    return journal

@router.get("/journals/{journal_id}/comments", response_model=List[CommentResponse])
def get_journal_comments(
    journal_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取期刊评论"""
    journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
    if not journal:
        raise HTTPException(status_code=404, detail="期刊不存在")

    offset = (page - 1) * size
    return db.query(Comment).filter(
        Comment.journal_id == journal.id
    ).order_by(Comment.crawled_at.desc()).offset(offset).limit(size).all()

@router.get("/export/journals")
def export_journals(
    category_id: Optional[int] = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    db: Session = Depends(get_db)
):
    """导出期刊数据"""
    query = db.query(Journal)
    if category_id:
        query = query.filter(Journal.category_id == category_id)

    journals = query.all()

    if format == "csv":
        import csv
        import io
        from fastapi.responses import StreamingResponse

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "期刊ID", "名称", "ISSN", "E-ISSN",
            "影响因子", "JCR分区", "中科院分区", "审稿速度", "录用比例"
        ])
        for j in journals:
            writer.writerow([
                j.id, j.journal_id, j.name, j.issn, j.eissn,
                j.impact_factor, j.jcr_partition, j.cas_partition,
                j.review_speed, j.acceptance_rate
            ])

        output.seek(0)
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=journals.csv"}
        )

    return [JournalResponse.model_validate(j) for j in journals]
