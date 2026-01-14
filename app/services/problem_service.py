"""问题任务服务 - 记录和管理出现问题的任务"""
import logging
from datetime import datetime, timezone
from typing import List, Optional, Dict
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.problem_task import ProblemTask, ProblemType

logger = logging.getLogger(__name__)


class ProblemService:
    """问题任务服务"""
    
    # 问题代码定义
    CODE_COMMENT_MISMATCH = "comment_mismatch"  # 评论数量不匹配
    CODE_COMMENT_LOW = "comment_low"  # 评论数量过少
    CODE_CRAWL_FAILED = "crawl_failed"  # 爬取失败
    CODE_PARSE_ERROR = "parse_error"  # 解析错误
    CODE_COOKIE_INVALID = "cookie_invalid"  # Cookie失效
    
    def __init__(self, db: Session):
        self.db = db
    
    def record_problem(
        self,
        journal_id: int,
        problem_type: str,
        problem_code: str,
        message: str = None,
        expected_value: int = None,
        actual_value: int = None
    ) -> ProblemTask:
        """记录问题任务
        
        Args:
            journal_id: 期刊ID
            problem_type: 问题类型 (warning/error)
            problem_code: 问题代码
            message: 问题描述
            expected_value: 期望值
            actual_value: 实际值
        """
        # 检查是否已存在相同问题（未解决的）
        existing = self.db.query(ProblemTask).filter(
            ProblemTask.journal_id == journal_id,
            ProblemTask.problem_code == problem_code,
            ProblemTask.resolved == 0
        ).first()
        
        if existing:
            # 更新现有记录
            existing.message = message
            existing.expected_value = expected_value
            existing.actual_value = actual_value
            existing.updated_at = datetime.now(timezone.utc)
            self.db.commit()
            return existing
        
        # 创建新记录
        problem = ProblemTask(
            journal_id=journal_id,
            problem_type=problem_type,
            problem_code=problem_code,
            message=message,
            expected_value=expected_value,
            actual_value=actual_value
        )
        self.db.add(problem)
        self.db.commit()
        
        logger.info(f"[问题记录] journal_id={journal_id}, type={problem_type}, code={problem_code}")
        return problem
    
    def record_warning(
        self,
        journal_id: int,
        problem_code: str,
        message: str = None,
        expected_value: int = None,
        actual_value: int = None
    ) -> ProblemTask:
        """记录警告"""
        return self.record_problem(
            journal_id=journal_id,
            problem_type=ProblemType.WARNING.value,
            problem_code=problem_code,
            message=message,
            expected_value=expected_value,
            actual_value=actual_value
        )
    
    def record_error(
        self,
        journal_id: int,
        problem_code: str,
        message: str = None
    ) -> ProblemTask:
        """记录错误"""
        return self.record_problem(
            journal_id=journal_id,
            problem_type=ProblemType.ERROR.value,
            problem_code=problem_code,
            message=message
        )
    
    def mark_resolved(self, problem_id: int) -> bool:
        """标记问题已解决"""
        problem = self.db.query(ProblemTask).filter(ProblemTask.id == problem_id).first()
        if problem:
            problem.resolved = 1
            problem.resolved_at = datetime.now(timezone.utc)
            self.db.commit()
            return True
        return False
    
    def mark_resolved_by_journal(self, journal_id: int, problem_code: str = None) -> int:
        """标记期刊的问题已解决"""
        query = self.db.query(ProblemTask).filter(
            ProblemTask.journal_id == journal_id,
            ProblemTask.resolved == 0
        )
        if problem_code:
            query = query.filter(ProblemTask.problem_code == problem_code)
        
        count = query.update({
            ProblemTask.resolved: 1,
            ProblemTask.resolved_at: datetime.now(timezone.utc)
        })
        self.db.commit()
        return count
    
    def get_unresolved(
        self,
        problem_type: str = None,
        problem_code: str = None,
        page: int = 1,
        size: int = 50
    ) -> Dict:
        """获取未解决的问题"""
        query = self.db.query(ProblemTask).filter(ProblemTask.resolved == 0)
        
        if problem_type:
            query = query.filter(ProblemTask.problem_type == problem_type)
        if problem_code:
            query = query.filter(ProblemTask.problem_code == problem_code)
        
        total = query.count()
        offset = (page - 1) * size
        items = query.order_by(ProblemTask.created_at.desc()).offset(offset).limit(size).all()
        
        return {
            "total": total,
            "page": page,
            "size": size,
            "items": items
        }
    
    def get_unresolved_journal_ids(self, problem_code: str = None) -> List[int]:
        """获取未解决问题的期刊ID列表"""
        query = self.db.query(ProblemTask.journal_id).filter(ProblemTask.resolved == 0)
        if problem_code:
            query = query.filter(ProblemTask.problem_code == problem_code)
        
        return [r[0] for r in query.distinct().all()]
    
    def get_stats(self) -> Dict:
        """获取问题统计"""
        total = self.db.query(ProblemTask).count()
        unresolved = self.db.query(ProblemTask).filter(ProblemTask.resolved == 0).count()
        
        # 按类型统计
        by_type = {}
        for ptype in [ProblemType.WARNING.value, ProblemType.ERROR.value]:
            count = self.db.query(ProblemTask).filter(
                ProblemTask.problem_type == ptype,
                ProblemTask.resolved == 0
            ).count()
            by_type[ptype] = count
        
        # 按代码统计
        by_code = {}
        code_counts = self.db.query(
            ProblemTask.problem_code,
            func.count(ProblemTask.id)
        ).filter(
            ProblemTask.resolved == 0
        ).group_by(ProblemTask.problem_code).all()
        
        for code, count in code_counts:
            by_code[code] = count
        
        return {
            "total": total,
            "unresolved": unresolved,
            "resolved": total - unresolved,
            "by_type": by_type,
            "by_code": by_code
        }
    
    def delete_resolved(self) -> int:
        """删除已解决的问题记录"""
        count = self.db.query(ProblemTask).filter(ProblemTask.resolved == 1).delete()
        self.db.commit()
        return count
