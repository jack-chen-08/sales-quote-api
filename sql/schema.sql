-- =============================================================================
-- 会员空间平台 · 报价单服务 建表 DDL（PostgreSQL 16）
-- 与 alembic/versions/0001_init_sales_quotes.py 等价，二者任选其一：
--   A) alembic upgrade head
--   B) psql -f sql/schema.sql
-- 金额一律以「分」存 integer；JSONB 用于 input_snapshot / calc_snapshot / rules。
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS sites (
    id          uuid        NOT NULL DEFAULT gen_random_uuid(),
    org_id      uuid        NOT NULL,
    site_id     uuid        NOT NULL,
    code        varchar(16) NOT NULL,
    name        varchar(128) NOT NULL,
    created_at  timestamptz NOT NULL,
    created_by  varchar(64) NOT NULL,
    updated_at  timestamptz NOT NULL,
    deleted_at  timestamptz,
    CONSTRAINT sites_pkey PRIMARY KEY (id),
    CONSTRAINT uq_sites_org_code UNIQUE (org_id, code)
);
COMMENT ON TABLE sites IS '会所；code 用于报价单号 BJ-{site_code}-...';

CREATE TABLE IF NOT EXISTS leads (
    id            uuid        NOT NULL DEFAULT gen_random_uuid(),
    org_id        uuid        NOT NULL,
    site_id       uuid        NOT NULL,
    name          varchar(128) NOT NULL,
    stage         varchar(32) NOT NULL,
    contact_phone varchar(32),
    created_at    timestamptz NOT NULL,
    created_by    varchar(64) NOT NULL,
    updated_at    timestamptz NOT NULL,
    deleted_at    timestamptz,
    CONSTRAINT leads_pkey PRIMARY KEY (id)
);
COMMENT ON COLUMN leads.stage IS 'new/contacted/visit_scheduled/quoting/negotiating/won/lost';

CREATE TABLE IF NOT EXISTS rule_sets (
    id             uuid        NOT NULL DEFAULT gen_random_uuid(),
    org_id         uuid        NOT NULL,
    site_id        uuid        NOT NULL,
    version        varchar(32) NOT NULL,
    effective_from timestamptz NOT NULL,
    effective_to   timestamptz,
    is_active      boolean     NOT NULL,
    rules          jsonb       NOT NULL,
    created_at     timestamptz NOT NULL,
    created_by     varchar(64) NOT NULL,
    updated_at     timestamptz NOT NULL,
    deleted_at     timestamptz,
    CONSTRAINT rule_sets_pkey PRIMARY KEY (id),
    CONSTRAINT uq_rule_sets_site_version UNIQUE (site_id, version)
);
COMMENT ON TABLE rule_sets IS '会所报价规则集（版本化 + 生效区间）';

CREATE TABLE IF NOT EXISTS sales_quotes (
    id                       uuid        NOT NULL DEFAULT gen_random_uuid(),
    org_id                   uuid        NOT NULL,
    site_id                  uuid        NOT NULL,
    lead_id                  uuid        NOT NULL,
    quote_no                 varchar(64) NOT NULL,
    revision                 integer     NOT NULL,
    rule_set_id              uuid        NOT NULL,
    input_snapshot           jsonb       NOT NULL,
    calc_snapshot            jsonb       NOT NULL,
    final_monthly_cents      integer     NOT NULL,
    contract_total_cents     integer     NOT NULL,
    first_year_amount_cents  integer     NOT NULL,
    deposit_cents            integer     NOT NULL,
    concession_bps           integer     NOT NULL,
    below_floor              boolean     NOT NULL,
    required_tier            varchar(16) NOT NULL,
    status                   varchar(32) NOT NULL,
    valid_until              timestamptz,
    sent_at                  timestamptz,
    approved_at              timestamptz,
    rejected_at              timestamptz,
    decision_reason          varchar(512),
    void_reason              varchar(512),
    voided_at                timestamptz,
    superseded_by_id         uuid,
    created_at               timestamptz NOT NULL,
    created_by               varchar(64) NOT NULL,
    updated_at               timestamptz NOT NULL,
    deleted_at               timestamptz,
    CONSTRAINT sales_quotes_pkey PRIMARY KEY (id),
    CONSTRAINT uq_sales_quotes_no_rev UNIQUE (org_id, quote_no, revision),
    CONSTRAINT sales_quotes_lead_id_fkey FOREIGN KEY (lead_id) REFERENCES leads (id),
    CONSTRAINT sales_quotes_rule_set_id_fkey FOREIGN KEY (rule_set_id) REFERENCES rule_sets (id),
    CONSTRAINT ck_sales_quotes_status CHECK (
        status IN ('draft','pending_approval','approved','rejected','sent',
                   'accepted','expired','superseded','void')
    ),
    CONSTRAINT ck_sales_quotes_revision CHECK (revision >= 1)
);
COMMENT ON TABLE sales_quotes IS '报价单；金额列只能由 pricing_engine.evaluate() 写入';
COMMENT ON COLUMN sales_quotes.revision IS '改版序号，同一 quote_no 下递增';
COMMENT ON COLUMN sales_quotes.valid_until IS 'approved_at + 15 天；到期由定时任务置 expired';
COMMENT ON COLUMN sales_quotes.calc_snapshot IS '计算引擎完整输出，与引擎直算结果逐字段一致';

CREATE TABLE IF NOT EXISTS quote_no_counters (
    id         uuid        NOT NULL DEFAULT gen_random_uuid(),
    site_code  varchar(16) NOT NULL,
    day_key    varchar(8)  NOT NULL,
    last_seq   integer     NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT quote_no_counters_pkey PRIMARY KEY (id),
    CONSTRAINT uq_quote_no_counters UNIQUE (site_code, day_key)
);
COMMENT ON TABLE quote_no_counters IS '按 (会所, 日期) 的报价单号日序号，保证 3 位序号';

CREATE INDEX IF NOT EXISTS ix_sites_org_id ON sites (org_id);
CREATE INDEX IF NOT EXISTS ix_sites_site_id ON sites (site_id);
CREATE INDEX IF NOT EXISTS ix_leads_org_id ON leads (org_id);
CREATE INDEX IF NOT EXISTS ix_leads_site_id ON leads (site_id);
CREATE INDEX IF NOT EXISTS ix_leads_site_stage ON leads (site_id, stage);
CREATE INDEX IF NOT EXISTS ix_rule_sets_org_id ON rule_sets (org_id);
CREATE INDEX IF NOT EXISTS ix_rule_sets_site_id ON rule_sets (site_id);
CREATE INDEX IF NOT EXISTS ix_rule_sets_site_effective ON rule_sets (site_id, effective_from);
CREATE INDEX IF NOT EXISTS ix_sales_quotes_org_id ON sales_quotes (org_id);
CREATE INDEX IF NOT EXISTS ix_sales_quotes_site_id ON sales_quotes (site_id);
CREATE INDEX IF NOT EXISTS ix_sales_quotes_lead_status ON sales_quotes (lead_id, status);
CREATE INDEX IF NOT EXISTS ix_sales_quotes_status_valid_until ON sales_quotes (status, valid_until);

COMMIT;
