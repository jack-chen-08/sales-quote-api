"""数据库引擎 / 会话 / Base。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import settings


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


_engine: Optional[Engine] = None
_SessionLocal: Optional[sessionmaker[Session]] = None


def _engine_options(url: str) -> dict:
    if url.startswith("sqlite"):
        # 内存库需要 StaticPool 才能在多连接间共享
        from sqlalchemy.pool import StaticPool

        return {
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        }
    return {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 5}


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        url = settings.database_url
        _engine = create_engine(url, future=True, **_engine_options(url))
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def session_scope() -> Session:
    return get_sessionmaker()()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：每请求一个会话。"""
    db = session_scope()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def create_all(engine: Optional[Engine] = None) -> None:
    from . import models  # noqa: F401  确保模型已注册

    Base.metadata.create_all(bind=engine or get_engine())
