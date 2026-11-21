"""pytest 公共夹具。

- 默认用 SQLite 内存库，便于快速复跑；
- 设置环境变量 ``TEST_DATABASE_URL`` 即切换到真实 PostgreSQL 16（同一套测试）。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import clock, funnel, pricing_engine
from app.db import Base, get_db
from app.main import create_app
from app.models import Lead, RuleSet, Site

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "").strip()
SQLITE_URL = "sqlite+pysqlite://"

T0 = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def db_engine():
    url = TEST_DATABASE_URL or SQLITE_URL
    if url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        engine = create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    else:
        engine = create_engine(url, future=True, pool_pre_ping=True)

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db(db_engine):
    """每例清空数据，返回一个长生命周期会话（供 API 与断言共用）。"""
    with db_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    Session = sessionmaker(bind=db_engine, expire_on_commit=False, future=True)
    session = Session()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client(db):
    app = create_app()

    def _override_get_db():
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def frozen_time():
    """冻结时钟在 2026-03-01 09:00 UTC。"""
    with clock.freeze(T0) as box:
        yield box


@pytest.fixture
def seeded(db, frozen_time):
    """一个组织 + 一个会所 + 一条 visit_scheduled 线索 + 一个生效规则集。"""
    org_id = uuid.uuid4()
    site_id = uuid.uuid4()

    site = Site(
        id=site_id,
        org_id=org_id,
        site_id=site_id,
        code="SH01",
        name="会员空间·测试会所",
        created_at=T0 - timedelta(days=30),
        created_by="seed",
        updated_at=T0 - timedelta(days=30),
    )
    lead = Lead(
        org_id=org_id,
        site_id=site_id,
        name="测试线索A",
        stage="visit_scheduled",
        created_at=T0 - timedelta(days=3),
        created_by="seed",
        updated_at=T0 - timedelta(days=3),
    )
    rules = dict(pricing_engine.DEFAULT_RULES)
    rules["version"] = "v2026.03"
    rule_set = RuleSet(
        org_id=org_id,
        site_id=site_id,
        version="v2026.03",
        effective_from=T0 - timedelta(days=10),
        effective_to=None,
        is_active=True,
        rules=rules,
        created_at=T0 - timedelta(days=10),
        created_by="seed",
        updated_at=T0 - timedelta(days=10),
    )
    db.add_all([site, lead, rule_set])
    db.commit()

    return SimpleNamespace(
        org_id=org_id,
        site_id=site_id,
        site=site,
        lead=lead,
        rule_set=rule_set,
        rules=rules,
        input_snapshot={
            "plan_code": "STD",
            "seat_count": 10,
            "contract_months": 24,
            "addon_codes": ["spa_pack"],
            "start_date": "2026-04-01",
        },
    )


@pytest.fixture(autouse=True)
def _reset_funnel_calls():
    funnel.reset_calls()
    yield
    funnel.reset_calls()


@pytest.fixture
def funnel_spy(monkeypatch):
    """包装真实 advance，记录调用参数（同时不改变行为）。"""
    real_advance = funnel.advance
    calls: list[dict] = []

    def spy(session, lead_id, to_status, trigger="system", **kwargs):
        calls.append(
            {
                "session": session,
                "lead_id": lead_id,
                "to_status": to_status,
                "trigger": trigger,
                **kwargs,
            }
        )
        return real_advance(session, lead_id, to_status, trigger, **kwargs)

    monkeypatch.setattr(funnel, "advance", spy)
    return calls
