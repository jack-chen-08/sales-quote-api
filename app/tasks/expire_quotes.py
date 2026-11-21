"""过期报价单定时任务。

规则：status ∈ {approved, sent} 且 valid_until <= now 的报价单 → expired。
valid_until 在审批通过时写为 approved_at + 15 天（settings.quote_valid_days）。

用法::

    # 单次执行（cron 每 10 分钟一次）
    DATABASE_URL=postgresql+psycopg://... .venv/bin/python -m app.tasks.expire_quotes

    # crontab 示例
    */10 * * * * cd /srv/ch2398 && DATABASE_URL=... .venv/bin/python -m app.tasks.expire_quotes

可注入 clock：``expire_overdue_quotes(session, now=frozen_dt)``，测试无需真等 15 天。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import clock
from ..models import SalesQuote

EXPIRABLE_STATUSES = ("approved", "sent")


def expire_overdue_quotes(
    session: Session, now: Optional[datetime] = None, limit: Optional[int] = None
) -> list[dict[str, str]]:
    """把过期报价置为 expired，返回被处理记录（id / quote_no / revision）。"""
    now = now or clock.now()

    candidates = session.execute(
        select(SalesQuote.id, SalesQuote.quote_no, SalesQuote.revision)
        .where(
            SalesQuote.status.in_(EXPIRABLE_STATUSES),
            SalesQuote.valid_until.is_not(None),
            SalesQuote.valid_until <= now,
            SalesQuote.deleted_at.is_(None),
        )
        .order_by(SalesQuote.valid_until.asc())
        .limit(limit or 10000)
    ).all()

    if not candidates:
        return []

    ids = [row[0] for row in candidates]
    session.execute(
        update(SalesQuote)
        .where(SalesQuote.id.in_(ids), SalesQuote.status.in_(EXPIRABLE_STATUSES))
        .values(status="expired", updated_at=now)
    )
    session.commit()

    return [
        {"id": str(row[0]), "quote_no": row[1], "revision": str(row[2])} for row in candidates
    ]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="过期报价单批处理")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写库")
    parser.add_argument("--limit", type=int, default=None, help="单次最多处理条数")
    args = parser.parse_args(argv)

    from ..db import get_sessionmaker
    from ..settings import get_settings

    if not get_settings().database_url.startswith("postgresql"):
        print("ERROR: 请通过环境变量 DATABASE_URL 指定 PostgreSQL 连接串", file=sys.stderr)
        return 2

    session = get_sessionmaker()()
    try:
        if args.dry_run:
            now = clock.now()
            rows = session.execute(
                select(SalesQuote.id).where(
                    SalesQuote.status.in_(EXPIRABLE_STATUSES),
                    SalesQuote.valid_until.is_not(None),
                    SalesQuote.valid_until <= now,
                    SalesQuote.deleted_at.is_(None),
                )
            ).all()
            print(json.dumps({"dry_run": True, "would_expire": len(rows)}))
            return 0

        expired = expire_overdue_quotes(session, limit=args.limit)
        print(json.dumps({"expired": len(expired), "items": expired}, ensure_ascii=False))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
