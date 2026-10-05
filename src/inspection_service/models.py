"""领域模型：抽样规则版本与不可变检验计划。

设计要点
--------
* 规则以「版本」为单位整体生效（草拟→待确认→已下达→已关闭），同一
  (物料类别, 供应商等级) 下同一时刻最多一个生效版本，杜绝"新旧表并行、
  说不清当时用哪张表"。
* 每个版本携带按批量区间分段的规则行；下达前做区间连续性/重叠检测，
  重叠或缺口一律拒绝（边界触碰，如 500/501，视为连续）。
* 收货时按 (类别, 供应商等级, 收货时刻, 批量) 解析出**唯一**一条规则行；
  解析不到或解析出多条都按错误处理，并记录解析轨迹。
* 检验计划生成时对规则版本、规则行、字码、样本量做完整快照（内容寻址
  的规则指纹），完成的计划不随后续新规则/新版本变化。
* 紧急加严与豁免是带批准链的人工覆盖，逐条不可变留痕；规则重叠、批次
  拆分都有确定的状态处理。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import states
from .errors import ConflictError, ImmutablePlanError, NotFoundError, ValidationError
from .sampling_tables import (
    DEFAULT_LEVEL,
    TABLE_ID,
    resolve_code_letter,
    sample_size_for_letter,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: str | datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 规则行与版本
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuleRow:
    """一个批量区间 [lot_min, lot_max]（lot_max=None 表示开放区间）的规则。"""

    lot_min: int
    lot_max: int | None
    base_sample_size: int | None  # None 表示按字码表推导
    inspection_level: str
    severity: str = states.SEVERITY_NORMAL
    accept_number: int | None = None  # Ac：合格判定数
    reject_number: int | None = None  # Re：不合格判定数
    notes: str = ""

    def validate(self) -> None:
        if not isinstance(self.lot_min, int) or self.lot_min < 1:
            raise ValidationError("区间下限必须为不小于 1 的整数", details={"lot_min": self.lot_min})
        if self.lot_max is not None:
            if not isinstance(self.lot_max, int) or self.lot_max < self.lot_min:
                raise ValidationError(
                    "区间上限必须不小于下限",
                    details={"lot_min": self.lot_min, "lot_max": self.lot_max},
                )
        if self.base_sample_size is not None:
            if not isinstance(self.base_sample_size, int) or self.base_sample_size < 1:
                raise ValidationError(
                    "基础样本量必须为正整数；如需按字码表推导请传 null",
                    details={"base_sample_size": self.base_sample_size},
                )
        if self.inspection_level not in ("S-1", "S-2", "S-3", "S-4", "I", "II", "III"):
            raise ValidationError("非法检验水平", details={"inspection_level": self.inspection_level})
        if self.severity not in states.SEVERITIES:
            raise ValidationError("非法严格度", details={"severity": self.severity})
        if self.accept_number is not None and self.accept_number < 0:
            raise ValidationError("合格判定数不能为负")
        if self.reject_number is not None and self.reject_number < 1:
            raise ValidationError("不合格判定数必须不小于 1")
        if self.accept_number is not None and self.reject_number is not None:
            if self.reject_number <= self.accept_number:
                raise ValidationError(
                    "不合格判定数 Re 必须大于合格判定数 Ac",
                    details={"ac": self.accept_number, "re": self.reject_number},
                )

    def contains(self, lot_qty: int) -> bool:
        return lot_qty >= self.lot_min and (self.lot_max is None or lot_qty <= self.lot_max)

    def to_dict(self) -> dict:
        return {
            "lot_min": self.lot_min,
            "lot_max": self.lot_max,
            "base_sample_size": self.base_sample_size,
            "inspection_level": self.inspection_level,
            "severity": self.severity,
            "accept_number": self.accept_number,
            "reject_number": self.reject_number,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "RuleRow":
        return cls(
            lot_min=int(value["lot_min"]),
            lot_max=None if value.get("lot_max") is None else int(value["lot_max"]),
            base_sample_size=None if value.get("base_sample_size") is None else int(value["base_sample_size"]),
            inspection_level=value.get("inspection_level", DEFAULT_LEVEL),
            severity=value.get("severity", states.SEVERITY_NORMAL),
            accept_number=value.get("accept_number"),
            reject_number=value.get("reject_number"),
            notes=value.get("notes", ""),
        )


def validate_rows_cover(rows: list[RuleRow]) -> list[dict]:
    """检查区间两两不重叠且首尾相连、仅最后一行可开放。

    返回按下限排序后的行；任何重叠/缺口/重复开放区间都抛 ConflictError。
    """
    if not rows:
        raise ValidationError("规则版本至少需要一条批量区间行")
    ordered = sorted(rows, key=lambda r: r.lot_min)
    problems: list[dict] = []
    open_rows = [r for r in ordered if r.lot_max is None]
    if len(open_rows) > 1:
        problems.append({"type": "multiple_open_ranges", "rows": [r.to_dict() for r in open_rows]})
    for prev, curr in zip(ordered, ordered[1:]):
        if prev.lot_max is None:
            problems.append({"type": "open_range_not_last", "row": prev.to_dict(), "next": curr.to_dict()})
            continue
        if curr.lot_min <= prev.lot_max:
            problems.append({
                "type": "overlap",
                "range_a": [prev.lot_min, prev.lot_max],
                "range_b": [curr.lot_min, curr.lot_max],
            })
        elif curr.lot_min != prev.lot_max + 1:
            problems.append({
                "type": "gap",
                "between": [prev.lot_max, curr.lot_min],
            })
    if ordered[0].lot_min != 1:
        problems.append({"type": "gap", "between": [1, ordered[0].lot_min]})
    if problems:
        raise ConflictError(
            f"批量区间存在 {len(problems)} 处重叠或缺口，规则版本不得下达",
            details={"problems": problems},
        )
    return ordered


@dataclass(frozen=True)
class DefectClass:
    """缺陷分级：代码（致命/严重/轻微）及该级别的合格/不合格判定数 Ac/Re。"""

    code: str
    name: str
    accept_number: int
    reject_number: int

    def validate(self) -> None:
        if not self.code.strip():
            raise ValidationError("缺陷分级代码不能为空")
        if self.accept_number < 0:
            raise ValidationError(f"{self.code} 的合格判定数不能为负")
        if self.reject_number <= self.accept_number:
            raise ValidationError(
                f"{self.code} 的不合格判定数 Re 必须大于 Ac",
                details={"ac": self.accept_number, "re": self.reject_number},
            )

    def verdict_for(self, count: int) -> str:
        if count >= self.reject_number:
            return "不通过"
        if count <= self.accept_number:
            return "通过"
        return "待判定"

    def to_dict(self) -> dict:
        return {
            "code": self.code, "name": self.name,
            "accept_number": self.accept_number, "reject_number": self.reject_number,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "DefectClass":
        return cls(
            code=str(value["code"]),
            name=str(value.get("name", value["code"])),
            accept_number=int(value["accept_number"]),
            reject_number=int(value["reject_number"]),
        )


DEFAULT_DEFECT_CLASSES = [
    {"code": "CR", "name": "致命", "accept_number": 0, "reject_number": 1},
    {"code": "MA", "name": "严重", "accept_number": 1, "reject_number": 2},
    {"code": "MI", "name": "轻微", "accept_number": 2, "reject_number": 3},
]


@dataclass
class RuleVersion:
    version_id: str
    material_category: str
    supplier_grade: str
    rows: list[RuleRow]
    defect_classes: list[DefectClass] = field(default_factory=list)
    status: str = states.RULE_DRAFT
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    issued_at: datetime | None = None
    closed_at: datetime | None = None
    change_summary: str = ""
    supersedes_version_id: str | None = None

    def __post_init__(self) -> None:
        if not self.defect_classes:
            self.defect_classes = [DefectClass.from_dict(d) for d in DEFAULT_DEFECT_CLASSES]

    def fingerprint(self) -> str:
        """对版本的业务内容做 SHA-256，供计划快照引用与完整性校验。"""
        payload = {
            "material_category": self.material_category,
            "supplier_grade": self.supplier_grade,
            "rows": [r.to_dict() for r in sorted(self.rows, key=lambda r: r.lot_min)],
            "defect_classes": [d.to_dict() for d in sorted(self.defect_classes, key=lambda d: d.code)],
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(body).hexdigest()

    def effective_on(self, moment: datetime) -> bool:
        if self.status != states.RULE_ISSUED:
            return False
        return self.issued_at is not None and self.issued_at <= moment and (
            self.closed_at is None or self.closed_at > moment
        )

    def row_for_lot(self, lot_qty: int) -> RuleRow:
        matches = [r for r in self.rows if r.contains(lot_qty)]
        if len(matches) > 1:
            # 理论上已被 validate_rows_cover 拦截，这里是运行时双保险
            raise ConflictError(
                "规则区间重叠，无法确定唯一规则行",
                details={"matches": [r.to_dict() for r in matches]},
            )
        if not matches:
            raise NotFoundError(
                "当前批量没有匹配的抽样规则行",
                details={"lot_qty": lot_qty, "category": self.material_category, "grade": self.supplier_grade},
            )
        return matches[0]

    def to_dict(self) -> dict:
        return {
            "version_id": self.version_id,
            "material_category": self.material_category,
            "supplier_grade": self.supplier_grade,
            "status": self.status,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
            "issued_at": iso(self.issued_at) if self.issued_at else None,
            "closed_at": iso(self.closed_at) if self.closed_at else None,
            "change_summary": self.change_summary,
            "supersedes_version_id": self.supersedes_version_id,
            "fingerprint": self.fingerprint(),
            "rows": [r.to_dict() for r in sorted(self.rows, key=lambda r: r.lot_min)],
            "defect_classes": [d.to_dict() for d in sorted(self.defect_classes, key=lambda d: d.code)],
        }


# ---------------------------------------------------------------------------
# 人工覆盖（紧急加严 / 豁免批准）
# ---------------------------------------------------------------------------

OVERRIDE_TIGHTEN = "emergency_tightening"
OVERRIDE_EXEMPT = "exemption"
OVERRIDE_KINDS = (OVERRIDE_TIGHTEN, OVERRIDE_EXEMPT)

# 覆盖对样本量的确定影响，用于"解释样本量计算"
KIND_EFFECT = {
    OVERRIDE_TIGHTEN: "紧急加严：按倍数上调样本量",
    OVERRIDE_EXEMPT: "豁免批准：样本量降为 0（免抽）",
}


@dataclass(frozen=True)
class Override:
    override_id: str
    kind: str
    requested_by: str
    reason: str
    created_at: datetime
    approver: str | None = None
    approved_at: datetime | None = None
    approver_comment: str = ""
    # 加严参数
    multiply: float = 1.0
    add: int = 0
    # 运行期状态
    revoked_by: str | None = None
    revoked_at: datetime | None = None
    revoke_reason: str = ""

    @property
    def approved(self) -> bool:
        return self.approver is not None and self.revoked_at is None

    def effective_sample_size(self, base: int) -> int:
        if not self.approved:
            return base
        if self.kind == OVERRIDE_EXEMPT:
            return 0
        if self.kind == OVERRIDE_TIGHTEN:
            import math

            return min(int(math.ceil(base * self.multiply)) + self.add, _LOT_CAP)
        return base

    def to_dict(self) -> dict:
        return {
            "override_id": self.override_id,
            "kind": self.kind,
            "effect": KIND_EFFECT.get(self.kind, ""),
            "requested_by": self.requested_by,
            "reason": self.reason,
            "created_at": iso(self.created_at),
            "approver": self.approver,
            "approved_at": iso(self.approved_at) if self.approved_at else None,
            "approver_comment": self.approver_comment,
            "multiply": self.multiply,
            "add": self.add,
            "status": "已批准" if self.approved else ("已撤销" if self.revoked_at else "待批准"),
            "revoked_by": self.revoked_by,
            "revoked_at": iso(self.revoked_at) if self.revoked_at else None,
            "revoke_reason": self.revoke_reason,
        }


_LOT_CAP = 10**9


# ---------------------------------------------------------------------------
# 检验计划（不可变快照）
# ---------------------------------------------------------------------------

PLAN_STATUS_FLOW = {
    states.PLAN_PENDING: {states.PLAN_IN_PROGRESS},
    states.PLAN_IN_PROGRESS: {states.PLAN_CLOSED},
    states.PLAN_CLOSED: set(),
}


@dataclass
class InspectionPlan:
    plan_id: str
    batch_no: str
    material_category: str
    supplier_grade: str
    supplier_id: str
    lot_qty: int
    status: str = states.PLAN_PENDING
    # 规则快照
    rule_version_id: str = ""
    rule_version_fingerprint: str = ""
    rule_row_snapshot: dict = field(default_factory=dict)
    defect_classes_snapshot: list[dict] = field(default_factory=list)
    sampling_table_id: str = TABLE_ID
    # 计算轨迹（解释样本量从哪来）
    code_letter: str = ""
    table_sample_size: int = 0
    base_sample_size: int = 0
    final_sample_size: int = 0
    resolution_trace: list[dict] = field(default_factory=list)
    recompute_log: list[dict] = field(default_factory=list)
    overrides: list[Override] = field(default_factory=list)
    # 生命周期
    created_by: str = ""
    created_at: datetime = field(default_factory=utcnow)
    started_at: datetime | None = None
    closed_at: datetime | None = None
    close_reason: str = ""
    # 结果（履行完成后登记，同样不可变）
    inspected_count: int | None = None
    defect_count: int | None = None
    defect_counts_by_class: dict = field(default_factory=dict)
    class_verdicts: list[dict] = field(default_factory=list)
    defect_grades_found: list[str] = field(default_factory=list)
    disposition: str = ""
    # 批次拆分血缘
    parent_plan_id: str | None = None
    child_plan_ids: list[str] = field(default_factory=list)

    # -- 不变量保护 --------------------------------------------------------

    def _require_not_closed(self, action: str) -> None:
        if self.status == states.PLAN_CLOSED:
            raise ImmutablePlanError(
                f"检验计划已关闭（{self.close_reason}），{action} 被禁止：完成的计划不随新规则变化",
                details={"plan_id": self.plan_id, "closed_at": iso(self.closed_at) if self.closed_at else None},
            )

    def _require_status(self, allowed: set[str], action: str) -> None:
        if self.status not in allowed:
            raise ImmutablePlanError(
                f"当前状态「{self.status}」不允许 {action}",
                details={"plan_id": self.plan_id, "status": self.status},
            )

    # -- 覆盖管理 ----------------------------------------------------------

    def _require_override_window(self, action: str) -> None:
        """覆盖只允许在「待确认」阶段（检验开始前）操作；开始后样本量即固定。"""
        if self.status == states.PLAN_CLOSED:
            raise ImmutablePlanError(
                f"检验计划已关闭（{self.close_reason}），{action} 被禁止：完成的计划不随新规则变化",
                details={"plan_id": self.plan_id, "closed_at": iso(self.closed_at) if self.closed_at else None},
            )
        if self.status != states.PLAN_PENDING:
            raise ImmutablePlanError(
                f"检验已开始（状态「{self.status}」），样本量已固定，{action} 被禁止；如须加严请对后续批次申请",
                details={"plan_id": self.plan_id, "status": self.status},
            )

    def attach_override(self, override: Override) -> None:
        """登记覆盖申请。仅待确认阶段允许；未批准不影响样本量。"""
        self._require_override_window("登记人工覆盖")
        if any(o.override_id == override.override_id for o in self.overrides):
            raise ConflictError("覆盖记录重复", details={"override_id": override.override_id})
        self.overrides.append(override)
        self._recompute()

    def approve_override(self, override_id: str, approver: str, comment: str, moment: datetime) -> Override:
        self._require_override_window("批准人工覆盖")
        target = self._find_override(override_id)
        if target.approver is not None:
            raise ConflictError("覆盖已批准，不可重复批准", details={"override_id": override_id})
        if target.revoked_at is not None:
            raise ConflictError("覆盖已撤销，不可批准", details={"override_id": override_id})
        # frozen=True → 用 dataclasses.replace 生成新版本再替换
        from dataclasses import replace

        approved = replace(target, approver=approver, approved_at=moment, approver_comment=comment)
        self.overrides = [o if o.override_id != override_id else approved for o in self.overrides]
        self._recompute()
        return approved

    def revoke_override(self, override_id: str, operator: str, reason: str, moment: datetime) -> Override:
        """撤销覆盖：仅检验开始前可撤销，撤销本身留痕；开始后任何覆盖状态都冻结。"""
        self._require_override_window("撤销人工覆盖")
        target = self._find_override(override_id)
        if target.revoked_at is not None:
            raise ConflictError("覆盖已撤销", details={"override_id": override_id})
        from dataclasses import replace

        revoked = replace(target, revoked_by=operator, revoked_at=moment, revoke_reason=reason)
        self.overrides = [o if o.override_id != override_id else revoked for o in self.overrides]
        self._recompute()
        return revoked

    def _find_override(self, override_id: str) -> Override:
        for o in self.overrides:
            if o.override_id == override_id:
                return o
        raise NotFoundError("覆盖不存在", details={"override_id": override_id})

    def approved_overrides(self) -> list[Override]:
        return [o for o in self.overrides if o.approved]

    # -- 样本量计算（可解释） ----------------------------------------------

    def _recompute(self) -> None:
        """依据规则快照与当前已批准覆盖重算最终样本量，并追加计算说明。"""
        size = self.base_sample_size
        steps: list[dict] = []
        steps.append({
            "step": "rule_snapshot",
            "detail": "取计划生成时冻结的规则行基础样本量",
            "sample_size": size,
        })
        active = self.approved_overrides()
        # 豁免优先级最高：任一已批准豁免 → 免抽
        exemptions = [o for o in active if o.kind == OVERRIDE_EXEMPT]
        tightenings = [o for o in active if o.kind == OVERRIDE_TIGHTEN]
        if exemptions:
            size = 0
            for o in exemptions:
                steps.append({
                    "step": OVERRIDE_EXEMPT,
                    "override_id": o.override_id,
                    "requested_by": o.requested_by,
                    "approver": o.approver,
                    "detail": f"豁免批准（{o.reason}），样本量降为 0",
                    "sample_size": 0,
                })
        else:
            for o in tightenings:
                before = size
                size = o.effective_sample_size(size)
                steps.append({
                    "step": OVERRIDE_TIGHTEN,
                    "override_id": o.override_id,
                    "requested_by": o.requested_by,
                    "approver": o.approver,
                    "multiply": o.multiply,
                    "add": o.add,
                    "detail": f"紧急加严（{o.reason}）：ceil({before}×{o.multiply})+{o.add}={size}",
                    "sample_size": size,
                })
        if size > self.lot_qty:
            steps.append({"step": "cap_to_lot", "detail": f"样本量不超过批量 {self.lot_qty}", "sample_size": self.lot_qty})
            size = self.lot_qty
        self.final_sample_size = size
        self.recompute_log.append({"at": iso(utcnow()), "steps": steps})

    def explanation(self) -> dict:
        """API 用：完整解释样本量计算与每次人工覆盖。"""
        return {
            "plan_id": self.plan_id,
            "batch_no": self.batch_no,
            "lot_qty": self.lot_qty,
            "rule_resolution": self.resolution_trace_of_resolution(),
            "sample_size_calculation": {
                "code_letter": self.code_letter,
                "sampling_table": self.sampling_table_id,
                "table_sample_size": self.table_sample_size,
                "rule_base_sample_size": self.base_sample_size,
                "final_sample_size": self.final_sample_size,
                "recompute_history": self.recompute_log,
            },
            "overrides": [self._override_explanation(o) for o in self.overrides],
            "rule_version_snapshot": {
                "version_id": self.rule_version_id,
                "fingerprint": self.rule_version_fingerprint,
                "row": self.rule_row_snapshot,
                "defect_classes": self.defect_classes_snapshot,
            },
            "immutability": {
                "status": self.status,
                "frozen_at_creation": True,
                "note": "规则版本与规则行在计划生成时快照；之后规则更新、版本关闭均不改变本计划",
            },
        }

    def resolution_trace_of_resolution(self) -> list[dict]:
        return [t for t in self.resolution_trace if t.get("step", "").startswith("resolve:")]

    def _override_explanation(self, o: Override) -> dict:
        data = o.to_dict()
        data["sample_size_after_apply"] = o.effective_sample_size(self.base_sample_size) if o.approved else None
        data["applied_to_final"] = o.approved
        return data

    # -- 生命周期 ----------------------------------------------------------

    def start(self, operator: str, moment: datetime) -> None:
        self._require_status({states.PLAN_PENDING}, "开始检验")
        self.status = states.PLAN_IN_PROGRESS
        self.started_at = moment

    def record_result(
        self,
        operator: str,
        inspected_count: int,
        defect_count: int | None,
        defect_grades: list[str],
        disposition: str,
        moment: datetime,
        defect_counts_by_class: dict | None = None,
    ) -> None:
        """登记检验结果并关闭计划。关闭后计划完全不可变。

        优先使用 ``defect_counts_by_class``（{分级代码: 缺陷数}），按计划生成时
        冻结的缺陷分级 Ac/Re 逐类判定；``defect_count`` 为空时取分级合计。
        """
        self._require_status({states.PLAN_IN_PROGRESS}, "登记检验结果")
        expected = self.final_sample_size
        if inspected_count != expected:
            raise ValidationError(
                "实抽数量必须等于计划样本量；如需偏差请在开始前通过人工覆盖调整",
                details={"expected": expected, "actual": inspected_count},
            )
        verdicts: list[dict] = []
        if defect_counts_by_class is not None:
            known = {d["code"]: DefectClass.from_dict(d) for d in self.defect_classes_snapshot}
            unknown = [c for c in defect_counts_by_class if c not in known]
            if unknown:
                raise ValidationError(
                    "缺陷分级不属于该规则版本",
                    details={"unknown": unknown, "allowed": list(known)},
                )
            for code, cls in known.items():
                count = int(defect_counts_by_class.get(code, 0))
                if count < 0:
                    raise ValidationError("缺陷数不能为负")
                verdicts.append({
                    "code": code, "name": cls.name, "count": count,
                    "ac": cls.accept_number, "re": cls.reject_number,
                    "verdict": cls.verdict_for(count),
                })
            total = sum(int(v) for v in defect_counts_by_class.values())
            if defect_count is not None and defect_count != total:
                raise ValidationError(
                    "缺陷总数与分级明细合计不一致",
                    details={"defect_count": defect_count, "sum_by_class": total},
                )
            defect_count = total
        if defect_count is None or not 0 <= defect_count <= inspected_count:
            raise ValidationError("缺陷数必须介于 0 与实抽数量之间")
        if disposition not in ("合格入库", "不合格退货", "让步接收", "全检"):
            raise ValidationError("非法处置结论", details={"disposition": disposition})
        rejected = [v for v in verdicts if v["verdict"] == "不通过"]
        if rejected and disposition == "合格入库":
            raise ConflictError(
                "存在分级判定不通过，禁止直接合格入库；请选择退货/让步接收/全检",
                details={"rejected_classes": rejected},
            )
        if not rejected and defect_counts_by_class is not None and disposition == "不合格退货":
            raise ConflictError("各分级均判定通过，不能按不合格退货处理")
        self.inspected_count = inspected_count
        self.defect_count = defect_count
        self.defect_counts_by_class = defect_counts_by_class or {}
        self.class_verdicts = verdicts
        self.defect_grades_found = list(defect_grades)
        self.disposition = disposition
        self.status = states.PLAN_CLOSED
        self.closed_at = moment
        self.close_reason = states.CLOSE_COMPLETED

    def close_due_to_split(self, operator: str, moment: datetime) -> None:
        """批次拆分：父计划以「批次拆分」关闭并冻结，子批量另行建计划。"""
        self._require_status({states.PLAN_PENDING, states.PLAN_IN_PROGRESS}, "批次拆分关闭")
        self.status = states.PLAN_CLOSED
        self.closed_at = moment
        self.close_reason = states.CLOSE_SPLIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "batch_no": self.batch_no,
            "material_category": self.material_category,
            "supplier_grade": self.supplier_grade,
            "supplier_id": self.supplier_id,
            "lot_qty": self.lot_qty,
            "status": self.status,
            "code_letter": self.code_letter,
            "base_sample_size": self.base_sample_size,
            "final_sample_size": self.final_sample_size,
            "inspection_level": self.rule_row_snapshot.get("inspection_level"),
            "severity": self.rule_row_snapshot.get("severity"),
            "accept_number": self.rule_row_snapshot.get("accept_number"),
            "reject_number": self.rule_row_snapshot.get("reject_number"),
            "rule_version_id": self.rule_version_id,
            "rule_version_fingerprint": self.rule_version_fingerprint,
            "rule_row_snapshot": self.rule_row_snapshot,
            "defect_classes_snapshot": self.defect_classes_snapshot,
            "sampling_table_id": self.sampling_table_id,
            "overrides": [o.to_dict() for o in self.overrides],
            "resolution_trace": self.resolution_trace,
            "recompute_log": self.recompute_log,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at) if self.started_at else None,
            "closed_at": iso(self.closed_at) if self.closed_at else None,
            "close_reason": self.close_reason,
            "inspected_count": self.inspected_count,
            "defect_count": self.defect_count,
            "defect_counts_by_class": self.defect_counts_by_class,
            "class_verdicts": self.class_verdicts,
            "defect_grades_found": self.defect_grades_found,
            "disposition": self.disposition,
            "parent_plan_id": self.parent_plan_id,
            "child_plan_ids": self.child_plan_ids,
        }


# ---------------------------------------------------------------------------
# 计划生成
# ---------------------------------------------------------------------------

def build_plan(
    *,
    plan_id: str,
    batch_no: str,
    supplier_id: str,
    lot_qty: int,
    version: RuleVersion,
    moment: datetime,
    created_by: str,
    candidates_scanned: list[RuleVersion],
    parent_plan_id: str | None = None,
) -> InspectionPlan:
    """按收货时刻解析唯一规则版本/规则行，并生成冻结快照的检验计划。"""
    if not isinstance(lot_qty, int) or lot_qty < 1:
        raise ValidationError("批量必须为正整数", details={"lot_qty": lot_qty})
    if not version.effective_on(moment):
        raise ConflictError(
            "所选规则版本在收货时刻未生效，不能生成计划",
            details={"version_id": version.version_id, "status": version.status, "at": iso(moment)},
        )
    # 唯一性双保险：同一键上若存在第二个同时生效版本，拒绝收货
    also_effective = [
        v.version_id
        for v in candidates_scanned
        if v.version_id != version.version_id and v.effective_on(moment)
    ]
    if also_effective:
        raise ConflictError(
            "存在多个同时生效的规则版本，无法解析唯一版本",
            details={"chosen": version.version_id, "also_effective": also_effective},
        )

    row = version.row_for_lot(lot_qty)
    letter = resolve_code_letter(lot_qty, row.inspection_level)
    table_size = sample_size_for_letter(letter)
    base = row.base_sample_size if row.base_sample_size is not None else table_size
    base = min(base, lot_qty)

    trace = [
        {
            "step": "resolve:candidates",
            "at": iso(moment),
            "detail": f"按键 (物料类别={version.material_category}, 供应商等级={version.supplier_grade}) 扫描到 {len(candidates_scanned)} 个版本",
            "candidate_version_ids": [v.version_id for v in candidates_scanned],
            "candidate_statuses": {v.version_id: v.status for v in candidates_scanned},
        },
        {
            "step": "resolve:effective_window",
            "at": iso(moment),
            "detail": "过滤 status=已下达 且 issued_at <= 收货时刻 < closed_at",
            "selected": version.version_id,
            "issued_at": iso(version.issued_at) if version.issued_at else None,
            "closed_at": iso(version.closed_at) if version.closed_at else None,
        },
        {
            "step": "resolve:lot_row",
            "at": iso(moment),
            "detail": f"批量 {lot_qty} 落入区间 [{row.lot_min}, {row.lot_max if row.lot_max is not None else '∞'}]",
            "row": row.to_dict(),
        },
        {
            "step": "resolve:code_letter",
            "at": iso(moment),
            "detail": f"按 {TABLE_ID}，检验水平 {row.inspection_level} → 字码 {letter} → 表定样本量 {table_size}",
            "code_letter": letter,
            "table_sample_size": table_size,
        },
        {
            "step": "resolve:freeze",
            "at": iso(moment),
            "detail": "规则版本指纹与规则行写入计划快照，此后不可变",
            "fingerprint": version.fingerprint(),
        },
    ]

    plan = InspectionPlan(
        plan_id=plan_id,
        batch_no=batch_no,
        material_category=version.material_category,
        supplier_grade=version.supplier_grade,
        supplier_id=supplier_id,
        lot_qty=lot_qty,
        rule_version_id=version.version_id,
        rule_version_fingerprint=version.fingerprint(),
        rule_row_snapshot=row.to_dict(),
        defect_classes_snapshot=[d.to_dict() for d in version.defect_classes],
        code_letter=letter,
        table_sample_size=table_size,
        base_sample_size=base,
        final_sample_size=base,
        resolution_trace=trace,
        created_by=created_by,
        created_at=moment,
        parent_plan_id=parent_plan_id,
    )
    return plan
