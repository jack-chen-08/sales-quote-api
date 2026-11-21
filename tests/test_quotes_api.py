"""报价单接口验收测试（8+ 例）。

覆盖 4 条硬性验收断言：
  A1 金额只读 → 422 QUOTE_AMOUNT_READONLY（test_preview_rejects_amount_fields / test_update_rejects_amount_fields）
  A2 calc_snapshot 与引擎直算逐字段一致          （test_saved_calc_snapshot_matches_engine）
  A3 revise 旧版 superseded / 新版 revision+1    （test_revise_supersedes_old_and_bumps_revision）
  A4 审批 15 天后置 expired（冻结时钟）           （test_quote_expires_after_valid_until）
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta

import pytest

from app import clock, funnel, pricing_engine
from app.models import Lead, SalesQuote
from app.quote_no import QUOTE_NO_PATTERN
from app.tasks.expire_quotes import expire_overdue_quotes
from tests.conftest import T0


def _create(client, seeded, **overrides):
    body = {"input_snapshot": seeded.input_snapshot, **overrides}
    resp = client.post(f"/api/sales/leads/{seeded.lead.id}/quotes", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _approve(client, quote_id):
    resp = client.post(f"/api/sales/quotes/{quote_id}/decision", json={"decision": "approved"})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _q(db, quote_id):
    """按字符串 / UUID 取库里的报价单（Uuid 列不接受 str）。"""
    return db.get(SalesQuote, quote_id if isinstance(quote_id, uuid.UUID) else uuid.UUID(str(quote_id)))


# --------------------------------------------------------------------------- #
# A1：请求体出现金额/比例结果字段 → 422 QUOTE_AMOUNT_READONLY
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("final_monthly_cents", 1),
        ("contract_total_cents", 100),
        ("first_year_amount_cents", 100),
        ("deposit_cents", 0),
        ("concession_bps", 9999),
    ],
)
def test_preview_rejects_amount_fields(client, seeded, bad_field, bad_value):
    body = {"lead_id": str(seeded.lead.id), "input_snapshot": seeded.input_snapshot}
    body[bad_field] = bad_value

    resp = client.post("/api/sales/quotes/preview", json=body)

    assert resp.status_code == 422, resp.text
    payload = resp.json()
    assert payload["code"] == "QUOTE_AMOUNT_READONLY"
    assert payload["field"] == bad_field


def test_create_rejects_nested_amount_fields(client, seeded):
    """嵌套在 input_snapshot 里同样拦截（员工 AI 助手不能夹带金额）。"""
    snapshot = dict(seeded.input_snapshot)
    snapshot["discount"] = {"concession_bps": 500}

    resp = client.post(
        f"/api/sales/leads/{seeded.lead.id}/quotes", json={"input_snapshot": snapshot}
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "QUOTE_AMOUNT_READONLY"
    assert "concession_bps" in resp.json()["extra"]["path"]


def test_update_rejects_amount_fields(client, seeded):
    quote = _create(client, seeded)

    resp = client.put(
        f"/api/sales/quotes/{quote['id']}",
        json={"input_snapshot": seeded.input_snapshot, "contract_total_cents": 1},
    )

    assert resp.status_code == 422
    assert resp.json()["code"] == "QUOTE_AMOUNT_READONLY"


# --------------------------------------------------------------------------- #
# A2：保存后的 calc_snapshot 与引擎直算结果逐字段相等
# --------------------------------------------------------------------------- #
def test_saved_calc_snapshot_matches_engine(client, db, seeded):
    created = _create(client, seeded)

    stored = _q(db, created["id"])
    assert stored is not None

    expected = pricing_engine.evaluate(seeded.rules, seeded.input_snapshot)

    assert stored.calc_snapshot == expected.calc_snapshot
    for field_name, value in expected.amounts.items():
        assert getattr(stored, field_name) == value, field_name
        assert created[field_name] == value, field_name
    assert stored.status == "draft"
    assert stored.revision == 1
    assert QUOTE_NO_PATTERN.match(stored.quote_no), stored.quote_no


def test_update_recomputes_amounts(client, db, seeded):
    quote = _create(client, seeded)
    bigger = dict(seeded.input_snapshot, seat_count=40, contract_months=36)

    resp = client.put(f"/api/sales/quotes/{quote['id']}", json={"input_snapshot": bigger})
    assert resp.status_code == 200, resp.text

    expected = pricing_engine.evaluate(seeded.rules, bigger)
    stored = _q(db, quote["id"])
    db.refresh(stored)
    assert stored.calc_snapshot == expected.calc_snapshot
    assert stored.contract_total_cents == expected.amounts["contract_total_cents"]
    assert stored.contract_total_cents > quote["contract_total_cents"]


# --------------------------------------------------------------------------- #
# A3：revise → 旧版 superseded + 新版 revision+1
# --------------------------------------------------------------------------- #
def test_revise_supersedes_old_and_bumps_revision(client, db, seeded):
    first = _create(client, seeded)
    _approve(client, first["id"])

    resp = client.post(f"/api/sales/quotes/{first['id']}/revise")
    assert resp.status_code == 201, resp.text
    second = resp.json()

    assert second["revision"] == first["revision"] + 1
    assert second["quote_no"] == first["quote_no"]
    assert second["status"] == "draft"

    old = _q(db, first["id"])
    db.refresh(old)
    assert old.status == "superseded"
    assert str(old.superseded_by_id) == second["id"]

    detail = client.get(f"/api/sales/quotes/{first['id']}").json()
    assert detail["status"] == "superseded"

    # draft / superseded 不能再改版
    again = client.post(f"/api/sales/quotes/{second['id']}/revise")
    assert again.status_code == 409
    assert again.json()["code"] == "QUOTE_NOT_REVISABLE"


# --------------------------------------------------------------------------- #
# A4：审批通过 15 天后 → expired（冻结时钟，不真等）
# --------------------------------------------------------------------------- #
def test_quote_expires_after_valid_until(client, db, seeded, frozen_time):
    quote = _create(client, seeded)
    approved = _approve(client, quote["id"])

    valid_until = _q(db, quote["id"]).valid_until
    assert valid_until is not None
    if valid_until.tzinfo is None:
        valid_until = valid_until.replace(tzinfo=frozen_time["t"].tzinfo)
    assert valid_until - T0 == timedelta(days=15)
    assert approved["status"] == "approved"

    # 第 14 天：仍有效
    frozen_time["t"] = T0 + timedelta(days=14)
    assert expire_overdue_quotes(db, now=clock.now()) == []

    # 第 16 天：过期
    frozen_time["t"] = T0 + timedelta(days=16)
    expired = expire_overdue_quotes(db, now=clock.now())
    assert len(expired) == 1
    assert expired[0]["quote_no"] == quote["quote_no"]

    refreshed = client.get(f"/api/sales/quotes/{quote['id']}").json()
    assert refreshed["status"] == "expired"


def test_expire_task_ignores_unexpired_and_other_statuses(client, db, seeded, frozen_time):
    draft = _create(client, seeded)
    sent_quote = _create(client, seeded)
    assert client.post(f"/api/sales/quotes/{sent_quote['id']}/mark-sent", json={}).status_code == 200

    frozen_time["t"] = T0 + timedelta(days=14)
    assert expire_overdue_quotes(db, now=clock.now()) == []

    frozen_time["t"] = T0 + timedelta(days=16)
    expired = {row["quote_no"] for row in expire_overdue_quotes(db, now=clock.now())}
    assert sent_quote["quote_no"] in expired
    assert draft["quote_no"] not in expired

    assert _q(db, draft["id"]).status == "draft"


# --------------------------------------------------------------------------- #
# 漏斗状态机
# --------------------------------------------------------------------------- #
def test_visit_scheduled_advances_funnel_with_system_trigger(client, db, seeded, funnel_spy):
    _create(client, seeded)

    assert len(funnel_spy) == 1
    call = funnel_spy[0]
    assert str(call["lead_id"]) == str(seeded.lead.id)
    assert call["to_status"] == "quoting"
    assert call["trigger"] == "system"

    lead = db.get(Lead, seeded.lead.id)
    db.refresh(lead)
    assert lead.stage == "quoting"


def test_first_draft_only_advances_once(client, db, seeded, funnel_spy):
    _create(client, seeded)
    db.refresh(db.get(Lead, seeded.lead.id))
    # 第二张草稿（同一线索）不再推进
    second = client.post(
        f"/api/sales/leads/{seeded.lead.id}/quotes", json={"input_snapshot": seeded.input_snapshot}
    )
    assert second.status_code == 201
    assert len(funnel_spy) == 1


def test_earlier_stage_creates_draft_without_advancing(client, db, seeded, funnel_spy):
    lead = db.get(Lead, seeded.lead.id)
    lead.stage = "contacted"
    db.commit()

    quote = _create(client, seeded)
    assert quote["status"] == "draft"
    assert funnel_spy == []

    db.refresh(lead)
    assert lead.stage == "contacted"


# --------------------------------------------------------------------------- #
# 作废 / 状态约束 / 列表
# --------------------------------------------------------------------------- #
def test_void_requires_reason(client, db, seeded):
    quote = _create(client, seeded)

    resp = client.post(f"/api/sales/quotes/{quote['id']}/void", json={"reason": "   "})
    assert resp.status_code == 422
    assert resp.json()["code"] == "QUOTE_VOID_REASON_REQUIRED"

    missing = client.post(f"/api/sales/quotes/{quote['id']}/void", json={})
    assert missing.status_code == 422

    ok = client.post(f"/api/sales/quotes/{quote['id']}/void", json={"reason": "客户改需求"})
    assert ok.status_code == 200
    assert ok.json()["status"] == "void"
    assert ok.json()["void_reason"] == "客户改需求"

    assert _q(db, quote["id"]).voided_at is not None


def test_put_only_allows_draft_or_rejected(client, seeded):
    quote = _create(client, seeded)
    _approve(client, quote["id"])

    resp = client.put(
        f"/api/sales/quotes/{quote['id']}", json={"input_snapshot": seeded.input_snapshot}
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "QUOTE_NOT_EDITABLE"

    # rejected 可以改，改完回到 draft
    rejected = _create(client, seeded)
    client.post(
        f"/api/sales/quotes/{rejected['id']}/decision",
        json={"decision": "rejected", "reason": "折扣过低"},
    )
    updated = client.put(
        f"/api/sales/quotes/{rejected['id']}",
        json={"input_snapshot": dict(seeded.input_snapshot, seat_count=12)},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "draft"


def test_mark_sent_sets_sent_at_and_valid_until(client, db, seeded):
    quote = _create(client, seeded)

    resp = client.post(f"/api/sales/quotes/{quote['id']}/mark-sent", json={"channel": "email"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "sent"
    assert body["sent_at"] is not None
    assert body["valid_until"] is not None

    again = client.post(f"/api/sales/quotes/{quote['id']}/mark-sent", json={})
    assert again.status_code == 409
    assert again.json()["code"] == "QUOTE_NOT_SENDABLE"


def test_list_filters_and_pagination(client, db, seeded):
    created = [_create(client, seeded) for _ in range(3)]
    client.post(f"/api/sales/quotes/{created[0]['id']}/mark-sent", json={})

    all_rows = client.get("/api/sales/quotes", params={"page": 1, "page_size": 2}).json()
    assert all_rows["total"] == 3
    assert len(all_rows["items"]) == 2
    assert all_rows["pages"] == 2

    page2 = client.get("/api/sales/quotes", params={"page": 2, "page_size": 2}).json()
    assert len(page2["items"]) == 1

    sent_only = client.get("/api/sales/quotes", params={"status": "sent"}).json()
    assert sent_only["total"] == 1
    assert sent_only["items"][0]["id"] == created[0]["id"]

    by_lead = client.get(
        "/api/sales/quotes", params={"lead_id": str(seeded.lead.id)}
    ).json()
    assert by_lead["total"] == 3


def test_quote_no_sequence_increments_per_day(client, seeded):
    first = _create(client, seeded)
    second = _create(client, seeded)

    assert re.match(r"^BJ-SH01-20260301-001$", first["quote_no"])
    assert re.match(r"^BJ-SH01-20260301-002$", second["quote_no"])


def test_preview_does_not_persist(client, db, seeded):
    resp = client.post(
        "/api/sales/quotes/preview",
        json={"lead_id": str(seeded.lead.id), "input_snapshot": seeded.input_snapshot},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected = pricing_engine.evaluate(seeded.rules, seeded.input_snapshot)
    assert body["calc_snapshot"] == expected.calc_snapshot
    assert db.query(SalesQuote).count() == 0


def test_detail_404_for_unknown_quote(client):
    resp = client.get("/api/sales/quotes/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    assert resp.json()["code"] == "QUOTE_NOT_FOUND"
