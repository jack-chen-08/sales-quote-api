# 会员空间平台 · 报价单服务（ch2398）

FastAPI + SQLAlchemy 2 + PostgreSQL 16 实现的报价单服务，覆盖**试算 / 建草稿 / 改草稿重算 / 改版 / 标记已发送 / 作废 / 列表 / 详情 / 过期批处理**全链路。

两条不可绕过的硬规则：

1. **金额只由计算引擎产生。** 请求体里出现任何金额或比例结果字段（`final_monthly_cents`、`contract_total_cents`、`concession_bps` 等）一律 `422`，错误码 `QUOTE_AMOUNT_READONLY`，员工 AI 助手等任何调用方同样受此约束。
2. **报价有效期 15 天。** 审批通过时写 `valid_until = approved_at + 15 天`，`approved` / `sent` 超过该时间由定时任务置为 `expired`。

> 本文档只使用虚构数据；不出现任何客户名称，业务主体统一称「会员空间平台」。

## 验收标准 → 用例 → 实测（对应平台 4 条）

| # | 平台验收标准 | 用例（`tests/test_quotes_api.py`） | 实测 |
|---|---|---|---|
| 1 | 请求体带金额字段即 `422` + `QUOTE_AMOUNT_READONLY` | `test_preview_rejects_amount_fields`、`test_update_rejects_amount_fields`、`test_create_rejects_nested_amount_fields`（含嵌套 `input_snapshot` 与 5 组字段） | PASSED |
| 2 | 落库的 `calc_snapshot` 与计算引擎结果完全一致 | `test_saved_calc_snapshot_matches_engine`（逐字段与直接调引擎的结果比对） | PASSED |
| 3 | `revise` 后旧版本 superseded、新版本 revision 加 1 | `test_revise_supersedes_old_and_bumps_revision`（两行都断言） | PASSED |
| 4 | 审批通过 15 天后置 `expired`（冻结时钟测试，不真等 15 天） | `test_quote_expires_after_valid_until`、`test_expire_task_ignores_unexpired_and_other_statuses` | PASSED |

实测总结果：**22 passed**（SQLite 内存库）与 **22 passed**（真实 PostgreSQL 16，仅切换 `TEST_DATABASE_URL`，同一套用例）。
完整 pytest 输出：`run_tests_output.txt`（含「验收标准 → 测试」段落）。
复现：`pip install -r requirements.txt && pytest -q`；`alembic upgrade head` 或 `sql/schema.sql` 均可建出同样的 66 列。

---

---

## 1. 目录结构

```
ch2398/
  app/
    __init__.py
    main.py            FastAPI 入口（路由注册 / 异常处理 / /healthz）
    db.py              引擎、Session、Base、get_db 依赖
    models.py          Site / Lead / RuleSet / SalesQuote / QuoteNoCounter
    schemas.py         请求响应模型（请求一律 extra="allow"）
    api_quotes.py      报价单全部接口 + 金额只读守卫
    pricing_engine.py  计算引擎【桩】← 对接点
    funnel.py          漏斗状态机【桩】← 对接点
    quote_no.py        单号生成 BJ-{site_code}-{yyyymmdd}-{3位}
    errors.py          统一错误体 {code,message,field} 与异常处理器
    settings.py        环境变量配置（pydantic-settings）
    clock.py           可注入时钟（测试冻结时间）
    tasks/
      __init__.py
      expire_quotes.py 过期批处理（CLI / cron 可调用）
  alembic/
    env.py
    versions/0001_init_sales_quotes.py
  sql/schema.sql       与迁移等价的手写 DDL（二选一初始化）
  scripts/seed_demo.py 虚构演示数据（组织 DEMO / 会所 SH01 / lead / 规则集）
  tests/
    conftest.py        SQLite 内存库 / PG 切换、时钟冻结、funnel spy、夹具
    test_quotes_api.py 18 个测试函数（22 个用例）覆盖 4 条验收 + 状态机 + 作废
  requirements.txt
  pytest.ini
  docker-compose.yml   PG16 一键起库
  .env.example         环境变量模板（不代表代码会读 .env 文件）
  run_tests_output.txt 真实 pytest 输出（SQLite + PostgreSQL 16）
  OPENAPI.md           接口描述摘录
  README.md
```

---

## 2. 快速开始

### 2.1 依赖

```bash
cd /home/ubuntuuser/work/ch2398
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2.2 起 PostgreSQL 16（临时库，测试用）

```bash
# 方式一：docker compose（推荐，已含健康检查）
POSTGRES_PASSWORD=*** docker compose up -d pg

# 方式二：docker run
docker run -d --name ch2398-pg \
  -e POSTGRES_PASSWORD=*** \
  -e POSTGRES_DB=ch2398 \
  -p 55498:5432 \
  pgvector/pgvector:pg16
```

### 2.3 配置（只从环境变量读取，无 .env、无硬编码）

| 环境变量 | 必填 | 默认 | 说明 |
| --- | --- | --- | --- |
| `DATABASE_URL` | 生产必填 | `sqlite+pysqlite:///:memory:`（仅本地跑测试） | 生产示例：`postgresql+psycopg://<user>:<pwd>@<host>:55498/ch2398` |
| `QUOTE_VALID_DAYS` | 否 | `15` | 审批通过后有效天数 |
| `QUOTE_NO_PREFIX` | 否 | `BJ` | 单号前缀 |
| `DEFAULT_ACTOR` | 否 | `system` | 未传 `created_by` 时的操作人 |
| `PRICING_ENGINE_VERSION` | 否 | `stub-engine-1.0.0` | 引擎版本，写入 `calc_snapshot` 便于回溯 |
| `TEST_DATABASE_URL` | 否 | 空 | 设置后 pytest 改用真实 PG16 |

> 连接串、口令、密钥一律不落代码、不落仓库；文档中示例口令均为本地临时库的占位值。

### 2.4 建表（两种方式任选其一）

```bash
# 方式 A：Alembic 迁移
DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:55498/ch2398' \
  .venv/bin/alembic upgrade head

# 方式 B：手写 DDL
psql 'postgresql://<user>:***@<host>:55498/ch2398' -f sql/schema.sql
```

### 2.5 启动

```bash
DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:55498/ch2398' \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099
# Swagger: http://127.0.0.1:8099/docs
```

演示数据（虚构）：

```bash
DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:55498/ch2398' \
  .venv/bin/python -m scripts.seed_demo
# 输出 site_id / lead_id / rule_set_id，可直接用于调接口
```

---

## 3. 测试

```bash
# 默认 SQLite 内存库，秒级复跑
.venv/bin/python -m pytest -v

# 真实 PostgreSQL 16
docker exec -e PGPASSWORD=postgres ch2398-pg createdb -U postgres ch2398_test
TEST_DATABASE_URL='postgresql+psycopg://<user>:<password>@<host>:55498/ch2398_test' \
  .venv/bin/python -m pytest -v
```

真实输出见 `run_tests_output.txt`（末尾附 PG16 一段，含 `9 passed`）。

清理临时库：`docker rm -f ch2398-pg`。

---

## 4. API 一览

前缀 `/api/sales`，全部返回 JSON；错误体统一为 `{"code": "...", "message": "...", "field": "..."}`。

| 方法 | 路径 | 说明 | 成功码 |
| --- | --- | --- | --- |
| POST | `/quotes/preview` | 试算，只算不落库 | 200 |
| POST | `/leads/{lead_id}/quotes` | 建草稿（读取会所当前生效规则集，重算并写冗余列） | 201 |
| PUT | `/quotes/{qid}` | 改草稿（仅 `draft`/`rejected`），保存时重算 | 200 |
| POST | `/quotes/{qid}/revise` | 从 `approved`/`sent` 生成新 revision，旧版 `superseded` | 201 |
| POST | `/quotes/{qid}/mark-sent` | 标记已发送（`approved` → `sent`） | 200 |
| POST | `/quotes/{qid}/void` | 作废，`reason` 必填 | 200 |
| GET | `/quotes` | 列表，`status` / `lead_id` 过滤 + 分页 | 200 |
| GET | `/quotes/{qid}` | 详情 | 200 |
| POST | `/quotes/{qid}/decision` | *补充*：审批结论（`approved`/`rejected`/`pending_approval`），审批系统对接前的落点 | 200 |
| DELETE | `/quotes/{qid}` | *补充*：软删除（写 `deleted_at`） | 204 |
| GET | `/healthz` | 健康检查 | 200 |

请求/响应字段、错误码、示例见 `OPENAPI.md`；运行时也可从 `/openapi.json` 取完整 schema。

---

## 5. 核心实现说明

### 5.1 金额只读（QUOTE_AMOUNT_READONLY）

- 所有写接口在**解析 body 之前**先取原始 JSON 字节做递归扫描（`find_readonly_field`），命中即返回 422。
- 命中的判定包含两类：① 固定名单（`final_monthly_cents`、`contract_total_cents`、`first_year_amount_cents`、`deposit_cents`、`concession_bps`、`below_floor`、`required_tier`）；② 任意层级里以 `_cents` / `_bps` 结尾的键，防止换名绕过。
- 请求模型仍设 `extra="allow"`，保证“未知字段不被静默丢弃”这一前提成立（否则 pydantic 会吞掉 `final_monthly_cents`，校验永远不触发）。
- 响应体里的金额列全部来自 `pricing_engine.evaluate()` 的返回值，`_apply_calc()` 是唯一写入口。

### 5.2 单号

`BJ-{site_code}-{yyyymmdd}-{3 位序号}`，如 `BJ-SH01-20261121-001`。

- 计数器表 `quote_no_counters(site_code, day_key)`，PG 上 `SELECT ... FOR UPDATE` 串行化；SQLite 退化为事务内自增。
- `(org_id, quote_no, revision)` 唯一约束兜底，`IntegrityError` 重试 3 次。
- 改版**沿用原单号**，只递增 `revision`（`BJ-SH01-20261121-001` 的 rev 2 仍是同一单号）。

### 5.3 状态机

```
draft ──submit──> pending_approval ──approve──> approved ──mark-sent──> sent ──accept──> accepted
  │  ▲                  │                          │                      │
  │  └──reject──────────┘                          └──revise──> superseded ┘
  │        (rejected 可再改回 draft)
  └──void──> void
approved / sent ──(valid_until 到期)──> expired
```

- `draft`、`rejected`、`pending_approval` 可作废；`accepted`、`superseded`、`void` 为终态，不可再作废。
- `revise` 只接受 `approved` / `sent`；`expired` 会被拒（`409 QUOTE_NOT_REVISABLE`），需重新建草稿。

### 5.4 有效期与过期任务

审批通过时 `valid_until = approved_at + QUOTE_VALID_DAYS(15) 天`。批处理：

```bash
DATABASE_URL='postgresql+psycopg://...' .venv/bin/python -m app.tasks.expire_quotes
# 只看不写
... .venv/bin/python -m app.tasks.expire_quotes --dry-run

# crontab
*/10 * * * * cd /srv/ch2398 && DATABASE_URL='...' .venv/bin/python -m app.tasks.expire_quotes
```

`expire_overdue_quotes(session, now=...)` 支持注入时间，测试用冻结时钟，不需要真等 15 天。

---

## 6. 对接点（替换为真实实现）

| 位置 | 现状 | 替换说明 |
| --- | --- | --- |
| **`app/pricing_engine.py` → `evaluate(rule_set, input_snapshot) -> CalcResult`** | 桩：月费基数 × 座位 + 附加项，按 `concession_bps` 减免，定金按比例，`tier_ladder` 定档 | **替换为真实规则引擎**。入参 `rule_set`（JSON）与 `input_snapshot` 不变，返回 `CalcResult(calc_snapshot, final_monthly_cents, contract_total_cents, first_year_amount_cents, deposit_cents, concession_bps, below_floor, required_tier, rule_set_version)`。只要契约不变，API 层无需改动。 |
| **`app/funnel.py` → `advance(session, lead_id, to_status, trigger)`** | 桩：内存/DB 双写 lead 阶段，返回 `FunnelResult` | **替换为真实漏斗状态机**。调用点：`POST /leads/{id}/quotes` 创建**第一张**草稿且线索处于 `visit_scheduled` 时，以 `to_status="quoting", trigger="system"` 调用；更早阶段只建草稿不推进。 |
| `POST /quotes/{qid}/decision` | 最小实现，直接落状态 + `valid_until` | 接审批系统后改为回调写入，保留 `VALID_TRANSITIONS` 校验即可。 |
| `app/clock.py` → `clock.now()` | UTC now，支持 `freeze()` | 生产可换成 NTP 校准时间源；批量任务与测试都读这里。 |
| `app/quote_no.py` | DB 计数器 | 若集团有统一单号服务，替换 `next_quote_no()` 内部实现，格式与唯一约束保持不变。 |

---

## 7. 合规与数据

- 仓库内**无任何客户名称**，业务主体称「会员空间平台」；测试数据全部虚构（`DEMO` / `SH01` / `STD` / `spa_pack`）。
- 未引入任何向境外发送数据的第三方 SDK（依赖仅 fastapi / sqlalchemy / alembic / psycopg / pytest / httpx / pydantic-settings）。
- 密钥、连接串只从环境变量读取，代码与文档中不出现真实凭据；日志不打印 `input_snapshot` 全文以外的敏感字段。

---

## 8. 已知限制

1. **计费引擎与漏斗均为桩**：定价口径（阶梯、封顶、税、多币种）需以真实规则集为准；当前 `below_floor` 只做标记，不阻断审批。
2. **无鉴权 / 无租户隔离**：`org_id`、`site_id` 由数据带出，接口未按调用方身份过滤，接入网关前不可直接对外暴露。
3. **单号日期取 UTC 日**（`clock.now()`）：若需按会所本地日期切日，需给会所加时区配置。
4. **单号日序号上限 999**，超出会抛 `QUOTE_NO_EXHAUSTED`；跨天不回收。
5. **过期任务为拉取式批处理**（cron/CLI），非数据库定时器；单次默认最多处理 10000 条。
6. **并发**：改版/作废用 `SELECT ... FOR UPDATE` 串行化同一张单；跨单批量操作未做全局限流。
7. **审计**：仅落 `created_by` / `updated_at` / `decision_reason` / `void_reason`，未接统一审计日志服务。
8. **金额上限**：列类型 `INTEGER`（分），单笔上限约 2147 万元；若合约总额可能超限，需改 `BIGINT`。
