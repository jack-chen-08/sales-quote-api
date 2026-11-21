"""报价计算引擎（**桩实现**）。

对接点
------
真实计费规则引擎接入时，只需替换本模块的 ``evaluate()``（保持签名与返回结构不变）：

    evaluate(rule_set: dict, input_snapshot: dict) -> CalcResult

要求：
  * 纯函数，不访问数据库、不读时钟、无副作用；
  * 只输出整数分（cents），不产生浮点；
  * ``calc_snapshot`` 必须可 JSON 序列化且与「同一 rule_set + 同一输入」逐字节稳定，
    因为库里的 ``sales_quotes.calc_snapshot`` 会与直接调用引擎的结果做逐字段比对。

桩逻辑（自洽、可解释）
----------------------
1. 月费基数 = 规则集 ``monthly_base_cents`` × 座位数 + 加购项月费；
2. 合约总额（毛）= 月费基数 × 合约月数；
3. 减免比例 concession_bps 由「合约月数阶梯 + 座位规模阶梯」查表求和，封顶 cap；
4. 减免金额 = 合约总额毛 × bps // 10000（向下取整，分）；
5. 合约总额（净）= 毛 - 减免；
6. 月均实付 = 净 // 合约月数；首年金额 = 净 × 12 // 合约月数；
7. 定金 = 月均实付 × 定金月数；
8. 低于底价 = 月均实付 < floor_monthly_cents；
9. 审批层级 required_tier 由净合约总额查阶梯表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

ENGINE_VERSION = "stub-engine-1.0.0"

# 规则集缺省值（方便测试与本地起服务；真实规则集应完整入库）
DEFAULT_RULES: dict[str, Any] = {
    "monthly_base_cents": 29900,
    "addon_prices_cents": {"spa_pack": 12000, "meeting_room": 8000},
    "allowed_months": [12, 24, 36],
    "concession_ladder": [
        {"min_months": 0, "bps": 0},
        {"min_months": 12, "bps": 300},
        {"min_months": 24, "bps": 600},
        {"min_months": 36, "bps": 900},
    ],
    "seat_bonus_ladder": [{"min_seats": 30, "bps": 100}],
    "concession_cap_bps": 2000,
    "deposit_months": 1,
    "floor_monthly_cents": 19900,
    "tier_ladder": [
        {"tier": "T1", "min_contract_cents": 3_000_000},
        {"tier": "T2", "min_contract_cents": 1_000_000},
        {"tier": "T3", "min_contract_cents": 0},
    ],
}

AMOUNT_FIELDS = (
    "final_monthly_cents",
    "contract_total_cents",
    "first_year_amount_cents",
    "deposit_cents",
    "concession_bps",
    "below_floor",
    "required_tier",
)


class PricingInputError(Exception):
    """输入快照非法（字段级错误，API 层转 422）。"""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name
        self.message = message


@dataclass
class CalcResult:
    """引擎输出：完整快照 + 冗余列。"""

    calc_snapshot: dict[str, Any]
    amounts: dict[str, Any] = field(default_factory=dict)


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PricingInputError(name, f"{name} 必须是整数")
    return value


def _ladder_bps(ladder: list[dict[str, Any]], key: str, value: int) -> int:
    bps = 0
    for step in ladder or []:
        if value >= int(step.get(key, 0)):
            bps = int(step.get("bps", 0))
    return bps


def normalize_rules(rule_set: Optional[dict[str, Any]]) -> dict[str, Any]:
    """把入参规则集补全为完整规则（缺省项取 DEFAULT_RULES）。"""
    rules = dict(DEFAULT_RULES)
    for key, value in (rule_set or {}).items():
        rules[key] = value
    return rules


def normalize_input(input_snapshot: Optional[dict[str, Any]]) -> dict[str, Any]:
    snap = dict(input_snapshot or {})
    if not snap:
        raise PricingInputError("input_snapshot", "输入快照不能为空")

    seat_count = _as_int(snap.get("seat_count"), "seat_count")
    if not 1 <= seat_count <= 500:
        raise PricingInputError("seat_count", "seat_count 必须在 1..500 之间")

    months = _as_int(snap.get("contract_months"), "contract_months")
    if months <= 0 or months % 12 != 0 or months > 60:
        raise PricingInputError("contract_months", "contract_months 必须是 12 的正整数倍且不超过 60")

    plan_code = snap.get("plan_code", "STD")
    if not isinstance(plan_code, str) or not plan_code.strip():
        raise PricingInputError("plan_code", "plan_code 必须是非空字符串")

    addons = snap.get("addon_codes") or []
    if not isinstance(addons, list) or not all(isinstance(a, str) for a in addons):
        raise PricingInputError("addon_codes", "addon_codes 必须是字符串数组")

    return {
        "plan_code": plan_code.strip(),
        "seat_count": seat_count,
        "contract_months": months,
        "addon_codes": sorted(addons),
        "start_date": snap.get("start_date"),
    }


def evaluate(rule_set: Optional[dict[str, Any]], input_snapshot: Optional[dict[str, Any]]) -> CalcResult:
    """计算报价。**这是唯一的金额产生入口，替换为真实引擎即可。**

    :param rule_set: 规则集 JSON（可来自 rule_sets.rules）
    :param input_snapshot: 输入快照（座位数 / 合约月数 / 方案 / 加购）
    :raises PricingInputError: 输入非法
    """
    rules = normalize_rules(rule_set)
    payload = normalize_input(input_snapshot)

    allowed_months = [int(m) for m in rules.get("allowed_months") or [12, 24, 36]]
    if payload["contract_months"] not in allowed_months:
        raise PricingInputError(
            "contract_months",
            f"contract_months 只允许 {allowed_months}，收到 {payload['contract_months']}",
        )

    addon_prices: dict[str, int] = {
        str(k): int(v) for k, v in (rules.get("addon_prices_cents") or {}).items()
    }
    unknown = [c for c in payload["addon_codes"] if c not in addon_prices]
    if unknown:
        raise PricingInputError("addon_codes", f"未知加购项: {', '.join(sorted(unknown))}")

    monthly_base_cents = int(rules["monthly_base_cents"]) * payload["seat_count"]
    monthly_addon_cents = sum(addon_prices[c] for c in payload["addon_codes"])
    monthly_gross_cents = monthly_base_cents + monthly_addon_cents

    contract_months = payload["contract_months"]
    contract_total_gross_cents = monthly_gross_cents * contract_months

    bps = _ladder_bps(rules.get("concession_ladder") or [], "min_months", contract_months)
    bps += _ladder_bps(rules.get("seat_bonus_ladder") or [], "min_seats", payload["seat_count"])
    cap = int(rules.get("concession_cap_bps", 2000))
    concession_bps = max(0, min(bps, cap))

    discount_cents = contract_total_gross_cents * concession_bps // 10000
    contract_total_cents = contract_total_gross_cents - discount_cents

    final_monthly_cents = contract_total_cents // contract_months
    first_year_amount_cents = contract_total_cents * 12 // contract_months
    deposit_cents = final_monthly_cents * int(rules.get("deposit_months", 1))

    floor_monthly_cents = int(rules.get("floor_monthly_cents", 0))
    below_floor = final_monthly_cents < floor_monthly_cents

    required_tier = "T1"
    for step in rules.get("tier_ladder") or []:
        if contract_total_cents >= int(step.get("min_contract_cents", 0)):
            required_tier = str(step.get("tier", "T1"))
            break

    steps = {
        "monthly_base_cents": monthly_base_cents,
        "monthly_addon_cents": monthly_addon_cents,
        "monthly_gross_cents": monthly_gross_cents,
        "contract_total_gross_cents": contract_total_gross_cents,
        "discount_cents": discount_cents,
        "final_monthly_cents": final_monthly_cents,
        "contract_total_cents": contract_total_cents,
        "first_year_amount_cents": first_year_amount_cents,
        "deposit_cents": deposit_cents,
    }

    calc_snapshot: dict[str, Any] = {
        "engine_version": ENGINE_VERSION,
        "rule_set_version": str(rules.get("version", "unversioned")),
        "input": payload,
        "rule_set": rules,
        "steps": steps,
        "concession_bps": concession_bps,
        "floor_monthly_cents": floor_monthly_cents,
        "below_floor": below_floor,
        "required_tier": required_tier,
        "currency": "CNY",
        "unit": "cent",
    }

    amounts = {
        "final_monthly_cents": final_monthly_cents,
        "contract_total_cents": contract_total_cents,
        "first_year_amount_cents": first_year_amount_cents,
        "deposit_cents": deposit_cents,
        "concession_bps": concession_bps,
        "below_floor": below_floor,
        "required_tier": required_tier,
    }

    return CalcResult(calc_snapshot=calc_snapshot, amounts=amounts)
