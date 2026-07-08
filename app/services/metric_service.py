import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.journal import Journal
from app.models.journal_metric import JournalMetricChange, JournalMetricSnapshot


METRIC_FIELDS = [
    "issn",
    "eissn",
    "impact_factor",
    "impact_factor_realtime",
    "self_citation_rate",
    "jcr_partition",
    "cas_partition",
    "cas_warning",
    "citescore",
    "review_speed",
    "acceptance_rate",
    "comment_count",
    "comment_pages",
    "crawled_comment_count",
]


def _normalize_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return str(value)
    return str(value)


def _metric_hash(metrics: dict[str, Any]) -> str:
    payload = json.dumps(metrics, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class MetricService:
    """记录期刊详情关键指标快照和变化."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def extract_metrics(journal: Journal, basic_info: dict | None = None) -> dict[str, Any]:
        basic_info = basic_info or {}
        metrics = {
            "issn": journal.issn,
            "eissn": journal.eissn,
            "impact_factor": journal.impact_factor,
            "impact_factor_realtime": journal.impact_factor_realtime,
            "self_citation_rate": journal.self_citation_rate,
            "jcr_partition": journal.jcr_partition,
            "cas_partition": journal.cas_partition,
            "cas_warning": journal.cas_warning,
            "citescore": journal.citescore,
            "review_speed": journal.review_speed,
            "acceptance_rate": journal.acceptance_rate,
            "comment_count": basic_info.get("comment_count"),
            "comment_pages": basic_info.get("comment_pages"),
            "crawled_comment_count": basic_info.get("crawled_comment_count"),
        }
        return {field: _normalize_value(metrics.get(field)) for field in METRIC_FIELDS}

    def record_snapshot(
        self,
        journal: Journal,
        basic_info: dict | None = None,
        task_id: int | None = None,
        source: str = "detail",
    ) -> int:
        metrics = self.extract_metrics(journal, basic_info)
        current_hash = _metric_hash(metrics)

        previous = self.db.query(JournalMetricSnapshot).filter(
            JournalMetricSnapshot.journal_id == journal.journal_id,
            JournalMetricSnapshot.source == source,
        ).order_by(JournalMetricSnapshot.crawled_at.desc()).first()

        if previous and previous.metric_hash == current_hash:
            return 0

        changed_count = 0
        previous_metrics = previous.metrics if previous and previous.metrics else {}
        if previous:
            for field in METRIC_FIELDS:
                old_value = _normalize_value(previous_metrics.get(field))
                new_value = _normalize_value(metrics.get(field))
                if old_value != new_value:
                    self.db.add(JournalMetricChange(
                        journal_id=journal.journal_id,
                        task_id=task_id,
                        source=source,
                        field_name=field,
                        old_value=old_value,
                        new_value=new_value,
                        changed_at=datetime.utcnow(),
                    ))
                    changed_count += 1

        self.db.add(JournalMetricSnapshot(
            journal_id=journal.journal_id,
            task_id=task_id,
            source=source,
            metrics=metrics,
            metric_hash=current_hash,
            crawled_at=datetime.utcnow(),
        ))
        return changed_count
