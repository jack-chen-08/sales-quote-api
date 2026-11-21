"""报价单 API。

路由前缀 ``/api/sales``。

硬规则落实点
------------
1. **金额只读**：任何请求体（含嵌套）出现金额/比例结果字段 → 422 QUOTE_AMOUNT_READONLY，
   在 ``guard_amounts`` 里对**原始 JSON** 递归扫描，早于 pydantic 校验执行，
   因此员工 AI 助手等任何调用方都无法绕过。
2. **金额只由计算引擎产生**：所有金额列都来自 ``pricing_engine.evaluate().amounts``。
3. **保存重算**：建草稿 / 修改草稿 / revise 都重新调用引擎，禁止回填历史金额。
4. **漏斗推进**：仅 visit_scheduled 且首张草稿 → advance(quoting, trigger=system)。
5. **有效期**：approve 时写 valid_until = approved_at + 15 天。
"""

from __future__ import annotations

import json
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import clock, funnel, pricing_engine
from .db import get_db
from .errors import (
    INVALID_INPUT_SNAPSHOT,
    LEAD_NOT_FOUND,
    NO_ACTIVE_RULE_SET,
    QUOTE_NOT_EDITABLE,
    QUOTE_NOT_FOUND,
    QUOTE_NOT_REVISABLE,
    QUOTE_NOT_SENDABLE,
    QUOTE_STATUS_INVALID,
    QUOTE_VOID_REASON_REQUIRED,
    RULE_SET_NOT_FOUND,
    ApiError,
    amount_readonly,
)
from .models import Lead, RuleSet, SalesQuote, Site
from .quote_no import next_quote_no
from .schemas import (
    DecisionRequest,
    MarkSentRequest,
    PreviewOut,
    PreviewRequest,
    QuoteCreateRequest,
    QuoteListOut,
    QuoteOut,
    QuoteUpdateRequest,
    VoidRequest,
)
from .settings import get_settings

router = APIRouter(prefix="/api/sales", tags=["sales-quotes"])

EDITABLE_STATUSES = {"draft", "rejected"}
REVISABLE_STATUSES = {"approved", "sent"}
FINAL_STATUSES = {"void", "superseded", "expired"}
TERMINAL_FOR_VOID = {"void"}

# 精确名单 + 命名规则双重拦截
READONLY_EXACT = {
    "calc_snapshot",
    "final_monthly_cents",
    "contract_total_cents",
    "first_year_amount_cents",
    "deposit_cents",
    "concession_bps",
    "below_floor",
    "required_tier",
    "discount_cents",
    "monthly_base_cents",
    "monthly_addon_cents",
    "monthly_gross_cents",
    "contract_total_gross_cents",
}
READONLY_SUFFIXES = ("_cents", "_bps")


def find_readonly_field(node: Any, path: str = "") -> Optional[tuple[str, str]]:
    """递归查找请求体中的金额/比例结果字段，返回 (字段名, JSON 路径)。"""
    if isinstance(node, dict):
        for key, value in node.items():
            key_str = str(key)
            here = f"{path}.{key_str}" if path else key_str
            if key_str in READONLY_EXACT or key_str.endswith(READONLY_SUFFIXES):
                return key_str, here
            found = find_readonly_field(value, here)
            if found:
                return found
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            found = find_readonly_field(item, f"{path}[{idx}]")
            if found:
                return found
    return None


async def guard_amounts(request: Request) -> None:
    """入口拦截：请求体出现金额/比例结果字段直接 422。"""
    raw = await request.body()
    if not raw:
        return
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise ApiError(400, "INVALID_JSON", "请求体不是合法 JSON")
    hit = find_readonly_field(payload)
    if hit:
        field_name, path = hit
        raise amount_readonly(field_name, path)


# 路由级依赖：先于 body 校验执行，保证错误码优先
router.dependencies.append(Depends(guard_amounts))


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _get_quote(db: Session, qid: uuid.UUID) -> SalesQuote:
    quote = db.get(SalesQuote, qid)
    if quote is None or quote.deleted_at is not None:
        raise ApiError(404, QUOTE_NOT_FOUND, f"报价单不存在: {qid}")
    return quote


def _get_lead(db: Session, lead_id: uuid.UUID) -> Lead:
    lead = db.get(Lead, lead_id)
    if lead is None or lead.deleted_at is not None:
        raise ApiError(404, LEAD_NOT_FOUND, f"线索不存在: {lead_id}")
    return lead


def _get_rule_set(db: Session, rule_set_id: uuid.UUID) -> RuleSet:
    rs = db.get(RuleSet, rule_set_id)
    if rs is None or rs.deleted_at is not None:
        raise ApiError(404, RULE_SET_NOT_FOUND, f"规则集不存在: {rule_set_id}")
    return rs


def resolve_active_rule_set(db: Session, site_id: uuid.UUID, at: datetime) -> RuleSet:
    """读取会所当前生效的规则集（is_active + 生效区间覆盖 now）。"""
    stmt = (
        select(RuleSet)
        .where(
            RuleSet.site_id == site_id,
            RuleSet.deleted_at.is_(None),
            RuleSet.is_active.is_(True),
            RuleSet.effective_from <= at,
            or_(RuleSet.effective_to.is_(None), RuleSet.effective_to > at),
        )
        .order_by(RuleSet.effective_from.desc(), RuleSet.created_at.desc())
        .limit(1)
    )
    rule_set = db.execute(stmt).scalar_one_or_none()
    if rule_set is None:
        raise ApiError(409, NO_ACTIVE_RULE_SET, f"会所 {site_id} 无当前生效规则集")
    return rule_set


def _site_code(db: Session, site_id: uuid.UUID) -> str:
    site = db.get(Site, site_id)
    if site is None:
        raise ApiError(404, "SITE_NOT_FOUND", f"会所不存在: {site_id}")
    return site.code


def _evaluate(rule_set: RuleSet, input_snapshot: dict[str, Any]) -> pricing_engine.CalcResult:
    rules = dict(rule_set.rules or {})
    rules.setdefault("version", rule_set.version)
    try:
        return pricing_engine.evaluate(rules, input_snapshot)
    except pricing_engine.PricingInputError as exc:
        raise ApiError(422, INVALID_INPUT_SNAPSHOT, exc.message, field=exc.field)


def _apply_calc(quote: SalesQuote, result: pricing_engine.CalcResult) -> None:
    quote.calc_snapshot = result.calc_snapshot
    quote.final_monthly_cents = result.amounts["final_monthly_cents"]
    quote.contract_total_cents = result.amounts["contract_total_cents"]
    quote.first_year_amount_cents = result.amounts["first_year_amount_cents"]
    quote.deposit_cents = result.amounts["deposit_cents"]
    quote.concession_bps = result.amounts["concession_bps"]
    quote.below_floor = result.amounts["below_floor"]
    quote.required_tier = result.amounts["required_tier"]


def _actor(explicit: Optional[str]) -> str:
    return (explicit or get_settings().default_actor or "system")[:64]


def _first_draft_count(db: Session, lead_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count(SalesQuote.id)).where(
                SalesQuote.lead_id == lead_id, SalesQuote.deleted_at.is_(None)
            )
        ).scalar_one()
    )


def _maybe_advance_funnel(db: Session, lead: Lead, quote_count: int) -> Optional[dict[str, Any]]:
    """仅 visit_scheduled + 首张草稿 → quoting(trigger=system)，更早阶段不推进。"""
    if lead.stage != "visit_scheduled" or quote_count != 1:
        return None
    result = funnel.advance(db, lead.id, "quoting", trigger="system")
    return {
        "advanced": result.advanced,
        "from_status": result.from_status,
        "to_status": result.to_status,
        "trigger": result.trigger,
    }


# --------------------------------------------------------------------------- #
# 1. 试算（不落库）
# --------------------------------------------------------------------------- #
@router.post("/quotes/preview", response_model=PreviewOut, summary="试算（只算不保存）")
def preview_quote(body: PreviewRequest, db: Session = Depends(get_db)) -> PreviewOut:
    now = clock.now()
    if body.rule_set_id is not None:
        rule_set = _get_rule_set(db, body.rule_set_id)
    elif body.lead_id is not None:
        lead = _get_lead(db, body.lead_id)
        rule_set = resolve_active_rule_set(db, lead.site_id, now)
    else:
        raise ApiError(422, "RULE_SET_REQUIRED", "rule_set_id 与 lead_id 至少提供一个")

    result = _evaluate(rule_set, body.input_snapshot)
    return PreviewOut(
        calc_snapshot=result.calc_snapshot,
        rule_set_id=rule_set.id,
        rule_set_version=rule_set.version,
        **result.amounts,
    )


# --------------------------------------------------------------------------- #
# 2. 建草稿
# --------------------------------------------------------------------------- #
@router.post(
    "/leads/{lead_id}/quotes",
    response_model=QuoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="建报价草稿（读取会所当前生效规则集）",
)
def create_quote(
    lead_id: uuid.UUID,
    body: QuoteCreateRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> SalesQuote:
    lead = _get_lead(db, lead_id)
    now = clock.now()

    if body.rule_set_id is not None:
        rule_set = _get_rule_set(db, body.rule_set_id)
        if rule_set.site_id != lead.site_id:
            raise ApiError(422, RULE_SET_NOT_FOUND, "规则集与会所不匹配", field="rule_set_id")
    else:
        rule_set = resolve_active_rule_set(db, lead.site_id, now)

    result = _evaluate(rule_set, body.input_snapshot)
    site_code = _site_code(db, lead.site_id)

    quote = SalesQuote(
        org_id=lead.org_id,
        site_id=lead.site_id,
        lead_id=lead.id,
        quote_no=next_quote_no(db, site_code, now),
        revision=1,
        rule_set_id=rule_set.id,
        input_snapshot=body.input_snapshot,
        status="draft",
        created_at=now,
        updated_at=now,
        created_by=_actor(body.created_by),
    )
    _apply_calc(quote, result)
    db.add(quote)
    db.flush()

    funnel_info = _maybe_advance_funnel(db, lead, _first_draft_count(db, lead.id))
    if funnel_info:
        response.headers["X-Funnel-Advance"] = json.dumps(funnel_info, ensure_ascii=False)

    db.commit()
    db.refresh(quote)
    return quote


# --------------------------------------------------------------------------- #
# 3. 修改草稿（draft / rejected）
# --------------------------------------------------------------------------- #
@router.put("/quotes/{qid}", response_model=QuoteOut, summary="修改草稿并重算")
def update_quote(
    qid: uuid.UUID, body: QuoteUpdateRequest, db: Session = Depends(get_db)
) -> SalesQuote:
    quote = _get_quote(db, qid)
    if quote.status not in EDITABLE_STATUSES:
        raise ApiError(
            409,
            QUOTE_NOT_EDITABLE,
            f"仅 draft / rejected 可修改，当前状态 {quote.status}",
            extra={"status": quote.status},
        )

    rule_set = _get_rule_set(db, quote.rule_set_id)
    result = _evaluate(rule_set, body.input_snapshot)
    now = clock.now()

    quote.input_snapshot = body.input_snapshot
    _apply_calc(quote, result)
    if quote.status == "rejected":
        # 修改后重新回到草稿，需重新审批
        quote.status = "draft"
        quote.rejected_at = None
        quote.decision_reason = None
    quote.updated_at = now
    if body.updated_by:
        quote.created_by = _actor(body.updated_by)

    db.commit()
    db.refresh(quote)
    return quote


# --------------------------------------------------------------------------- #
# 4. revise：从 approved / sent 生成新 revision
# --------------------------------------------------------------------------- #
@router.post(
    "/quotes/{qid}/revise",
    response_model=QuoteOut,
    status_code=status.HTTP_201_CREATED,
    summary="改版（旧版 superseded，revision+1）",
)
def revise_quote(qid: uuid.UUID, db: Session = Depends(get_db)) -> SalesQuote:
    old = _get_quote(db, qid)
    if old.status not in REVISABLE_STATUSES:
        raise ApiError(
            409,
            QUOTE_NOT_REVISABLE,
            f"仅 approved / sent 可改版，当前状态 {old.status}",
            extra={"status": old.status},
        )

    now = clock.now()
    next_revision = int(
        db.execute(
            select(func.max(SalesQuote.revision)).where(
                SalesQuote.org_id == old.org_id,
                SalesQuote.quote_no == old.quote_no,
                SalesQuote.deleted_at.is_(None),
            )
        ).scalar_one()
        or old.revision
    ) + 1

    new_quote = SalesQuote(
        org_id=old.org_id,
        site_id=old.site_id,
        lead_id=old.lead_id,
        quote_no=old.quote_no,  # 同一报价族保留单号，revision 区分版本
        revision=next_revision,
        rule_set_id=old.rule_set_id,
        input_snapshot=dict(old.input_snapshot or {}),
        status="draft",
        created_at=now,
        updated_at=now,
        created_by=old.created_by,
    )
    rule_set = _get_rule_set(db, old.rule_set_id)
    _apply_calc(new_quote, _evaluate(rule_set, new_quote.input_snapshot))

    db.add(new_quote)
    db.flush()

    old.status = "superseded"
    old.superseded_by_id = new_quote.id
    old.updated_at = now

    db.commit()
    db.refresh(new_quote)
    return new_quote


# --------------------------------------------------------------------------- #
# 5. 发送
# --------------------------------------------------------------------------- #
@router.post("/quotes/{qid}/mark-sent", response_model=QuoteOut, summary="标记已发送")
def mark_sent(
    qid: uuid.UUID, body: Optional[MarkSentRequest] = None, db: Session = Depends(get_db)
) -> SalesQuote:
    quote = _get_quote(db, qid)
    if quote.status not in {"draft", "approved", "pending_approval"}:
        raise ApiError(
            409,
            QUOTE_NOT_SENDABLE,
            f"当前状态 {quote.status} 不允许发送",
            extra={"status": quote.status},
        )

    now = clock.now()
    quote.status = "sent"
    quote.sent_at = now
    if quote.valid_until is None:
        quote.valid_until = now + timedelta(days=get_settings().quote_valid_days)
    quote.updated_at = now

    db.commit()
    db.refresh(quote)
    return quote


# --------------------------------------------------------------------------- #
# 6. 作废（原因必填）
# --------------------------------------------------------------------------- #
@router.post("/quotes/{qid}/void", response_model=QuoteOut, summary="作废（原因必填）")
def void_quote(qid: uuid.UUID, body: VoidRequest, db: Session = Depends(get_db)) -> SalesQuote:
    quote = _get_quote(db, qid)
    if quote.status in TERMINAL_FOR_VOID:
        raise ApiError(409, QUOTE_STATUS_INVALID, "报价单已作废", extra={"status": quote.status})

    reason = (body.reason or "").strip()
    if not reason:
        raise ApiError(
            422,
            QUOTE_VOID_REASON_REQUIRED,
            "作废必须填写原因",
            field="reason",
        )

    now = clock.now()
    quote.status = "void"
    quote.void_reason = reason
    quote.voided_at = now
    quote.updated_at = now

    db.commit()
    db.refresh(quote)
    return quote


# --------------------------------------------------------------------------- #
# 7. 审批结论（补充接口，供审批系统 / 定时任务闭环使用）
# --------------------------------------------------------------------------- #
@router.post("/quotes/{qid}/decision", response_model=QuoteOut, summary="审批结论（补充）")
def decide_quote(
    qid: uuid.UUID, body: DecisionRequest, db: Session = Depends(get_db)
) -> SalesQuote:
    quote = _get_quote(db, qid)
    if quote.status in FINAL_STATUSES:
        raise ApiError(409, QUOTE_STATUS_INVALID, f"状态 {quote.status} 不可审批")

    now = clock.now()
    if body.decision == "approved":
        quote.status = "approved"
        quote.approved_at = now
        # 硬规则：审批通过后 15 天有效
        quote.valid_until = now + timedelta(days=get_settings().quote_valid_days)
    elif body.decision == "rejected":
        quote.status = "rejected"
        quote.rejected_at = now
        if not (body.reason or "").strip():
            raise ApiError(422, "QUOTE_REJECT_REASON_REQUIRED", "驳回必须填写原因", field="reason")
    else:
        quote.status = "pending_approval"

    quote.decision_reason = (body.reason or None)
    quote.updated_at = now

    db.commit()
    db.refresh(quote)
    return quote


# --------------------------------------------------------------------------- #
# 8. 列表 / 详情
# --------------------------------------------------------------------------- #
@router.get("/quotes", response_model=QuoteListOut, summary="报价单列表（过滤+分页）")
def list_quotes(
    db: Session = Depends(get_db),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    lead_id: Optional[uuid.UUID] = Query(default=None),
    site_id: Optional[uuid.UUID] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> QuoteListOut:
    stmt = select(SalesQuote).where(SalesQuote.deleted_at.is_(None))
    if status_filter:
        stmt = stmt.where(SalesQuote.status == status_filter)
    if lead_id:
        stmt = stmt.where(SalesQuote.lead_id == lead_id)
    if site_id:
        stmt = stmt.where(SalesQuote.site_id == site_id)

    total = int(db.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())
    rows = (
        db.execute(
            stmt.order_by(SalesQuote.created_at.desc(), SalesQuote.revision.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        .scalars()
        .all()
    )
    pages = math.ceil(total / page_size) if total else 0
    return QuoteListOut(
        items=[QuoteOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
        pages=pages,
    )


@router.get("/quotes/{qid}", response_model=QuoteOut, summary="报价单详情")
def get_quote(qid: uuid.UUID, db: Session = Depends(get_db)) -> SalesQuote:
    return _get_quote(db, qid)


@router.delete("/quotes/{qid}", status_code=status.HTTP_204_NO_CONTENT, summary="软删除（补充）")
def delete_quote(qid: uuid.UUID, db: Session = Depends(get_db)) -> Response:
    quote = _get_quote(db, qid)
    now = clock.now()
    quote.deleted_at = now
    quote.updated_at = now
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
