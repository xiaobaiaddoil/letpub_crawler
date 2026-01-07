from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from app.config import config
import json

# 配置 PostgreSQL JSONB 支持
from psycopg2.extensions import register_adapter, AsIs

def adapt_dict(d):
    """将 dict 转换为 JSON 字符串供 psycopg2 使用"""
    return AsIs(f"'{json.dumps(d, ensure_ascii=False)}'::jsonb")

register_adapter(dict, adapt_dict)

engine = create_engine(
    config.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False)
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

def get_db():
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db():
    """初始化数据库表"""
    from app.models import Category, Journal, Comment, CrawlTask, CookiePool
    Base.metadata.create_all(bind=engine)
