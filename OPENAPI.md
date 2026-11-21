# 报价单服务 · 接口描述摘录

Base URL：`/api/sales`；Content-Type：`application/json`；金额单位一律为**分**（整数）。

运行时完整 schema：`GET /openapi.json`，交互文档：`GET /docs`。

---

## 0. 通用约定

### 0.1 错误体

```json
{
  "code": "QUOTE_AMOUNT_READONLY",
  "message": "金额与比例结果字段只允许由计算引擎产生，请求体中不得出现；如需调整请修改输入快照（座位数 / 合约月数 / 方案）由引擎重算。",
  "field": "final_monthly_cents",
  "extra": {"path": "input_snapshot.final_monthly_cents"}
}
```

| code | HTTP | 触发条件 |
| --- | --- | --- |
| `QUOTE_AMOUNT_READONLY` | 422 | 请求体任意层级出现金额/比例结果字段（含 `*_cents`、`*_bps` 后缀或固定名单） |
| `VALIDATION_ERROR` | 422 | 参数类型/必填校验失败（`errors[]` 给出 loc/msg/type） |
| `INVALID_JSON` | 400 | 请求体不是合法 JSON |
| `QUOTE_NOT_FOUND` / `LEAD_NOT_FOUND` / `RULE_SET_NOT_FOUND` | 404 | 对象不存在或已软删除 |
| `NO_ACTIVE_RULE_SET` | 409 | 会所无当前生效规则集 |
| `QUOTE_NOT_EDITABLE` | 409 | 非 `draft`/`rejected` 调用 PUT |
| `QUOTE_NOT_REVISABLE` | 409 | 非 `approved`/`sent` 调用 revise |
| `QUOTE_NOT_SENDABLE` | 409 | 非 `approved` 调用 mark-sent |
| `QUOTE_VOID_REASON_REQUIRED` | 422 | 作废未填 `reason` 或为空白 |
| `QUOTE_STATUS_INVALID` | 409 | 状态流转不允许（如终态再作废） |
| `INVALID_INPUT_SNAPSHOT` | 422 | 输入快照缺字段/取值非法（引擎抛错） |

> 金额只读守卫是**路由级依赖**，在 body 解析之前执行，所以即使其它字段也不合法，只要出现金额字段就优先返回 `QUOTE_AMOUNT_READONLY`。

### 0.2 状态

`draft` · `pending_approval` · `approved` · `rejected` · `sent` · `accepted` · `expired` · `superseded` · `void`

### 0.3 计费输入快照（`input_snapshot`）

```json
{
  "plan_code": "STD",
  "seat_count": 10,
  "addon_codes": ["spa_pack"],
  "contract_months": 24,
  "start_date": "2026-12-01"
}
```

`start_date` 可空。**不得包含任何金额字段**——金额由 `pricing_engine.evaluate()` 产出。

---

## 1. POST /quotes/preview — 试算（不落库）

请求：

```json
{
  "lead_id": "b85dfb2f-128e-490e-880e-c63070d70cc4",
  "rule_set_id": "3eefc8d5-d286-457e-bca6-97edc0b75668",
  "input_snapshot": {"plan_code": "STD", "seat_count": 10, "addon_codes": ["spa_pack"], "contract_months": 24}
}
```

`lead_id`、`rule_set_id` 均可省略：省略 `rule_set_id` 时按 `lead_id` 所属会所取当前生效规则集；两者都给时校验规则集属于该会所。

响应 `200`：

```json
{
  "calc_snapshot": {
    "engine_version": "stub-engine-1.0.0",
    "currency": "CNY",
    "unit": "cent",
    "input": {"plan_code": "STD", "seat_count": 10, "addon_codes": ["spa_pack"], "contract_months": 24, "start_date": null},
    "rule_set": {"version": "v2026.03", "tier_ladder": [{"tier": "T1", "min_contract_cents": 3000000}, {"tier": "T2", "min_contract_cents": 1000000}, {"tier": "T3", "min_contract_cents": 0}]},
    "steps": {
      "monthly_base_cents": 299000,
      "monthly_addon_cents": 12000,
      "monthly_gross_cents": 311000,
      "discount_cents": 447840,
      "final_monthly_cents": 292340,
      "contract_total_gross_cents": 7464000,
      "contract_total_cents": 7016160,
      "first_year_amount_cents": 3508080,
      "deposit_cents": 292340
    },
    "concession_bps": 600
  },
  "final_monthly_cents": 292340,
  "contract_total_cents": 7016160,
  "first_year_amount_cents": 3508080,
  "deposit_cents": 292340,
  "concession_bps": 600,
  "below_floor": false,
  "required_tier": "T1",
  "rule_set_id": "3eefc8d5-d286-457e-bca6-97edc0b75668",
  "rule_set_version": "v2026.03"
}
```

金额口径（桩引擎）：`monthly_gross = 月费基数×座位 + 附加项`；`discount = monthly_gross × months × bps / 10000`；`final_monthly = gross − monthly_gross×bps/10000`；`contract_total = final_monthly × months`；`first_year = final_monthly × 12`；`deposit = final_monthly × 定金比例`。

---

## 2. POST /leads/{lead_id}/quotes — 建草稿

请求同 preview（`input_snapshot` 必填）。服务端：取会所当前生效规则集 → 生成单号 → 调引擎 → 写冗余金额列 → `status=draft`。

响应 `201`：完整 `QuoteOut`（见 §9），关键字段：

```json
{
  "id": "8ed14f4b-7073-4412-bd87-dc68873a94cd",
  "quote_no": "BJ-SH01-20261121-001",
  "revision": 1,
  "status": "draft",
  "final_monthly_cents": 292340,
  "contract_total_cents": 7016160,
  "first_year_amount_cents": 3508080,
  "deposit_cents": 292340,
  "concession_bps": 600,
  "below_floor": false,
  "required_tier": "T1",
  "valid_until": null,
  "sent_at": null
}
```

漏斗联动：线索阶段为 `visit_scheduled` 且这是**第一张**草稿时，以 `to_status="quoting"`、`trigger="system"` 调用漏斗；更早阶段允许建草稿但不推进；已推进过不重复推进。

---

## 3. PUT /quotes/{qid} — 改草稿（保存时重算）

请求：

```json
{"input_snapshot": {"plan_code": "PRO", "seat_count": 12, "addon_codes": [], "contract_months": 36}, "updated_by": "E1001"}
```

- 仅 `draft` / `rejected` 可改，其余 `409 QUOTE_NOT_EDITABLE`。
- `rejected` 改完回到 `draft`，清空 `rejected_at` / `decision_reason`，需重新审批。
- 整份替换 `input_snapshot`，**服务端重算**并覆盖 `calc_snapshot` 与全部冗余列；单号与 revision 不变。

响应 `200`：`QuoteOut`。

---

## 4. POST /quotes/{qid}/revise — 改版

无请求体（或 `{}`）。

- 允许来源：`approved`、`sent`；其它状态 `409 QUOTE_NOT_REVISABLE`（`expired` 亦不可改版，需新建）。
- 旧版 `status=superseded`、`superseded_by_id=新版 id`。
- 新版：同 `quote_no`，`revision = 旧版 + 1`，复制 `input_snapshot` 并**重新调用引擎**，`status=draft`，金额/有效期/发送时间清零。

响应 `201`：新 `QuoteOut`。

> 唯一约束 `(org_id, quote_no, revision)`，并发改版会因约束冲突重试/失败，不会产生同号同版本两条。

---

## 5. POST /quotes/{qid}/mark-sent — 标记已发送

请求（可空）：

```json
{"channel": "feishu", "note": "已发送至客户联系人"}
```

- 仅 `approved` 可发，否则 `409 QUOTE_NOT_SENDABLE`。
- 置 `status=sent`、`sent_at=now`；`valid_until` 保持审批时算出的值（不因发送而延长）。

响应 `200`：`QuoteOut`。

---

## 6. POST /quotes/{qid}/void — 作废

请求：

```json
{"reason": "客户改选其他方案"}
```

- `reason` 必填且非空白，否则 `422 QUOTE_VOID_REASON_REQUIRED`。
- 终态 `void` 不可再作废（`409 QUOTE_STATUS_INVALID`）；`accepted`/`superseded` 需走业务例外流程，默认拒绝。
- 置 `status=void`、`void_reason`、`voided_at`。

响应 `200`：`QuoteOut`。

---

## 7. GET /quotes — 列表

| 查询参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `status` | string | 空 | 状态精确过滤 |
| `lead_id` | uuid | 空 | 按线索过滤 |
| `site_id` | uuid | 空 | 按会所过滤（补充） |
| `page` | int ≥1 | 1 | 页码 |
| `page_size` | int 1–100 | 20 | 每页条数 |

软删除记录（`deleted_at` 非空）不返回。排序：`created_at DESC, revision DESC`。

响应 `200`：

```json
{"items": [ /* QuoteOut */ ], "total": 12, "page": 1, "page_size": 20, "pages": 1}
```

---

## 8. GET /quotes/{qid} — 详情

响应 `200`：`QuoteOut`；不存在或已软删除 `404 QUOTE_NOT_FOUND`。

---

## 9. QuoteOut 字段

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `id` | uuid | 主键 |
| `org_id` / `site_id` / `lead_id` | uuid | 组织 / 会所 / 线索 |
| `quote_no` | string | `BJ-{site_code}-{yyyymmdd}-{3位}` |
| `revision` | int | 改版序号，从 1 递增 |
| `rule_set_id` | uuid | 使用的规则集 |
| `input_snapshot` | object | 输入快照（无金额） |
| `calc_snapshot` | object | 计算引擎完整输出 |
| `final_monthly_cents` | int | 最终月费 |
| `contract_total_cents` | int | 合约总额 |
| `first_year_amount_cents` | int | 首年金额 |
| `deposit_cents` | int | 定金 |
| `concession_bps` | int | 减免基点 |
| `below_floor` | bool | 是否低于底价 |
| `required_tier` | string | 所需审批档位 |
| `status` | enum | 见 §0.2 |
| `valid_until` | datetime? | 审批通过时 = `approved_at + 15 天` |
| `sent_at` | datetime? | 标记发送时间 |
| `approved_at` / `decision_reason` | datetime? / string? | 审批信息 |
| `void_reason` / `voided_at` | string? / datetime? | 作废信息 |
| `superseded_by_id` | uuid? | 被哪一版替代 |
| `created_at` / `created_by` / `updated_at` / `deleted_at` | — | 审计与软删除 |

---

## 10. 补充接口

### POST /quotes/{qid}/decision — 审批结论

```json
{"decision": "approved", "reason": "折扣在授权范围内"}
```

`decision ∈ {approved, rejected, pending_approval}`。`approved` 时写 `approved_at` 与 `valid_until = approved_at + QUOTE_VALID_DAYS(15) 天`；`rejected` 写 `rejected_at`、`decision_reason`。审批系统对接后由回调替换（见 README 对接点）。

### DELETE /quotes/{qid} — 软删除

写 `deleted_at`，返回 `204`；之后详情/列表不可见。

### GET /healthz — 健康检查

```json
{"status": "ok"}
```
