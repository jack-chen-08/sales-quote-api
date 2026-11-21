"""演示数据种子（仅虚构数据，用于本地联调 / 冒烟）。

用法::

    DATABASE_URL=postgresql+psycopg://<user>:<password>@<host>:55498/ch2398 \
      .venv/bin/python -m scripts.seed_demo
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta

from app import clock, pricing_engine
from app.db import get_sessionmaker
from app.models import Lead, RuleSet, Site


def seed() -> dict[str, str]:
    now = clock.now()
    org_id = uuid.uuid4()
    site_id = uuid.uuid4()
    session = get_sessionmaker()()
    try:
        site = Site(
            id=site_id,
            org_id=org_id,
            site_id=site_id,
            code="SH01",
            name="会员空间·演示会所",
            created_at=now,
            created_by="seed",
            updated_at=now,
        )
        lead = Lead(
            org_id=org_id,
            site_id=site_id,
            name="演示线索",
            stage="visit_scheduled",
            created_at=now,
            created_by="seed",
            updated_at=now,
        )
        rules = dict(pricing_engine.DEFAULT_RULES)
        rules["version"] = "v2026.03"
        rule_set = RuleSet(
            org_id=org_id,
            site_id=site_id,
            version="v2026.03",
            effective_from=now - timedelta(days=1),
            is_active=True,
            rules=rules,
            created_at=now,
            created_by="seed",
            updated_at=now,
        )
        session.add_all([site, lead, rule_set])
        session.commit()
        return {
            "org_id": str(org_id),
            "site_id": str(site_id),
            "lead_id": str(lead.id),
            "rule_set_id": str(rule_set.id),
        }
    finally:
        session.close()


if __name__ == "__main__":
    print(json.dumps(seed(), ensure_ascii=False))
