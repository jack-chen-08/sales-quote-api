"""漏斗状态机（**桩实现**）。

对接点
------
真实漏斗服务接入时，替换 ``advance()`` 的实现（保持签名）：

    advance(session, lead_id, to_status, trigger="system") -> FunnelResult

调用约定（本单硬规则）：
  * 线索阶段为 ``visit_scheduled`` 且这是该线索的**第一张**草稿时，
    建草稿后调用 ``advance(..., "quoting", trigger="system")``；
  * 更早阶段（new / contacted）允许建草稿但**不推进**漏斗。

单元测试通过 monkeypatch 替换 ``app.funnel.advance`` 为 spy 来断言调用参数，
同时本模块自带 ``CALLS`` 记录，便于非侵入式观察。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

STAGES: tuple[str, ...] = (
    "new",
    "contacted",
    "visit_scheduled",
    "quoting",
    "negotiating",
    "won",
    "lost",
)

# 允许的推进路径（trigger=system 的业务推进只允许向前）
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "new": ("contacted", "lost"),
    "contacted": ("visit_scheduled", "lost"),
    "visit_scheduled": ("quoting", "lost"),
    "quoting": ("negotiating", "lost"),
    "negotiating": ("won", "lost"),
    "won": (),
    "lost": (),
}

# 观察窗口：每次调用都记录，测试与排障可用
CALLS: list[dict[str, Any]] = []


@dataclass
class FunnelResult:
    advanced: bool
    lead_id: str
    from_status: Optional[str]
    to_status: str
    trigger: str
    reason: Optional[str] = None


def calls() -> list[dict[str, Any]]:
    return list(CALLS)


def reset_calls() -> None:
    CALLS.clear()


def can_transition(from_status: str, to_status: str) -> bool:
    return to_status in TRANSITIONS.get(from_status, ())


def advance(
    session,
    lead_id: uuid.UUID | str,
    to_status: str,
    trigger: str = "system",
    now: Optional[datetime] = None,
    reason: Optional[str] = None,
) -> FunnelResult:
    """推进线索漏斗状态。

    本桩实现直接改 ``leads.stage``；真实实现应调用漏斗服务/状态机 SDK。
    非法流转不抛错，返回 ``advanced=False``（由调用方决定是否告警）。
    """
    from .models import Lead

    lead_uid = lead_id if isinstance(lead_id, uuid.UUID) else uuid.UUID(str(lead_id))
    record = {
        "lead_id": str(lead_uid),
        "to_status": to_status,
        "trigger": trigger,
        "at": (now or datetime.now(tz=None)).isoformat() if now else None,
    }
    CALLS.append(record)

    lead = session.get(Lead, lead_uid) if session is not None else None
    if lead is None:
        return FunnelResult(False, str(lead_uid), None, to_status, trigger, "lead_not_found")

    from_status = lead.stage
    if from_status == to_status:
        return FunnelResult(False, str(lead_uid), from_status, to_status, trigger, "already_in_status")
    if not can_transition(from_status, to_status):
        return FunnelResult(False, str(lead_uid), from_status, to_status, trigger, "illegal_transition")

    lead.stage = to_status
    return FunnelResult(True, str(lead_uid), from_status, to_status, trigger, reason)
