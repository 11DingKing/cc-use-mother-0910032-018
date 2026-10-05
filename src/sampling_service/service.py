"""抽样规则版本与检验计划应用服务。

核心业务约定：
1. 抽样规则时态：版本按 [effective_from, superseded_at) 生效，收货时刻解析唯一版本；
   检验计划冻结规则快照，新规则版本发布后，已生成/已完成的计划不受影响。
2. 区间重叠检测：同一版本内相同（物料类别, 供应商等级）的批量区间不得重叠，
   提交与下达时强制校验，保证任何批量最多命中一条规则。
3. 样本量计算顺序：基础比例 → 最小/最大约束 → 不超过批量 → 紧急加严（取最严）
   → 豁免批准（取最具体）→ 人工覆盖（最终裁定，全部留痕）。
4. 批次拆分：子批次继承父计划解析结果，父计划样本量按子批数量比例分配（最大余数法），
   防止通过拆分批量落入更低抽样区间。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from .models import (
    Batch,
    Exemption,
    ExemptionMode,
    ExemptionStatus,
    InspectionPlan,
    OverrideRecord,
    PlanStatus,
    Role,
    RuleVersion,
    SamplingRule,
    Tightening,
    TighteningStatus,
    VersionStatus,
)
from .store import JsonStore


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_ts_field(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{field} 必须是 ISO 8601 时间字符串", code="TIMESTAMP_INVALID")
    try:
        return _parse_ts(value)
    except ValueError:
        raise ValidationError(f"{field} 不是合法的时间格式：{value!r}", code="TIMESTAMP_INVALID")


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} 必须是非空字符串", code="FIELD_REQUIRED")
    return value.strip()


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field} 必须是整数", code="FIELD_INVALID")
    if int(value) != value:
        raise ValidationError(f"{field} 必须是整数", code="FIELD_INVALID")
    return int(value)


def allocate_samples(total: int, quantities: list[int]) -> list[int]:
    """按子批数量比例分配样本总量（最大余数法），单项不超过子批数量。"""
    if total <= 0:
        return [0] * len(quantities)
    sum_q = sum(quantities)
    raw = [total * q / sum_q for q in quantities]
    alloc = [min(q, math.floor(r)) for q, r in zip(quantities, raw)]
    leftover = total - sum(alloc)
    while leftover > 0:
        candidates = [i for i, q in enumerate(quantities) if alloc[i] < q]
        if not candidates:  # 子批总量等于父批总量，而父样本量不超过父批总量，不会走到这里
            raise ConflictError("子批次容量不足，无法分配样本", code="SPLIT_CAPACITY")
        idx = max(candidates, key=lambda j: (raw[j] - math.floor(raw[j]), quantities[j]))
        alloc[idx] += 1
        leftover -= 1
    return alloc


class SamplingService:
    def __init__(self, store: JsonStore | None = None, clock: Callable[[], datetime] | None = None):
        self.store = store or JsonStore()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 基础工具

    def _now(self) -> str:
        return self.clock().isoformat()

    @staticmethod
    def _require_role(role: str, allowed: set[Role]) -> Role:
        try:
            parsed = Role(role)
        except ValueError:
            raise ForbiddenError(f"未知角色：{role!r}", code="UNKNOWN_ROLE")
        if parsed not in allowed:
            names = "、".join(r.value for r in sorted(allowed, key=lambda r: r.value))
            raise ForbiddenError(f"角色 {parsed.value} 无权执行该操作，需要：{names}", code="ROLE_NOT_ALLOWED")
        return parsed

    def _get_version(self, version_id: str) -> RuleVersion:
        raw = self.store.data["versions"].get(version_id)
        if raw is None:
            raise NotFoundError(f"规则版本不存在：{version_id}", code="VERSION_NOT_FOUND")
        return RuleVersion.from_dict(raw)

    def _save_version(self, version: RuleVersion) -> None:
        self.store.data["versions"][version.version_id] = version.to_dict()

    def _get_plan(self, plan_id: str) -> InspectionPlan:
        raw = self.store.data["plans"].get(plan_id)
        if raw is None:
            raise NotFoundError(f"检验计划不存在：{plan_id}", code="PLAN_NOT_FOUND")
        return InspectionPlan.from_dict(raw)

    def _save_plan(self, plan: InspectionPlan) -> None:
        self.store.data["plans"][plan.plan_id] = plan.to_dict()

    # ------------------------------------------------------------------ 规则版本

    def _parse_rule(self, version_id: str, index: int, raw: dict[str, Any]) -> SamplingRule:
        if not isinstance(raw, dict):
            raise ValidationError("规则必须是对象", code="RULE_INVALID")
        try:
            rule = SamplingRule(
                rule_id=f"{version_id}-R{index + 1:02d}",
                material_category=_require_text(raw.get("material_category"), "material_category"),
                supplier_level=_require_text(raw.get("supplier_level"), "supplier_level"),
                lot_min=_as_int(raw.get("lot_min"), "lot_min"),
                lot_max=None if raw.get("lot_max") is None else _as_int(raw.get("lot_max"), "lot_max"),
                sample_ratio=float(raw.get("sample_ratio")),
                min_sample=_as_int(raw.get("min_sample", 1), "min_sample"),
                max_sample=None if raw.get("max_sample") is None else _as_int(raw.get("max_sample"), "max_sample"),
                defect_criteria=raw.get("defect_criteria"),
            )
        except (TypeError, ValueError):
            raise ValidationError("规则字段类型错误", code="RULE_FIELD_INVALID")
        if rule.lot_min < 1:
            raise ValidationError("lot_min 必须 ≥ 1", code="RULE_LOT_RANGE_INVALID")
        if rule.lot_max is not None and rule.lot_max < rule.lot_min:
            raise ValidationError("lot_max 不能小于 lot_min", code="RULE_LOT_RANGE_INVALID")
        if not (0 < rule.sample_ratio <= 1):
            raise ValidationError("sample_ratio 必须在 (0, 1] 区间", code="RULE_RATIO_INVALID")
        if rule.min_sample < 1:
            raise ValidationError("min_sample 必须 ≥ 1", code="RULE_SAMPLE_INVALID")
        if rule.max_sample is not None and rule.max_sample < rule.min_sample:
            raise ValidationError("max_sample 不能小于 min_sample", code="RULE_SAMPLE_INVALID")
        criteria = rule.defect_criteria
        if not isinstance(criteria, dict) or not criteria:
            raise ValidationError("defect_criteria 必须是非空对象", code="RULE_CRITERIA_INVALID")
        for name, item in criteria.items():
            ac = item.get("ac") if isinstance(item, dict) else None
            if not isinstance(ac, int) or isinstance(ac, bool) or ac < 0:
                raise ValidationError(f"缺陷等级 {name} 的接收数 ac 必须是非负整数", code="RULE_CRITERIA_INVALID")
        return rule

    @staticmethod
    def find_overlaps(rules: list[SamplingRule]) -> list[dict[str, str]]:
        """检测同一（物料类别, 供应商等级）下的批量区间重叠。"""
        groups: dict[tuple[str, str], list[SamplingRule]] = {}
        for rule in rules:
            groups.setdefault((rule.material_category, rule.supplier_level), []).append(rule)
        conflicts: list[dict[str, str]] = []
        for grouped in groups.values():
            ordered = sorted(grouped, key=lambda r: r.lot_min)
            for prev, nxt in zip(ordered, ordered[1:]):
                if prev.lot_max is None or nxt.lot_min <= prev.lot_max:
                    conflicts.append({"a": prev.rule_id, "b": nxt.rule_id})
        return conflicts

    def create_rule_version(self, *, actor: str, role: str, title: str, rules: list[dict[str, Any]]) -> dict[str, Any]:
        self._require_role(role, {Role.QUALITY_ENGINEER})
        title = _require_text(title, "title")
        if not isinstance(rules, list) or not rules:
            raise ValidationError("规则版本至少包含一条规则", code="RULES_EMPTY")
        with self._lock:
            version_id = self.store.next_id("RV")
            parsed = [self._parse_rule(version_id, i, raw) for i, raw in enumerate(rules)]
            version = RuleVersion(
                version_id=version_id,
                title=title,
                status=VersionStatus.DRAFT.value,
                rules=parsed,
                created_by=actor,
                created_at=self._now(),
            )
            self._save_version(version)
            self.store.save()
            return version.to_dict()

    def list_rule_versions(self, status: str | None = None) -> list[dict[str, Any]]:
        items = [RuleVersion.from_dict(raw).to_dict() for raw in self.store.data["versions"].values()]
        if status:
            items = [v for v in items if v["status"] == status]
        return sorted(items, key=lambda v: v["version_id"])

    def get_rule_version(self, version_id: str) -> dict[str, Any]:
        return self._get_version(version_id).to_dict()

    def submit_rule_version(self, *, actor: str, role: str, version_id: str) -> dict[str, Any]:
        self._require_role(role, {Role.QUALITY_ENGINEER})
        with self._lock:
            version = self._get_version(version_id)
            if version.status != VersionStatus.DRAFT.value:
                raise ConflictError(
                    f"仅草拟状态可提交，当前状态：{version.status}", code="VERSION_STATE_INVALID"
                )
            conflicts = self.find_overlaps(version.rules)
            if conflicts:
                raise ConflictError(
                    "同一物料类别与供应商等级下存在批量区间重叠，禁止提交",
                    code="RULE_OVERLAP",
                    details={"conflicts": conflicts},
                )
            version.status = VersionStatus.PENDING.value
            self._save_version(version)
            self.store.save()
            return version.to_dict()

    def release_rule_version(self, *, actor: str, role: str, version_id: str) -> dict[str, Any]:
        """下达版本：校验重叠后生效，并同时关闭此前已下达的版本（时态排他）。"""
        self._require_role(role, {Role.QUALITY_ENGINEER})
        with self._lock:
            version = self._get_version(version_id)
            if version.status != VersionStatus.PENDING.value:
                raise ConflictError(
                    f"仅待确认状态可下达，当前状态：{version.status}", code="VERSION_STATE_INVALID"
                )
            conflicts = self.find_overlaps(version.rules)
            if conflicts:
                raise ConflictError(
                    "同一物料类别与供应商等级下存在批量区间重叠，禁止下达",
                    code="RULE_OVERLAP",
                    details={"conflicts": conflicts},
                )
            now = self._now()
            for raw in self.store.data["versions"].values():
                current = RuleVersion.from_dict(raw)
                if current.status == VersionStatus.RELEASED.value:
                    current.status = VersionStatus.CLOSED.value
                    current.superseded_at = now
                    self._save_version(current)
            version.status = VersionStatus.RELEASED.value
            version.effective_from = now
            self._save_version(version)
            self.store.save()
            return version.to_dict()

    # ------------------------------------------------------------------ 时态解析

    def _resolve_version(self, at: datetime) -> RuleVersion:
        """解析收货时刻唯一生效的版本。"""
        matches: list[RuleVersion] = []
        for raw in self.store.data["versions"].values():
            version = RuleVersion.from_dict(raw)
            if version.status not in (VersionStatus.RELEASED.value, VersionStatus.CLOSED.value):
                continue
            if version.effective_from is None:
                continue
            if _parse_ts(version.effective_from) > at:
                continue
            if version.superseded_at is not None and at >= _parse_ts(version.superseded_at):
                continue
            matches.append(version)
        if not matches:
            raise NotFoundError("收货时刻没有生效中的抽样规则版本", code="NO_EFFECTIVE_VERSION")
        if len(matches) > 1:
            ids = [v.version_id for v in matches]
            raise ConflictError("收货时刻命中多个规则版本，无法解析唯一版本", code="AMBIGUOUS_VERSION",
                                details={"version_ids": ids})
        return matches[0]

    @staticmethod
    def _resolve_rule(version: RuleVersion, category: str, level: str, quantity: int) -> SamplingRule:
        matches = [r for r in version.rules if r.matches(category, level, quantity)]
        if not matches:
            raise NotFoundError(
                f"版本 {version.version_id} 中不存在匹配（{category}, {level}, 批量 {quantity}）的抽样规则",
                code="RULE_NOT_FOUND",
            )
        if len(matches) > 1:  # 下达时已做重叠检测，此处为防御性检查
            raise ConflictError("命中多条抽样规则，无法解析唯一规则", code="RULE_AMBIGUOUS",
                                details={"rule_ids": [r.rule_id for r in matches]})
        return matches[0]

    # ------------------------------------------------------------------ 样本量计算

    def _active_tightening(self, category: str, level: str, at: datetime) -> Tightening | None:
        """取生效中且乘数最大（最严）的加严指令。"""
        best: Tightening | None = None
        for raw in self.store.data["tightenings"].values():
            t = Tightening.from_dict(raw)
            if t.status != TighteningStatus.ACTIVE.value:
                continue
            if t.material_category != category:
                continue
            if t.supplier_level is not None and t.supplier_level != level:
                continue
            if _parse_ts(t.effective_from) > at:
                continue
            if t.expires_at is not None and at >= _parse_ts(t.expires_at):
                continue
            if best is None or t.multiplier > best.multiplier:
                best = t
        return best

    def _applicable_exemption(self, category: str, level: str, batch_id: str, at: datetime) -> Exemption | None:
        """取最具体的已批准豁免：批次级 > 类别+等级级 > 类别级；同级取先批准的。"""
        best: Exemption | None = None
        best_rank = -1
        for raw in self.store.data["exemptions"].values():
            e = Exemption.from_dict(raw)
            if e.status != ExemptionStatus.APPROVED.value:
                continue
            if e.material_category != category:
                continue
            if e.supplier_level is not None and e.supplier_level != level:
                continue
            if e.batch_id is not None and e.batch_id != batch_id:
                continue
            if e.decided_at is not None and _parse_ts(e.decided_at) > at:
                continue
            if e.expires_at is not None and at >= _parse_ts(e.expires_at):
                continue
            rank = 2 if e.batch_id else (1 if e.supplier_level else 0)
            if rank > best_rank or (rank == best_rank and best is not None and e.decided_at < best.decided_at):
                best, best_rank = e, rank
        return best

    def _compute_figures(
        self, rule: SamplingRule, quantity: int, batch_id: str, at: datetime
    ) -> tuple[int, int, list[dict[str, Any]], list[str], list[str]]:
        """返回 (基础样本量, 最终样本量, 计算步骤, 加严指令, 豁免单)。"""
        steps: list[dict[str, Any]] = []
        raw_value = quantity * rule.sample_ratio
        n = math.ceil(raw_value)
        steps.append({
            "step": "按比例计算",
            "formula": f"ceil({quantity} × {rule.sample_ratio})",
            "raw": raw_value,
            "value": n,
        })
        if n < rule.min_sample:
            n = rule.min_sample
            steps.append({"step": "最小样本量约束", "min_sample": rule.min_sample, "value": n})
        if rule.max_sample is not None and n > rule.max_sample:
            n = rule.max_sample
            steps.append({"step": "最大样本量约束", "max_sample": rule.max_sample, "value": n})
        if n > quantity:
            n = quantity
            steps.append({"step": "不超过批量", "lot_size": quantity, "value": n})
        base = n

        tightening_ids: list[str] = []
        tightening = self._active_tightening(rule.material_category, rule.supplier_level, at)
        if tightening is not None:
            tightened = min(quantity, math.ceil(n * tightening.multiplier))
            steps.append({
                "step": "紧急加严",
                "tightening_id": tightening.tightening_id,
                "multiplier": tightening.multiplier,
                "from": n,
                "value": tightened,
                "reason": tightening.reason,
            })
            n = tightened
            tightening_ids.append(tightening.tightening_id)

        exemption_ids: list[str] = []
        exemption = self._applicable_exemption(rule.material_category, rule.supplier_level, batch_id, at)
        if exemption is not None:
            if exemption.mode == ExemptionMode.SKIP.value:
                reduced = 0
            else:
                reduced = math.ceil(n * float(exemption.reduce_factor))
            steps.append({
                "step": "豁免批准",
                "exemption_id": exemption.exemption_id,
                "mode": exemption.mode,
                "from": n,
                "value": reduced,
                "reason": exemption.reason,
            })
            n = reduced
            exemption_ids.append(exemption.exemption_id)

        return base, n, steps, tightening_ids, exemption_ids

    # ------------------------------------------------------------------ 收货与检验计划

    def create_receipt(
        self,
        *,
        actor: str,
        role: str,
        batch_id: str,
        material_category: str,
        supplier_level: str,
        quantity: Any,
        received_at: str | None = None,
    ) -> dict[str, Any]:
        """登记收货批次：解析唯一规则版本，生成不可变检验计划。"""
        self._require_role(role, {Role.WAREHOUSE_KEEPER})
        batch_id = _require_text(batch_id, "batch_id")
        material_category = _require_text(material_category, "material_category")
        supplier_level = _require_text(supplier_level, "supplier_level")
        quantity = _as_int(quantity, "quantity")
        if quantity < 1:
            raise ValidationError("收货数量必须 ≥ 1", code="QUANTITY_INVALID")
        at = _parse_ts_field(received_at, "received_at") if received_at else self.clock()
        received = at.isoformat()
        with self._lock:
            existing = self.store.data["batches"].get(batch_id)
            if existing is not None:
                raise ConflictError(
                    f"批次 {batch_id} 已登记，检验计划 {existing['plan_id']}",
                    code="DUPLICATE_BATCH",
                    details={"plan_id": existing["plan_id"]},
                )
            version = self._resolve_version(at)
            rule = self._resolve_rule(version, material_category, supplier_level, quantity)
            base, final, steps, tightening_ids, exemption_ids = self._compute_figures(
                rule, quantity, batch_id, at
            )
            now = self._now()
            plan = InspectionPlan(
                plan_id=self.store.next_id("IP"),
                batch_id=batch_id,
                material_category=material_category,
                supplier_level=supplier_level,
                lot_size=quantity,
                rule_version_id=version.version_id,
                rule_id=rule.rule_id,
                rule_snapshot=rule.to_dict(),
                resolved_at=received,
                sample_size=final,
                base_sample_size=base,
                steps=steps,
                defect_criteria=rule.defect_criteria,
                tightening_ids=tightening_ids,
                exemption_ids=exemption_ids,
                overrides=[],
                events=[{
                    "at": now, "actor": actor, "action": "计划生成",
                    "detail": f"依据规则版本 {version.version_id} 的规则 {rule.rule_id} 生成",
                }],
                status=PlanStatus.RELEASED.value,
                created_at=now,
            )
            batch = Batch(
                batch_id=batch_id,
                material_category=material_category,
                supplier_level=supplier_level,
                quantity=quantity,
                received_at=received,
                plan_id=plan.plan_id,
            )
            self._save_plan(plan)
            self.store.data["batches"][batch_id] = batch.to_dict()
            self.store.save()
            return plan.to_dict()

    def preview_plan(
        self,
        *,
        actor: str,
        role: str,
        material_category: str,
        supplier_level: str,
        quantity: Any,
        at: str | None = None,
    ) -> dict[str, Any]:
        """试算：按指定时刻解析规则并解释样本量，不产生任何持久化数据。"""
        self._require_role(role, set(Role))
        material_category = _require_text(material_category, "material_category")
        supplier_level = _require_text(supplier_level, "supplier_level")
        quantity = _as_int(quantity, "quantity")
        if quantity < 1:
            raise ValidationError("数量必须 ≥ 1", code="QUANTITY_INVALID")
        moment = _parse_ts_field(at, "at") if at else self.clock()
        with self._lock:
            version = self._resolve_version(moment)
            rule = self._resolve_rule(version, material_category, supplier_level, quantity)
            base, final, steps, tightening_ids, exemption_ids = self._compute_figures(
                rule, quantity, "<preview>", moment
            )
        return {
            "rule_version_id": version.version_id,
            "rule_id": rule.rule_id,
            "resolved_at": moment.isoformat(),
            "lot_size": quantity,
            "base_sample_size": base,
            "sample_size": final,
            "steps": steps,
            "defect_criteria": rule.defect_criteria,
            "tightening_ids": tightening_ids,
            "exemption_ids": exemption_ids,
        }

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        return self._get_plan(plan_id).to_dict()

    def list_plans(self, batch_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        items = [InspectionPlan.from_dict(raw).to_dict() for raw in self.store.data["plans"].values()]
        if batch_id:
            items = [p for p in items if p["batch_id"] == batch_id]
        if status:
            items = [p for p in items if p["status"] == status]
        return sorted(items, key=lambda p: p["plan_id"])

    def explain_plan(self, plan_id: str) -> dict[str, Any]:
        """解释样本量计算全过程与每次人工覆盖，供事后追溯。"""
        plan = self._get_plan(plan_id)
        version = self._get_version(plan.rule_version_id)
        return {
            "plan_id": plan.plan_id,
            "batch_id": plan.batch_id,
            "status": plan.status,
            "rule_version_id": plan.rule_version_id,
            "rule_id": plan.rule_id,
            "resolved_at": plan.resolved_at,
            "version_window": {
                "effective_from": version.effective_from,
                "superseded_at": version.superseded_at,
            },
            "inputs": {
                "lot_size": plan.lot_size,
                "sample_ratio": plan.rule_snapshot["sample_ratio"],
                "min_sample": plan.rule_snapshot["min_sample"],
                "max_sample": plan.rule_snapshot["max_sample"],
            },
            "steps": plan.steps,
            "base_sample_size": plan.base_sample_size,
            "final_sample_size": plan.sample_size,
            "defect_criteria": plan.defect_criteria,
            "tightening_ids": plan.tightening_ids,
            "exemption_ids": plan.exemption_ids,
            "overrides": [o.to_dict() for o in plan.overrides],
        }

    def plan_audit(self, plan_id: str) -> dict[str, Any]:
        plan = self._get_plan(plan_id)
        return {
            "plan_id": plan.plan_id,
            "batch_id": plan.batch_id,
            "status": plan.status,
            "closed_at": plan.closed_at,
            "close_reason": plan.close_reason,
            "overrides": [o.to_dict() for o in plan.overrides],
            "events": plan.events,
        }

    # ------------------------------------------------------------------ 计划生命周期

    def start_plan(self, *, actor: str, role: str, plan_id: str) -> dict[str, Any]:
        self._require_role(role, {Role.WAREHOUSE_KEEPER})
        with self._lock:
            plan = self._get_plan(plan_id)
            if plan.status != PlanStatus.RELEASED.value:
                raise ConflictError(f"仅已下达状态可开始检验，当前状态：{plan.status}",
                                    code="PLAN_STATE_INVALID")
            plan.status = PlanStatus.IN_PROGRESS.value
            plan.events.append({"at": self._now(), "actor": actor, "action": "开始检验", "detail": ""})
            self._save_plan(plan)
            self.store.save()
            return plan.to_dict()

    def complete_plan(self, *, actor: str, role: str, plan_id: str, note: str | None = None) -> dict[str, Any]:
        """关闭计划：此后样本量与规则快照永久冻结，不随新规则版本变化。"""
        self._require_role(role, {Role.WAREHOUSE_KEEPER, Role.QUALITY_ENGINEER})
        with self._lock:
            plan = self._get_plan(plan_id)
            if plan.status == PlanStatus.CLOSED.value:
                raise ConflictError("检验计划已关闭", code="PLAN_CLOSED")
            now = self._now()
            plan.status = PlanStatus.CLOSED.value
            plan.closed_at = now
            plan.close_reason = note or "检验完成"
            plan.events.append({"at": now, "actor": actor, "action": "计划关闭",
                                "detail": plan.close_reason})
            self._save_plan(plan)
            self.store.save()
            return plan.to_dict()

    def override_plan(
        self, *, actor: str, role: str, plan_id: str, new_sample_size: Any, reason: str
    ) -> dict[str, Any]:
        """人工覆盖样本量：质量工程师最终裁定，全程留痕。"""
        self._require_role(role, {Role.QUALITY_ENGINEER})
        reason = _require_text(reason, "reason")
        new_size = _as_int(new_sample_size, "new_sample_size")
        with self._lock:
            plan = self._get_plan(plan_id)
            if plan.status == PlanStatus.CLOSED.value:
                raise ConflictError("已关闭的检验计划不可覆盖", code="PLAN_CLOSED")
            if not (0 <= new_size <= plan.lot_size):
                raise ValidationError(
                    f"覆盖后的样本量必须在 [0, {plan.lot_size}] 区间", code="OVERRIDE_OUT_OF_RANGE"
                )
            now = self._now()
            record = OverrideRecord(
                override_id=self.store.next_id("OV"),
                old_sample_size=plan.sample_size,
                new_sample_size=new_size,
                reason=reason,
                actor=actor,
                at=now,
            )
            plan.overrides.append(record)
            plan.sample_size = new_size
            plan.steps.append({
                "step": "人工覆盖",
                "override_id": record.override_id,
                "from": record.old_sample_size,
                "value": new_size,
                "actor": actor,
                "reason": reason,
            })
            plan.events.append({"at": now, "actor": actor, "action": "人工覆盖",
                                "detail": f"{record.old_sample_size} → {new_size}：{reason}"})
            self._save_plan(plan)
            self.store.save()
            return plan.to_dict()

    # ------------------------------------------------------------------ 批次拆分

    def split_batch(self, *, actor: str, role: str, batch_id: str, quantities: list[Any]) -> dict[str, Any]:
        """拆分批次：子批继承父计划的版本与规则快照，父样本量按比例分配到子计划。"""
        self._require_role(role, {Role.WAREHOUSE_KEEPER})
        if not isinstance(quantities, list) or len(quantities) < 2:
            raise ValidationError("拆分至少需要两个子批次", code="SPLIT_COUNT_INVALID")
        parts = [_as_int(q, "quantities") for q in quantities]
        if any(q < 1 for q in parts):
            raise ValidationError("子批次数量必须 ≥ 1", code="SPLIT_QUANTITY_INVALID")
        with self._lock:
            raw_batch = self.store.data["batches"].get(batch_id)
            if raw_batch is None:
                raise NotFoundError(f"批次不存在：{batch_id}", code="BATCH_NOT_FOUND")
            batch = Batch.from_dict(raw_batch)
            plan = self._get_plan(batch.plan_id)
            if plan.status == PlanStatus.CLOSED.value:
                raise ConflictError("已关闭的检验计划不能拆分", code="PLAN_CLOSED")
            if sum(parts) != batch.quantity:
                raise ValidationError(
                    f"子批次数量之和 {sum(parts)} 必须等于原批次数量 {batch.quantity}",
                    code="SPLIT_QUANTITY_MISMATCH",
                )
            child_ids = [f"{batch_id}-S{i + 1}" for i in range(len(parts))]
            for child_id in child_ids:
                if child_id in self.store.data["batches"]:
                    raise ConflictError(f"子批次编号已存在：{child_id}", code="DUPLICATE_BATCH")

            allocation = allocate_samples(plan.sample_size, parts)
            now = self._now()
            parent_status = plan.status
            plan.status = PlanStatus.CLOSED.value
            plan.closed_at = now
            plan.close_reason = "批次拆分"
            plan.events.append({"at": now, "actor": actor, "action": "批次拆分",
                                "detail": f"拆分为 {len(parts)} 个子批次：{'、'.join(child_ids)}"})
            self._save_plan(plan)

            children: list[InspectionPlan] = []
            for child_id, qty, sample in zip(child_ids, parts, allocation):
                child_plan = InspectionPlan(
                    plan_id=self.store.next_id("IP"),
                    batch_id=child_id,
                    material_category=batch.material_category,
                    supplier_level=batch.supplier_level,
                    lot_size=qty,
                    rule_version_id=plan.rule_version_id,
                    rule_id=plan.rule_id,
                    rule_snapshot=plan.rule_snapshot,
                    resolved_at=plan.resolved_at,
                    sample_size=sample,
                    base_sample_size=sample,
                    steps=[{
                        "step": "批次拆分继承",
                        "parent_plan_id": plan.plan_id,
                        "formula": f"父计划样本 {plan.sample_size} × {qty}/{batch.quantity}（最大余数法分配）",
                        "value": sample,
                        "rule_version_id": plan.rule_version_id,
                    }],
                    defect_criteria=plan.defect_criteria,
                    tightening_ids=list(plan.tightening_ids),
                    exemption_ids=list(plan.exemption_ids),
                    overrides=[],
                    events=[{"at": now, "actor": actor, "action": "批次拆分生成",
                             "detail": f"继承父计划 {plan.plan_id} 的规则版本 {plan.rule_version_id}"}],
                    status=parent_status,
                    created_at=now,
                    parent_plan_id=plan.plan_id,
                )
                child_batch = Batch(
                    batch_id=child_id,
                    material_category=batch.material_category,
                    supplier_level=batch.supplier_level,
                    quantity=qty,
                    received_at=batch.received_at,
                    plan_id=child_plan.plan_id,
                    parent_batch_id=batch_id,
                )
                self._save_plan(child_plan)
                self.store.data["batches"][child_id] = child_batch.to_dict()
                children.append(child_plan)
            self.store.save()
            return {
                "parent_plan_id": plan.plan_id,
                "children": [c.to_dict() for c in children],
            }

    # ------------------------------------------------------------------ 紧急加严

    def create_tightening(
        self,
        *,
        actor: str,
        role: str,
        material_category: str,
        supplier_level: str | None = None,
        multiplier: Any,
        reason: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(role, {Role.QUALITY_ENGINEER})
        material_category = _require_text(material_category, "material_category")
        reason = _require_text(reason, "reason")
        try:
            factor = float(multiplier)
        except (TypeError, ValueError):
            raise ValidationError("multiplier 必须是数值", code="MULTIPLIER_INVALID")
        if factor <= 1:
            raise ValidationError("加严乘数必须 > 1", code="MULTIPLIER_INVALID")
        if expires_at is not None:
            _parse_ts_field(expires_at, "expires_at")
        with self._lock:
            tightening = Tightening(
                tightening_id=self.store.next_id("TG"),
                material_category=material_category,
                supplier_level=supplier_level,
                multiplier=factor,
                reason=reason,
                created_by=actor,
                effective_from=self._now(),
                expires_at=expires_at,
                status=TighteningStatus.ACTIVE.value,
            )
            self.store.data["tightenings"][tightening.tightening_id] = tightening.to_dict()
            self.store.save()
            return tightening.to_dict()

    def revoke_tightening(self, *, actor: str, role: str, tightening_id: str) -> dict[str, Any]:
        self._require_role(role, {Role.QUALITY_ENGINEER})
        with self._lock:
            raw = self.store.data["tightenings"].get(tightening_id)
            if raw is None:
                raise NotFoundError(f"加严指令不存在：{tightening_id}", code="TIGHTENING_NOT_FOUND")
            tightening = Tightening.from_dict(raw)
            if tightening.status != TighteningStatus.ACTIVE.value:
                raise ConflictError("加严指令已撤销", code="TIGHTENING_REVOKED")
            tightening.status = TighteningStatus.REVOKED.value
            tightening.revoked_at = self._now()
            self.store.data["tightenings"][tightening_id] = tightening.to_dict()
            self.store.save()
            return tightening.to_dict()

    def list_tightenings(self) -> list[dict[str, Any]]:
        items = [Tightening.from_dict(raw).to_dict() for raw in self.store.data["tightenings"].values()]
        return sorted(items, key=lambda t: t["tightening_id"])

    # ------------------------------------------------------------------ 豁免批准

    def request_exemption(
        self,
        *,
        actor: str,
        role: str,
        material_category: str,
        mode: str,
        reason: str,
        supplier_level: str | None = None,
        batch_id: str | None = None,
        reduce_factor: Any = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._require_role(role, {Role.PLANNER, Role.QUALITY_ENGINEER})
        material_category = _require_text(material_category, "material_category")
        reason = _require_text(reason, "reason")
        if mode not in (ExemptionMode.SKIP.value, ExemptionMode.REDUCE.value):
            raise ValidationError("mode 必须是 skip 或 reduce", code="EXEMPTION_MODE_INVALID")
        factor: float | None = None
        if mode == ExemptionMode.REDUCE.value:
            try:
                factor = float(reduce_factor)
            except (TypeError, ValueError):
                raise ValidationError("reduce 模式必须提供数值 reduce_factor", code="REDUCE_FACTOR_INVALID")
            if not (0 < factor < 1):
                raise ValidationError("reduce_factor 必须在 (0, 1) 区间", code="REDUCE_FACTOR_INVALID")
        if expires_at is not None:
            _parse_ts_field(expires_at, "expires_at")
        with self._lock:
            exemption = Exemption(
                exemption_id=self.store.next_id("EX"),
                material_category=material_category,
                supplier_level=supplier_level,
                batch_id=batch_id,
                mode=mode,
                reduce_factor=factor,
                reason=reason,
                requested_by=actor,
                status=ExemptionStatus.PENDING.value,
                created_at=self._now(),
                expires_at=expires_at,
            )
            self.store.data["exemptions"][exemption.exemption_id] = exemption.to_dict()
            self.store.save()
            return exemption.to_dict()

    def _decide_exemption(self, *, actor: str, role: str, exemption_id: str, approve: bool) -> dict[str, Any]:
        self._require_role(role, {Role.QUALITY_ENGINEER})
        with self._lock:
            raw = self.store.data["exemptions"].get(exemption_id)
            if raw is None:
                raise NotFoundError(f"豁免单不存在：{exemption_id}", code="EXEMPTION_NOT_FOUND")
            exemption = Exemption.from_dict(raw)
            if exemption.status != ExemptionStatus.PENDING.value:
                raise ConflictError(f"豁免单已处理，当前状态：{exemption.status}",
                                    code="EXEMPTION_STATE_INVALID")
            if exemption.requested_by == actor:
                raise ConflictError("申请人与批准人不得为同一人", code="EXEMPTION_SELF_APPROVAL")
            exemption.status = ExemptionStatus.APPROVED.value if approve else ExemptionStatus.REJECTED.value
            exemption.decided_by = actor
            exemption.decided_at = self._now()
            self.store.data["exemptions"][exemption_id] = exemption.to_dict()
            self.store.save()
            return exemption.to_dict()

    def approve_exemption(self, *, actor: str, role: str, exemption_id: str) -> dict[str, Any]:
        return self._decide_exemption(actor=actor, role=role, exemption_id=exemption_id, approve=True)

    def reject_exemption(self, *, actor: str, role: str, exemption_id: str) -> dict[str, Any]:
        return self._decide_exemption(actor=actor, role=role, exemption_id=exemption_id, approve=False)

    def list_exemptions(self) -> list[dict[str, Any]]:
        items = [Exemption.from_dict(raw).to_dict() for raw in self.store.data["exemptions"].values()]
        return sorted(items, key=lambda e: e["exemption_id"])
