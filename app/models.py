"""ORM 模型：会所、线索、规则集、报价单、单号计数器。

业务表统一含 id(UUID)、org_id、site_id、created_at、created_by、
updated_at、deleted_at（软删除掩码）。金额一律以「分」存整数。
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

JSONType = JSON().with_variant(postgresql.JSONB, "postgresql")


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class TimestampMixin:
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )


class Site(TimestampMixin, Base):
    """会所（site_code 用于报价单号）。"""

    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_sites_org_code"),)

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Lead(TimestampMixin, Base):
    """线索。stage 取值见 funnel.STAGES。"""

    __tablename__ = "leads"
    __table_args__ = (Index("ix_leads_site_stage", "site_id", "stage"),)

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    contact_phone: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)


class RuleSet(TimestampMixin, Base):
    """会所报价规则集（版本化，生效区间）。"""

    __tablename__ = "rule_sets"
    __table_args__ = (
        UniqueConstraint("site_id", "version", name="uq_rule_sets_site_version"),
        Index("ix_rule_sets_site_effective", "site_id", "effective_from"),
    )

    version: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    rules: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)


class SalesQuote(TimestampMixin, Base):
    """报价单。金额列全部由计算引擎写入，不可由请求体指定。"""

    __tablename__ = "sales_quotes"
    __table_args__ = (
        UniqueConstraint("org_id", "quote_no", "revision", name="uq_sales_quotes_no_rev"),
        Index("ix_sales_quotes_lead_status", "lead_id", "status"),
        Index("ix_sales_quotes_status_valid_until", "status", "valid_until"),
        CheckConstraint(
            "status IN ('draft','pending_approval','approved','rejected',"
            "'sent','accepted','expired','superseded','void')",
            name="ck_sales_quotes_status",
        ),
        CheckConstraint("revision >= 1", name="ck_sales_quotes_revision"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("leads.id"), nullable=False
    )
    quote_no: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    rule_set_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rule_sets.id"), nullable=False
    )
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    calc_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)

    # 冗余列（计算引擎输出，便于列表/报表直接查询）
    final_monthly_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    contract_total_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    first_year_amount_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deposit_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    concession_bps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    below_floor: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    required_tier: Mapped[str] = mapped_column(String(16), nullable=False, default="T1")

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # 审批 / 作废留痕
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    void_reason: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    voided_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    superseded_by_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )


class QuoteNoCounter(Base):
    """按 (site_code, yyyymmdd) 的日序号计数器，保证单号 3 位序号递增。"""

    __tablename__ = "quote_no_counters"
    __table_args__ = (UniqueConstraint("site_code", "day_key", name="uq_quote_no_counters"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=_uuid)
    site_code: Mapped[str] = mapped_column(String(16), nullable=False)
    day_key: Mapped[str] = mapped_column(String(8), nullable=False)
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


__all__ = [
    "Site",
    "Lead",
    "RuleSet",
    "SalesQuote",
    "QuoteNoCounter",
]

# 供类型检查/文档引用
_ = (date, ForeignKey)
