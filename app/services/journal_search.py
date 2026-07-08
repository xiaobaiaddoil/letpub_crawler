import json
from typing import Optional

from sqlalchemy import Text, and_, cast, func, literal, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, Session

from app.models.journal import Journal


_PG_TRGM_AVAILABLE: dict[str, bool] = {}

SORTABLE_FIELDS = {
    "impact_factor": Journal.impact_factor,
    "impact_factor_realtime": Journal.impact_factor_realtime,
    "name": Journal.name,
    "journal_id": Journal.journal_id,
    "created_at": Journal.created_at,
    "updated_at": Journal.updated_at,
}

def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(column, value: str):
    return column.ilike(f"%{_escape_like(value)}%", escape="\\")


def _json_text_contains(value: str):
    return _contains(cast(Journal.detail_data, Text), value)


def _dialect_name(db: Session) -> str:
    try:
        bind = db.get_bind()
    except Exception:
        return ""
    return getattr(getattr(bind, "dialect", None), "name", "") or ""


def _bind_key(db: Session) -> str:
    try:
        bind = db.get_bind()
        url = getattr(bind, "url", None)
        if url is not None:
            return url.render_as_string(hide_password=True)
    except Exception:
        return ""
    return ""


def _pg_trgm_available(db: Session) -> bool:
    if _dialect_name(db) != "postgresql":
        return False

    key = _bind_key(db)
    if key in _PG_TRGM_AVAILABLE:
        return _PG_TRGM_AVAILABLE[key]

    try:
        available = bool(
            db.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm')")
            ).scalar()
        )
    except SQLAlchemyError:
        db.rollback()
        available = False

    _PG_TRGM_AVAILABLE[key] = available
    return available


def _typo_fuzzy_match(db: Session, column, value: str, threshold: float):
    if not value or not _pg_trgm_available(db):
        return None
    return func.similarity(func.lower(cast(column, Text)), func.lower(literal(value))) >= threshold


def _name_match(db: Session, value: str, typo_fuzzy: bool, fuzzy_threshold: float):
    predicates = [_contains(Journal.name, value)]
    if typo_fuzzy:
        typo_match = _typo_fuzzy_match(db, Journal.name, value, fuzzy_threshold)
        if typo_match is not None:
            predicates.append(typo_match)
    return or_(*predicates)


def _postgres_json_text(field: str):
    indexed = Journal.detail_data[field]
    as_text = getattr(indexed, "astext", None)
    if as_text is not None:
        return cast(as_text, Text)
    if hasattr(indexed, "as_string"):
        return cast(indexed.as_string(), Text)
    return cast(indexed, Text)


def _detail_field_filter(
    db: Session,
    detail_field: str,
    detail_value: Optional[str],
    detail_op: str,
):
    dialect = _dialect_name(db)
    if dialect == "postgresql":
        if detail_op == "exists":
            return Journal.detail_data.has_key(detail_field)  # noqa: W601

        field_text = _postgres_json_text(detail_field)
        if detail_op == "equals":
            return field_text == (detail_value or "")
        return _contains(field_text, detail_value or "")

    key_json = json.dumps(detail_field, ensure_ascii=False)
    predicates = [or_(_json_text_contains(key_json), _json_text_contains(detail_field))]
    if detail_op != "exists":
        predicates.append(_json_text_contains(detail_value or ""))
    return and_(*predicates)


def apply_journal_filters(
    query: Query,
    db: Session,
    *,
    q: Optional[str] = None,
    name: Optional[str] = None,
    issn: Optional[str] = None,
    category_id: Optional[int] = None,
    detail_crawled: Optional[bool] = None,
    comments_crawled: Optional[bool] = None,
    impact_factor_min: Optional[float] = None,
    impact_factor_max: Optional[float] = None,
    impact_factor_realtime_min: Optional[float] = None,
    impact_factor_realtime_max: Optional[float] = None,
    jcr_partition: Optional[str] = None,
    cas_partition: Optional[str] = None,
    cas_warning: Optional[str] = None,
    citescore: Optional[str] = None,
    review_speed: Optional[str] = None,
    acceptance_rate: Optional[str] = None,
    self_citation_rate: Optional[str] = None,
    detail_text: Optional[str] = None,
    detail_field: Optional[str] = None,
    detail_value: Optional[str] = None,
    detail_op: str = "contains",
    typo_fuzzy: bool = False,
    fuzzy_threshold: float = 0.3,
) -> Query:
    q = _clean(q)
    name = _clean(name)
    issn = _clean(issn)
    jcr_partition = _clean(jcr_partition)
    cas_partition = _clean(cas_partition)
    cas_warning = _clean(cas_warning)
    citescore = _clean(citescore)
    review_speed = _clean(review_speed)
    acceptance_rate = _clean(acceptance_rate)
    self_citation_rate = _clean(self_citation_rate)
    detail_text = _clean(detail_text)
    detail_field = _clean(detail_field)
    detail_value = _clean(detail_value)
    detail_op = detail_op if detail_op in {"contains", "equals", "exists"} else "contains"
    fuzzy_threshold = min(max(fuzzy_threshold, 0.1), 1.0)

    if category_id is not None:
        query = query.filter(Journal.category_id == category_id)

    if detail_crawled is not None:
        query = query.filter(Journal.detail_crawled == detail_crawled)

    if comments_crawled is not None:
        query = query.filter(Journal.comments_crawled == comments_crawled)

    if q:
        predicates = [
            _name_match(db, q, typo_fuzzy, fuzzy_threshold),
            _contains(Journal.issn, q),
            _contains(Journal.eissn, q),
        ]
        if q.isdigit():
            predicates.append(Journal.journal_id == int(q))
        query = query.filter(or_(*predicates))

    if name:
        query = query.filter(_name_match(db, name, typo_fuzzy, fuzzy_threshold))

    if issn:
        query = query.filter(or_(_contains(Journal.issn, issn), _contains(Journal.eissn, issn)))

    if impact_factor_min is not None:
        query = query.filter(Journal.impact_factor >= impact_factor_min)
    if impact_factor_max is not None:
        query = query.filter(Journal.impact_factor <= impact_factor_max)
    if impact_factor_realtime_min is not None:
        query = query.filter(Journal.impact_factor_realtime >= impact_factor_realtime_min)
    if impact_factor_realtime_max is not None:
        query = query.filter(Journal.impact_factor_realtime <= impact_factor_realtime_max)

    text_filters = [
        (Journal.jcr_partition, jcr_partition),
        (Journal.cas_partition, cas_partition),
        (Journal.cas_warning, cas_warning),
        (Journal.citescore, citescore),
        (Journal.review_speed, review_speed),
        (Journal.acceptance_rate, acceptance_rate),
        (Journal.self_citation_rate, self_citation_rate),
    ]
    for column, value in text_filters:
        if value:
            query = query.filter(_contains(column, value))

    if detail_text:
        query = query.filter(_json_text_contains(detail_text))

    if detail_field:
        query = query.filter(_detail_field_filter(db, detail_field, detail_value, detail_op))

    return query


def apply_journal_sort(query: Query, sort: str, order: str) -> Query:
    column = SORTABLE_FIELDS.get(sort, Journal.impact_factor)
    direction = column.asc() if order == "asc" else column.desc()
    return query.order_by(direction.nullslast(), Journal.journal_id.asc())
