from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, or_
from typing import Any, List, Optional
from pydantic import BaseModel
from datetime import datetime
import csv
import io
import json
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
    comment_count: int = 0
    jcr_partition_summary: Optional[str] = None
    cas_partition_summary: Optional[str] = None
    citescore_summary: Optional[str] = None

    class Config:
        from_attributes = True

class JournalDetailResponse(JournalResponse):
    """期刊详情响应（包含完整详情数据）"""
    page_comment_count: Optional[int] = None
    detail_data: Optional[dict[str, Any]] = None

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


def _parse_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return None
    if text[0] not in "[{":
        return value

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _summary_text(value: Any) -> str:
    if isinstance(value, dict):
        return " / ".join(f"{key}: {_summary_text(item)}" for key, item in value.items())
    if isinstance(value, list):
        return " / ".join(_summary_text(item) for item in value)
    return str(value)


def _compact_text(value: Any, limit: int = 90) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        text = _summary_text(value)
    else:
        text = str(value)
    text = text.strip()
    if not text:
        return None
    return text if len(text) <= limit else f"{text[:limit]}..."


def _first_table_row(data: dict[str, Any]) -> dict[str, Any]:
    tables = data.get("tables") or []
    if not isinstance(tables, list):
        return {}

    for table in tables:
        if isinstance(table, dict):
            return table
        if isinstance(table, list):
            for row in table:
                if isinstance(row, dict):
                    return row
    return {}


def _join_parts(parts: list[Any], limit: int = 120) -> Optional[str]:
    seen = []
    for part in parts:
        text = _compact_text(part, limit)
        if text and text not in seen:
            seen.append(text)
    return _compact_text(" / ".join(seen), limit) if seen else None


def _summarize_jcr(value: Any) -> Optional[str]:
    data = _parse_json_value(value)
    if isinstance(data, dict):
        row = _first_table_row(data)
        return _join_parts([
            data.get("text"),
            row.get("JIF分区"),
            row.get("JIF排名"),
            row.get("按JIF指标学科分区"),
        ])
    return _compact_text(data)


def _summarize_citescore(value: Any) -> Optional[str]:
    data = _parse_json_value(value)
    if isinstance(data, dict):
        row = _first_table_row(data)
        ranks = row.get("CiteScore排名") if isinstance(row, dict) else None
        first_rank = ranks[0] if isinstance(ranks, list) and ranks else {}
        return _join_parts([
            f"CiteScore {row.get('CiteScore')}" if row.get("CiteScore") else None,
            first_rank.get("分区") if isinstance(first_rank, dict) else None,
            first_rank.get("学科") if isinstance(first_rank, dict) else None,
            first_rank.get("排名") if isinstance(first_rank, dict) else None,
        ])
    return _compact_text(data)


def _summarize_generic_partition(value: Any) -> Optional[str]:
    data = _parse_json_value(value)
    if isinstance(data, dict):
        row = _first_table_row(data)
        return _join_parts([
            data.get("text"),
            row.get("大类学科"),
            row.get("分区") or row.get("大类") or row.get("小类"),
            row.get("小类学科"),
            row.get("排名"),
            row.get("学科"),
        ])
    return _compact_text(data)


def _journal_to_response(
    journal: Journal,
    comment_count: int = 0,
    include_detail: bool = False,
) -> dict[str, Any]:
    parsed_detail_data = _parse_json_value(journal.detail_data)
    detail_data = parsed_detail_data if isinstance(parsed_detail_data, dict) else None
    jcr_source = journal.jcr_partition or (detail_data or {}).get("jcr_partition") or (
        detail_data or {}
    ).get("WOS期刊JCR分区_（_2024-2025年最新版_）")
    cas_source = (
        journal.cas_partition
        or (detail_data or {}).get("cas_partition")
        or (detail_data or {}).get("期刊分区表_（_2025年3月升级版_）")
        or (detail_data or {}).get("《新锐期刊分区表》_（_2026年3月发布_）")
        or (detail_data or {}).get("期刊分区表_（_2023年12月旧的升级版_）")
    )
    citescore_source = journal.citescore or (detail_data or {}).get("citescore") or (
        detail_data or {}
    ).get("CiteScore_（_2025年最新版_）")
    response = {
        "id": journal.id,
        "journal_id": journal.journal_id,
        "name": journal.name,
        "issn": journal.issn,
        "eissn": journal.eissn,
        "impact_factor": float(journal.impact_factor) if journal.impact_factor is not None else None,
        "impact_factor_realtime": float(journal.impact_factor_realtime) if journal.impact_factor_realtime is not None else None,
        "self_citation_rate": journal.self_citation_rate,
        "jcr_partition": journal.jcr_partition,
        "cas_partition": journal.cas_partition,
        "cas_warning": journal.cas_warning,
        "citescore": journal.citescore,
        "review_speed": journal.review_speed,
        "acceptance_rate": journal.acceptance_rate,
        "detail_crawled": journal.detail_crawled,
        "comments_crawled": journal.comments_crawled,
        "category_id": journal.category_id,
        "created_at": journal.created_at,
        "comment_count": comment_count,
        "jcr_partition_summary": _summarize_jcr(jcr_source),
        "cas_partition_summary": _summarize_generic_partition(cas_source),
        "citescore_summary": _summarize_citescore(citescore_source),
    }
    if include_detail:
        response["detail_data"] = detail_data
        response["page_comment_count"] = detail_data.get("comment_count") if detail_data else None
    return response

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
    search: Optional[str] = None,  # 搜索期刊ID、名称或ISSN
    detail_crawled: Optional[bool] = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db)
):
    """获取期刊列表（支持期刊ID/名称/ISSN搜索）"""
    query = db.query(Journal)

    if category_id:
        query = query.filter(Journal.category_id == category_id)

    if search:
        search_text = search.strip()
        if search_text.isdigit():
            query = query.filter(Journal.journal_id == int(search_text))
        else:
            search_pattern = f"%{search_text}%"
            query = query.filter(
                or_(
                    Journal.name.ilike(search_pattern),
                    Journal.issn.ilike(search_pattern),
                    Journal.eissn.ilike(search_pattern),
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
    journal_ids = [journal.journal_id for journal in journals]
    comment_counts = {}
    if journal_ids:
        comment_counts = dict(
            db.query(Comment.journal_id, func.count(Comment.id))
            .filter(Comment.journal_id.in_(journal_ids))
            .group_by(Comment.journal_id)
            .all()
        )

    return JournalListResponse(
        total=total,
        page=page,
        size=size,
        items=[
            _journal_to_response(journal, comment_counts.get(journal.journal_id, 0))
            for journal in journals
        ]
    )

@router.get("/journals/{journal_id}", response_model=JournalDetailResponse)
def get_journal(journal_id: int, db: Session = Depends(get_db)):
    """获取期刊详情"""
    journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
    if not journal:
        raise HTTPException(status_code=404, detail="期刊不存在")
    comment_count = db.query(Comment).filter(Comment.journal_id == journal_id).count()
    return _journal_to_response(journal, comment_count, include_detail=True)

@router.get("/journals/{journal_id}/comments", response_model=List[CommentResponse])
def get_journal_comments(
    journal_id: int,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    """获取期刊评论"""
    journal = db.query(Journal).filter(Journal.journal_id == journal_id).first()
    if not journal:
        raise HTTPException(status_code=404, detail="期刊不存在")

    offset = (page - 1) * size
    # Comment.journal_id 存储的是期刊的 journal_id，不是主键 id
    return db.query(Comment).filter(
        Comment.journal_id == journal_id
    ).order_by(Comment.crawled_at.desc()).offset(offset).limit(size).all()

@router.get("/export/journals")
def export_journals(
    category_id: Optional[int] = None,
    format: str = Query("json", pattern="^(json|csv)$"),
    include_comments: bool = Query(False, description="是否包含评论数据"),
    db: Session = Depends(get_db)
):
    """导出期刊数据（可选包含评论）
    
    评论数据以 JSON 列表形式放在期刊的一列中
    """
    import json as json_lib
    
    query = db.query(Journal)
    if category_id:
        query = query.filter(Journal.category_id == category_id)

    journals = query.all()

    if format == "csv":
        # 使用 BytesIO 并添加 UTF-8 BOM 解决乱码问题
        output = io.BytesIO()
        output.write(b'\xef\xbb\xbf')  # UTF-8 BOM
        
        text_output = io.StringIO()
        writer = csv.writer(text_output)
        
        if include_comments:
            # 导出期刊数据，评论作为 JSON 列表放在一列
            writer.writerow([
                "期刊ID", "期刊名称", "ISSN", "E-ISSN", "影响因子", 
                "JCR分区", "中科院分区", "审稿速度", "录用比例", "评论数量", "评论数据"
            ])
            for j in journals:
                comments = db.query(Comment).filter(Comment.journal_id == j.journal_id).all()
                # 将评论转为 JSON 列表
                comments_list = []
                for c in comments:
                    comments_list.append({
                        "comment_id": c.comment_id,
                        "content": c.content,
                        "author": c.author,
                        "rating": c.rating,
                        "submit_experience": c.submit_experience,
                        "comment_time": c.comment_time.strftime('%Y-%m-%d %H:%M') if c.comment_time else None
                    })
                
                writer.writerow([
                    j.journal_id, j.name, j.issn, j.eissn, j.impact_factor,
                    j.jcr_partition, j.cas_partition, j.review_speed, j.acceptance_rate,
                    len(comments_list),
                    json_lib.dumps(comments_list, ensure_ascii=False) if comments_list else ''
                ])
            filename = "journals_with_comments.csv"
        else:
            # 仅导出期刊数据
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
            filename = "journals.csv"

        # 写入 BytesIO
        output.write(text_output.getvalue().encode('utf-8'))
        output.seek(0)
        
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    return [JournalResponse.model_validate(j) for j in journals]


@router.get("/export/comments")
def export_comments(
    journal_id: Optional[int] = None,
    format: str = Query("csv", pattern="^(json|csv)$"),
    db: Session = Depends(get_db)
):
    """导出评论数据"""
    query = db.query(Comment)
    if journal_id:
        query = query.filter(Comment.journal_id == journal_id)

    comments = query.all()

    if format == "csv":
        # 使用 BytesIO 并添加 UTF-8 BOM
        output = io.BytesIO()
        output.write(b'\xef\xbb\xbf')  # UTF-8 BOM
        
        text_output = io.StringIO()
        writer = csv.writer(text_output)
        writer.writerow([
            "评论ID", "期刊ID", "评论内容", "作者", "评分", 
            "投稿经历", "评论时间", "爬取时间"
        ])
        for c in comments:
            writer.writerow([
                c.comment_id, c.journal_id, c.content, c.author, c.rating,
                c.submit_experience,
                c.comment_time.strftime('%Y-%m-%d %H:%M') if c.comment_time else '',
                c.crawled_at.strftime('%Y-%m-%d %H:%M') if c.crawled_at else ''
            ])

        output.write(text_output.getvalue().encode('utf-8'))
        output.seek(0)
        
        return StreamingResponse(
            iter([output.getvalue()]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=comments.csv"}
        )

    return [CommentResponse.model_validate(c) for c in comments]
