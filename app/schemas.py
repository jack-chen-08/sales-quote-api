"""请求 / 响应模型。

请求模型一律 ``extra="allow"``：金额只读校验必须在入口处看到**原始**字段，
不能靠 pydantic 静默丢弃未知字段（否则 final_monthly_cents 会被无声忽略）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

QuoteStatus = Literal[
    "draft",
    "pending_approval",
    "approved",
    "rejected",
    "sent",
    "accepted",
    "expired",
    "superseded",
    "void",
]


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class PreviewRequest(_Loose):
    lead_id: Optional[uuid.UUID] = None
    rule_set_id: Optional[uuid.UUID] = None
    input_snapshot: dict[str, Any] = Field(..., description="输入快照，不含任何金额字段")
    created_by: Optional[str] = None


class QuoteCreateRequest(PreviewRequest):
    """建草稿：input_snapshot 必填，rule_set_id 可覆盖（需属于同会所）。"""


class QuoteUpdateRequest(_Loose):
    input_snapshot: dict[str, Any] = Field(..., description="整份替换输入快照并重算")
    updated_by: Optional[str] = None


class DecisionRequest(_Loose):
    """审批结论（补充接口：审批系统对接前的最小实现）。"""

    decision: Literal["approved", "rejected", "pending_approval"]
    reason: Optional[str] = Field(default=None, max_length=512)


class MarkSentRequest(_Loose):
    channel: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=512)


class VoidRequest(_Loose):
    reason: str = Field(..., max_length=512)


class QuoteOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    org_id: uuid.UUID
    site_id: uuid.UUID
    lead_id: uuid.UUID
    quote_no: str
    revision: int
    rule_set_id: uuid.UUID

    input_snapshot: dict[str, Any]
    calc_snapshot: dict[str, Any]

    final_monthly_cents: int
    contract_total_cents: int
    first_year_amount_cents: int
    deposit_cents: int
    concession_bps: int
    below_floor: bool
    required_tier: str

    status: str
    valid_until: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    void_reason: Optional[str] = None
    voided_at: Optional[datetime] = None
    superseded_by_id: Optional[uuid.UUID] = None

    created_at: datetime
    created_by: str
    updated_at: datetime
    deleted_at: Optional[datetime] = None


class QuoteListOut(BaseModel):
    items: list[QuoteOut]
    total: int
    page: int
    page_size: int
    pages: int


class PreviewOut(BaseModel):
    calc_snapshot: dict[str, Any]
    final_monthly_cents: int
    contract_total_cents: int
    first_year_amount_cents: int
    deposit_cents: int
    concession_bps: int
    below_floor: bool
    required_tier: str
    rule_set_id: uuid.UUID
    rule_set_version: str
