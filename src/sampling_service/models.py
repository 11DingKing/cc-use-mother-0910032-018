"""抽样检验领域模型。

角色与状态取值与 domain/contract.json 对齐：
- 角色：采购计划员 / 供应商 / 质量工程师 / 仓储管理员
- 规则版本与检验计划状态：草拟 / 待确认 / 已下达 / 履行中 / 已关闭
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Role(str, Enum):
    PLANNER = "采购计划员"
    SUPPLIER = "供应商"
    QUALITY_ENGINEER = "质量工程师"
    WAREHOUSE_KEEPER = "仓储管理员"


class VersionStatus(str, Enum):
    DRAFT = "草拟"
    PENDING = "待确认"
    RELEASED = "已下达"
    CLOSED = "已关闭"


class PlanStatus(str, Enum):
    RELEASED = "已下达"
    IN_PROGRESS = "履行中"
    CLOSED = "已关闭"


class TighteningStatus(str, Enum):
    ACTIVE = "生效中"
    REVOKED = "已撤销"


class ExemptionStatus(str, Enum):
    PENDING = "待批准"
    APPROVED = "已批准"
    REJECTED = "已拒绝"


class ExemptionMode(str, Enum):
    SKIP = "skip"      # 免检：样本量降为 0
    REDUCE = "reduce"  # 减量：样本量按比例缩减


@dataclass
class SamplingRule:
    """单条抽样规则：按物料类别 + 供应商等级 + 批量区间生效。"""

    rule_id: str
    material_category: str
    supplier_level: str
    lot_min: int
    lot_max: int | None  # None 表示上不封顶
    sample_ratio: float
    min_sample: int
    max_sample: int | None
    defect_criteria: dict[str, Any]  # 缺陷分级判定标准，如 {"critical": {"ac": 0}}

    def matches(self, category: str, level: str, quantity: int) -> bool:
        if self.material_category != category or self.supplier_level != level:
            return False
        if quantity < self.lot_min:
            return False
        return self.lot_max is None or quantity <= self.lot_max

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SamplingRule":
        return cls(**data)


@dataclass
class RuleVersion:
    """抽样规则版本。同一时刻最多一个版本处于已下达。"""

    version_id: str
    title: str
    status: str
    rules: list[SamplingRule]
    created_by: str
    created_at: str
    effective_from: str | None = None
    superseded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuleVersion":
        data = dict(data)
        data["rules"] = [SamplingRule.from_dict(r) for r in data["rules"]]
        return cls(**data)


@dataclass
class OverrideRecord:
    """人工覆盖审计记录。"""

    override_id: str
    old_sample_size: int
    new_sample_size: int
    reason: str
    actor: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OverrideRecord":
        return cls(**data)


@dataclass
class InspectionPlan:
    """检验计划：收货时生成，冻结规则快照与样本量计算过程，完成后不可变。"""

    plan_id: str
    batch_id: str
    material_category: str
    supplier_level: str
    lot_size: int
    rule_version_id: str
    rule_id: str
    rule_snapshot: dict[str, Any]
    resolved_at: str
    sample_size: int
    base_sample_size: int
    steps: list[dict[str, Any]]
    defect_criteria: dict[str, Any]
    tightening_ids: list[str]
    exemption_ids: list[str]
    overrides: list[OverrideRecord]
    events: list[dict[str, Any]]
    status: str
    created_at: str
    parent_plan_id: str | None = None
    closed_at: str | None = None
    close_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "InspectionPlan":
        data = dict(data)
        data["overrides"] = [OverrideRecord.from_dict(o) for o in data["overrides"]]
        return cls(**data)


@dataclass
class Batch:
    """收货批次。"""

    batch_id: str
    material_category: str
    supplier_level: str
    quantity: int
    received_at: str
    plan_id: str
    parent_batch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Batch":
        return cls(**data)


@dataclass
class Tightening:
    """紧急加严指令：对生效时点之后新建的检验计划提高样本量。"""

    tightening_id: str
    material_category: str
    supplier_level: str | None  # None 表示该类别下所有供应商等级
    multiplier: float
    reason: str
    created_by: str
    effective_from: str
    expires_at: str | None
    status: str
    revoked_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tightening":
        return cls(**data)


@dataclass
class Exemption:
    """抽样豁免：经质量工程师批准后对匹配范围免检或减量。"""

    exemption_id: str
    material_category: str
    supplier_level: str | None
    batch_id: str | None  # 指定后仅对单个批次生效
    mode: str
    reduce_factor: float | None
    reason: str
    requested_by: str
    status: str
    created_at: str
    decided_by: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Exemption":
        return cls(**data)
