"""生命周期状态词汇。

与 ``domain/contract.json`` 中五个状态对齐：
规则版本走 草拟→待确认→已下达→已关闭；检验计划走 待确认→履行中→已关闭。
"""
from __future__ import annotations

# 规则版本状态
RULE_DRAFT = "草拟"
RULE_PENDING = "待确认"
RULE_ISSUED = "已下达"
RULE_CLOSED = "已关闭"

# 检验计划状态
PLAN_PENDING = "待确认"
PLAN_IN_PROGRESS = "履行中"
PLAN_CLOSED = "已关闭"

# 计划关闭原因
CLOSE_COMPLETED = "检验完成"
CLOSE_SPLIT = "批次拆分"

# 严格度
SEVERITY_NORMAL = "normal"
SEVERITY_TIGHTENED = "tightened"
SEVERITY_REDUCED = "reduced"
SEVERITIES = (SEVERITY_NORMAL, SEVERITY_TIGHTENED, SEVERITY_REDUCED)

SEVERITY_LABELS = {
    SEVERITY_NORMAL: "正常",
    SEVERITY_TIGHTENED: "加严",
    SEVERITY_REDUCED: "放宽",
}

# 角色（与契约 actors 对齐，附英文别名）
ROLE_QUALITY_ENGINEER = "质量工程师"
ROLE_WAREHOUSE = "仓储管理员"
ROLE_PLANNER = "采购计划员"
ROLE_SUPPLIER = "供应商"

ROLE_ALIASES = {
    "quality_engineer": ROLE_QUALITY_ENGINEER,
    "qe": ROLE_QUALITY_ENGINEER,
    "warehouse": ROLE_WAREHOUSE,
    "planner": ROLE_PLANNER,
    "supplier": ROLE_SUPPLIER,
}


def normalize_role(value: str | None) -> str | None:
    if value is None:
        return None
    return ROLE_ALIASES.get(value.strip().lower(), value.strip())
