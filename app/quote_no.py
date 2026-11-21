"""报价单号生成：``BJ-{site_code}-{yyyymmdd}-{3 位序号}``。

序号按 (site_code, 日期) 维度递增，落库在 ``quote_no_counters`` 表里。
PostgreSQL 上用 ``SELECT ... FOR UPDATE`` 串行化；SQLite（测试）退化为事务内自增。
并发下若唯一约束冲突（IntegrityError），用 SAVEPOINT 回滚后重试。
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import clock
from .models import QuoteNoCounter
from .settings import get_settings

QUOTE_NO_PATTERN = re.compile(r"^BJ-[A-Z0-9]{1,16}-\d{8}-\d{3}$")

MAX_RETRY = 8


def format_quote_no(prefix: str, site_code: str, when: date, seq: int) -> str:
    if seq > 999:
        raise ValueError("当日单号已用尽（>999）")
    return f"{prefix}-{site_code}-{when.strftime('%Y%m%d')}-{seq:03d}"


def next_quote_no(
    session: Session,
    site_code: str,
    when: Optional[datetime] = None,
    prefix: Optional[str] = None,
) -> str:
    """分配下一个报价单号（调用方负责提交事务）。"""
    when = when or clock.now()
    prefix = prefix or get_settings().quote_no_prefix
    day_key = when.strftime("%Y%m%d")

    last_error: Optional[Exception] = None
    for _ in range(MAX_RETRY):
        counter = session.execute(
            select(QuoteNoCounter)
            .where(QuoteNoCounter.site_code == site_code, QuoteNoCounter.day_key == day_key)
            .with_for_update()
        ).scalar_one_or_none()

        if counter is None:
            counter = QuoteNoCounter(
                site_code=site_code,
                day_key=day_key,
                last_seq=1,
                created_at=when,
                updated_at=when,
            )
            session.add(counter)
        else:
            counter.last_seq = counter.last_seq + 1
            counter.updated_at = when

        try:
            with session.begin_nested():
                session.flush()
            return format_quote_no(prefix, site_code, when, counter.last_seq)
        except IntegrityError as exc:  # 并发插入同一 (site_code, day) 行
            last_error = exc
            session.expunge(counter) if counter in session else None
            continue

    raise RuntimeError(f"分配报价单号失败: {last_error}")


def parse_quote_no(quote_no: str) -> tuple[str, str, int]:
    """解析单号 → (site_code, yyyymmdd, seq)。"""
    parts = quote_no.split("-")
    if len(parts) != 4:
        raise ValueError(f"非法报价单号: {quote_no}")
    return parts[1], parts[2], int(parts[3])
