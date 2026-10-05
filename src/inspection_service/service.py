"""应用服务层：编排规则版本、检验计划、人工覆盖、批次拆分用例。

所有写操作都向审计事件流追加不可变记录，actor 必须显式给出。
"""
from __future__ import annotations

from datetime import datetime
from itertools import count
from typing import Any

from . import states
from .errors import ConflictError, NotFoundError, ValidationError
from .models import (
    DefectClass,
    OVERRIDE_EXEMPT,
    OVERRIDE_KINDS,
    OVERRIDE_TIGHTEN,
    InspectionPlan,
    Override,
    RuleRow,
    RuleVersion,
    build_plan,
    iso,
    utcnow,
    validate_rows_cover,
)
from .store import Repository


class InspectionService:
    def __init__(self, repo: Repository | None = None) -> None:
        self.repo = repo or Repository()
        self._rule_seq = count(1)
        self._plan_seq = count(1)
        self._ov_seq = count(1)

    # -- 规则版本 ----------------------------------------------------------

    def create_rule_version(
        self,
        *,
        actor: str,
        material_category: str,
        supplier_grade: str,
        rows: list[dict],
        defect_classes: list[dict] | None = None,
        change_summary: str = "",
        now: datetime | None = None,
    ) -> RuleVersion:
        moment = now or utcnow()
        category = _require_text(material_category, "物料类别")
        grade = _require_text(supplier_grade, "供应商等级")
        if not rows:
            raise ValidationError("规则版本至少需要一条批量区间行")
        parsed_rows = [RuleRow.from_dict(r) for r in rows]
        for row in parsed_rows:
            row.validate()
        validate_rows_cover(parsed_rows)  # 草拟阶段即阻止重叠/缺口，而非等到下达
        parsed_defects = [DefectClass.from_dict(d) for d in defect_classes] if defect_classes else []
        for dc in parsed_defects:
            dc.validate()
        codes = [d.code for d in parsed_defects]
        if len(codes) != len(set(codes)):
            raise ValidationError("缺陷分级代码不能重复")

        existing = self.repo.versions_for_key(category, grade)
        supersedes = existing[-1].version_id if existing else None
        version = RuleVersion(
            version_id=f"RV-{next(self._rule_seq):04d}",
            material_category=category,
            supplier_grade=grade,
            rows=parsed_rows,
            defect_classes=parsed_defects,
            status=states.RULE_DRAFT,
            created_by=actor,
            created_at=moment,
            change_summary=change_summary,
            supersedes_version_id=supersedes,
        )
        self.repo.add_rule_version(version)
        self.repo.record(
            at=moment, actor=actor, action="rule_version.create",
            target_type="rule_version", target_id=version.version_id,
            detail={"rows": len(parsed_rows), "defect_classes": codes or "<默认三级>",
                    "supersedes": supersedes},
        )
        return version

    def submit_rule_version(self, *, actor: str, version_id: str, now: datetime | None = None) -> RuleVersion:
        moment = now or utcnow()
        version = self.repo.get_rule_version(version_id)
        if version.status != states.RULE_DRAFT:
            raise ConflictError("仅草拟版本可提交确认", details={"status": version.status})
        validate_rows_cover(version.rows)
        version.status = states.RULE_PENDING
        self.repo.record(
            at=moment, actor=actor, action="rule_version.submit",
            target_type="rule_version", target_id=version_id,
        )
        return version

    def issue_rule_version(self, *, actor: str, version_id: str, now: datetime | None = None) -> RuleVersion:
        """下达版本：同一 (类别, 等级) 上旧的生效版本自动关闭，保证任意时刻唯一生效。"""
        moment = now or utcnow()
        version = self.repo.get_rule_version(version_id)
        if version.status not in (states.RULE_DRAFT, states.RULE_PENDING):
            raise ConflictError("仅草拟/待确认版本可下达", details={"status": version.status})
        validate_rows_cover(version.rows)

        siblings = self.repo.versions_for_key(version.material_category, version.supplier_grade)
        closed_previous: list[str] = []
        for other in siblings:
            if other.version_id == version.version_id:
                continue
            if other.status == states.RULE_ISSUED:
                other.status = states.RULE_CLOSED
                other.closed_at = moment
                closed_previous.append(other.version_id)
                self.repo.record(
                    at=moment, actor=actor, action="rule_version.auto_close_superseded",
                    target_type="rule_version", target_id=other.version_id,
                    detail={"by": version_id},
                )
        version.status = states.RULE_ISSUED
        version.issued_at = moment
        self.repo.record(
            at=moment, actor=actor, action="rule_version.issue",
            target_type="rule_version", target_id=version_id,
            detail={"closed_previous": closed_previous},
        )
        return version

    def close_rule_version(self, *, actor: str, version_id: str, now: datetime | None = None) -> RuleVersion:
        """仅关闭版本（停止对未来收货生效），不影响已生成计划。"""
        moment = now or utcnow()
        version = self.repo.get_rule_version(version_id)
        if version.status != states.RULE_ISSUED:
            raise ConflictError("仅已下达版本可关闭", details={"status": version.status})
        version.status = states.RULE_CLOSED
        version.closed_at = moment
        self.repo.record(
            at=moment, actor=actor, action="rule_version.close",
            target_type="rule_version", target_id=version_id,
        )
        return version

    def resolve_rule_version(
        self, material_category: str, supplier_grade: str, moment: datetime
    ) -> RuleVersion:
        """收货时解析唯一生效版本；0 个或多个都视为错误。"""
        candidates = self.repo.versions_for_key(material_category, supplier_grade)
        effective = [v for v in candidates if v.effective_on(moment)]
        if len(effective) > 1:
            raise ConflictError(
                "规则版本时间窗重叠，无法解析唯一版本",
                details={"effective": [v.version_id for v in effective]},
            )
        if not effective:
            raise NotFoundError(
                "收货时刻没有已下达且生效的抽样规则版本",
                details={"material_category": material_category, "supplier_grade": supplier_grade, "at": iso(moment)},
            )
        return effective[0]

    # -- 检验计划 ----------------------------------------------------------

    def receive_goods(
        self,
        *,
        actor: str,
        batch_no: str,
        material_category: str,
        supplier_grade: str,
        supplier_id: str,
        lot_qty: int,
        received_at: datetime | None = None,
        parent_plan_id: str | None = None,
    ) -> InspectionPlan:
        """收货：解析唯一版本与唯一规则行，生成不可变检验计划。"""
        moment = received_at or utcnow()
        batch_no = _require_text(batch_no, "批次号")
        _require_text(supplier_id, "供应商")
        if parent_plan_id:
            parent = self.repo.get_plan(parent_plan_id)
            if parent.status != states.PLAN_CLOSED or parent.close_reason != states.CLOSE_SPLIT:
                raise ConflictError(
                    "只有因批次拆分关闭的父计划才能派生新计划",
                    details={"parent_plan_id": parent_plan_id, "status": parent.status, "close_reason": parent.close_reason},
                )
            if material_category != parent.material_category or supplier_grade != parent.supplier_grade:
                raise ValidationError("拆分子批的物料类别与供应商等级必须与父批一致")

        candidates = self.repo.versions_for_key(material_category, supplier_grade)
        version = self.resolve_rule_version(material_category, supplier_grade, moment)
        plan = build_plan(
            plan_id=f"IP-{next(self._plan_seq):05d}",
            batch_no=batch_no,
            supplier_id=supplier_id,
            lot_qty=lot_qty,
            version=version,
            moment=moment,
            created_by=actor,
            candidates_scanned=candidates,
            parent_plan_id=parent_plan_id,
        )
        self.repo.add_plan(plan)
        if parent_plan_id:
            self.repo.get_plan(parent_plan_id).child_plan_ids.append(plan.plan_id)
        self.repo.record(
            at=moment, actor=actor, action="plan.create",
            target_type="inspection_plan", target_id=plan.plan_id,
            detail={
                "batch_no": batch_no,
                "lot_qty": lot_qty,
                "rule_version_id": version.version_id,
                "rule_fingerprint": version.fingerprint(),
                "code_letter": plan.code_letter,
                "base_sample_size": plan.base_sample_size,
                "parent_plan_id": parent_plan_id,
            },
        )
        return plan

    def start_plan(self, *, actor: str, plan_id: str, now: datetime | None = None) -> InspectionPlan:
        moment = now or utcnow()
        plan = self.repo.get_plan(plan_id)
        plan.start(actor, moment)
        self.repo.record(
            at=moment, actor=actor, action="plan.start",
            target_type="inspection_plan", target_id=plan_id,
            detail={"final_sample_size": plan.final_sample_size},
        )
        return plan

    def record_result(
        self,
        *,
        actor: str,
        plan_id: str,
        inspected_count: int,
        defect_count: int | None = None,
        defect_grades: list[str] | None = None,
        defect_counts_by_class: dict | None = None,
        disposition: str,
        now: datetime | None = None,
    ) -> InspectionPlan:
        moment = now or utcnow()
        plan = self.repo.get_plan(plan_id)
        plan.record_result(
            actor, inspected_count, defect_count, defect_grades or [],
            disposition, moment, defect_counts_by_class=defect_counts_by_class,
        )
        self.repo.record(
            at=moment, actor=actor, action="plan.complete",
            target_type="inspection_plan", target_id=plan_id,
            detail={
                "inspected_count": inspected_count,
                "defect_count": plan.defect_count,
                "defect_counts_by_class": plan.defect_counts_by_class,
                "class_verdicts": plan.class_verdicts,
                "defect_grades": defect_grades or [],
                "disposition": disposition,
            },
        )
        return plan

    # -- 批次拆分 ----------------------------------------------------------

    def split_batch(
        self,
        *,
        actor: str,
        parent_plan_id: str,
        child_lots: list[int],
        now: datetime | None = None,
    ) -> list[InspectionPlan]:
        """批次拆分：父计划以「批次拆分」关闭冻结；按各子批量重新解析当时规则生成子计划。

        子批量之和必须等于父批量；子计划各自独立冻结，规则按拆分时刻重新解析
        （若恰好跨越版本切换，父子可能引用不同版本，均各自留痕可解释）。
        """
        moment = now or utcnow()
        parent = self.repo.get_plan(parent_plan_id)
        if not child_lots:
            raise ValidationError("拆分至少需要一个子批量")
        if any((not isinstance(q, int)) or q < 1 for q in child_lots):
            raise ValidationError("子批量必须为正整数")
        if sum(child_lots) != parent.lot_qty:
            raise ConflictError(
                "子批量之和必须等于父批量",
                details={"parent_lot_qty": parent.lot_qty, "child_sum": sum(child_lots)},
            )
        parent.close_due_to_split(actor, moment)
        self.repo.record(
            at=moment, actor=actor, action="plan.split_close_parent",
            target_type="inspection_plan", target_id=parent_plan_id,
            detail={"child_lots": child_lots},
        )
        children: list[InspectionPlan] = []
        for qty in child_lots:
            child = self.receive_goods(
                actor=actor,
                batch_no=f"{parent.batch_no}-S{len(children) + 1}",
                material_category=parent.material_category,
                supplier_grade=parent.supplier_grade,
                supplier_id=parent.supplier_id,
                lot_qty=qty,
                received_at=moment,
                parent_plan_id=parent.plan_id,
            )
            children.append(child)
        return children

    # -- 人工覆盖 ----------------------------------------------------------

    def request_override(
        self,
        *,
        actor: str,
        plan_id: str,
        kind: str,
        reason: str,
        multiply: float = 1.0,
        add: int = 0,
        now: datetime | None = None,
    ) -> Override:
        """申请紧急加严或豁免。申请即留痕；未批准不影响样本量。"""
        moment = now or utcnow()
        plan = self.repo.get_plan(plan_id)
        if kind not in OVERRIDE_KINDS:
            raise ValidationError("非法覆盖类型", details={"kind": kind, "allowed": list(OVERRIDE_KINDS)})
        _require_text(reason, "覆盖原因")
        if kind == OVERRIDE_TIGHTEN:
            if multiply < 1.0:
                raise ValidationError("紧急加严倍数必须不小于 1.0（不得借加严之名减少抽样）")
            if add < 0:
                raise ValidationError("加严追加样本量不能为负")
        override = Override(
            override_id=f"OV-{next(self._ov_seq):05d}",
            kind=kind,
            requested_by=actor,
            reason=reason,
            created_at=moment,
            multiply=multiply,
            add=add,
        )
        self.repo.add_override(override)
        plan.attach_override(override)
        self.repo.record(
            at=moment, actor=actor, action=f"override.request.{kind}",
            target_type="override", target_id=override.override_id,
            detail={"plan_id": plan_id, "reason": reason, "multiply": multiply, "add": add,
                    "final_sample_size": plan.final_sample_size},
        )
        return override

    def approve_override(
        self,
        *,
        actor: str,
        override_id: str,
        comment: str = "",
        now: datetime | None = None,
    ) -> Override:
        """批准覆盖。approve 角色在 HTTP 层限制为质量工程师。"""
        moment = now or utcnow()
        override = self.repo.get_override(override_id)
        plan = self._plan_of_override(override_id)
        approved = plan.approve_override(override_id, actor, comment, moment)
        self.repo.overrides[override_id] = approved
        self.repo.record(
            at=moment, actor=actor, action=f"override.approve.{override.kind}",
            target_type="override", target_id=override_id,
            detail={"plan_id": plan.plan_id, "comment": comment,
                    "final_sample_size": plan.final_sample_size},
        )
        return approved

    def revoke_override(
        self,
        *,
        actor: str,
        override_id: str,
        reason: str,
        now: datetime | None = None,
    ) -> Override:
        moment = now or utcnow()
        _require_text(reason, "撤销原因")
        override = self.repo.get_override(override_id)
        plan = self._plan_of_override(override_id)
        revoked = plan.revoke_override(override_id, actor, reason, moment)
        self.repo.overrides[override_id] = revoked
        self.repo.record(
            at=moment, actor=actor, action=f"override.revoke.{override.kind}",
            target_type="override", target_id=override_id,
            detail={"plan_id": plan.plan_id, "reason": reason,
                    "final_sample_size": plan.final_sample_size},
        )
        return revoked

    def _plan_of_override(self, override_id: str) -> InspectionPlan:
        for plan in self.repo.all_plans():
            if any(o.override_id == override_id for o in plan.overrides):
                return plan
        raise NotFoundError("覆盖未关联任何计划", details={"override_id": override_id})

    # -- 查询 --------------------------------------------------------------

    def explain_plan(self, plan_id: str) -> dict[str, Any]:
        plan = self.repo.get_plan(plan_id)
        explanation = plan.explanation()
        explanation["audit_trail"] = [
            e.to_dict() for e in self.repo.audit_trail("inspection_plan", plan_id)
        ] + [
            e.to_dict()
            for e in self.repo.audit_trail("override")
            if e.detail.get("plan_id") == plan_id
        ]
        explanation["audit_trail"].sort(key=lambda e: (e["seq"]))
        return explanation

    def batch_history(self, batch_no: str) -> dict[str, Any]:
        seeds = self.repo.plans_for_batch(batch_no)
        if not seeds:
            raise NotFoundError("批次不存在", details={"batch_no": batch_no})
        # 沿父子血缘收集整个拆分簇（子批次号带 -S1 后缀，不能只靠名字匹配）
        collected: dict[str, InspectionPlan] = {}
        stack = list(seeds)
        while stack:
            current = stack.pop()
            if current.plan_id in collected:
                continue
            collected[current.plan_id] = current
            if current.parent_plan_id:
                stack.append(self.repo.get_plan(current.parent_plan_id))
            stack.extend(self.repo.get_plan(cid) for cid in current.child_plan_ids)
        plans = sorted(collected.values(), key=lambda p: p.created_at)
        return {
            "batch_no": batch_no,
            "plans": [
                {
                    "plan_id": p.plan_id,
                    "status": p.status,
                    "lot_qty": p.lot_qty,
                    "final_sample_size": p.final_sample_size,
                    "rule_version_id": p.rule_version_id,
                    "rule_version_fingerprint": p.rule_version_fingerprint,
                    "parent_plan_id": p.parent_plan_id,
                    "child_plan_ids": p.child_plan_ids,
                    "close_reason": p.close_reason,
                    "created_at": iso(p.created_at),
                }
                for p in plans
            ],
        }


def _require_text(value: str, label: str) -> str:
    if value is None or not str(value).strip():
        raise ValidationError(f"{label}不能为空")
    return str(value).strip()
