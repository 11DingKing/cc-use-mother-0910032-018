"""服务端领域用例测试：时态、重叠、冻结、覆盖、拆分。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inspection_service import InspectionService
from inspection_service.errors import (
    ConflictError,
    ImmutablePlanError,
    NotFoundError,
    ValidationError,
)
from inspection_service.models import OVERRIDE_EXEMPT, OVERRIDE_TIGHTEN
from inspection_service import states

QE = "质量工程师-张敏"
WH = "仓储管理员-李库"

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


def rows_v1():
    return [
        {"lot_min": 1, "lot_max": 50, "base_sample_size": 5, "inspection_level": "II",
         "accept_number": 0, "reject_number": 1},
        {"lot_min": 51, "lot_max": 200, "base_sample_size": 20, "inspection_level": "II",
         "accept_number": 1, "reject_number": 2},
        {"lot_min": 201, "lot_max": None, "base_sample_size": 50, "inspection_level": "II",
         "accept_number": 3, "reject_number": 4},
    ]


def rows_v2():
    return [
        {"lot_min": 1, "lot_max": 50, "base_sample_size": 8, "inspection_level": "II",
         "accept_number": 0, "reject_number": 1},
        {"lot_min": 51, "lot_max": 200, "base_sample_size": 32, "inspection_level": "II",
         "accept_number": 1, "reject_number": 2},
        {"lot_min": 201, "lot_max": None, "base_sample_size": 80, "inspection_level": "II",
         "accept_number": 3, "reject_number": 4},
    ]


class RuleVersionLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()

    def test_overlap_rejected_at_creation(self) -> None:
        bad = [
            {"lot_min": 1, "lot_max": 100, "base_sample_size": 5, "inspection_level": "II"},
            {"lot_min": 50, "lot_max": 200, "base_sample_size": 8, "inspection_level": "II"},
            {"lot_min": 201, "lot_max": None, "base_sample_size": 13, "inspection_level": "II"},
        ]
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_rule_version(
                actor=QE, material_category="电子元件", supplier_grade="A", rows=bad)
        self.assertEqual(ctx.exception.details["problems"][0]["type"], "overlap")

    def test_gap_rejected(self) -> None:
        bad = [
            {"lot_min": 1, "lot_max": 50, "base_sample_size": 5, "inspection_level": "II"},
            {"lot_min": 100, "lot_max": None, "base_sample_size": 8, "inspection_level": "II"},
        ]
        with self.assertRaises(ConflictError) as ctx:
            self.svc.create_rule_version(
                actor=QE, material_category="电子元件", supplier_grade="A", rows=bad)
        self.assertTrue(any(p["type"] == "gap" for p in ctx.exception.details["problems"]))

    def test_touching_boundaries_are_continuous(self) -> None:
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v1())
        self.assertEqual(v.status, states.RULE_DRAFT)
        self.svc.submit_rule_version(actor=QE, version_id=v.version_id)
        self.svc.issue_rule_version(actor=QE, version_id=v.version_id, now=T0)
        self.assertEqual(self.svc.repo.get_rule_version(v.version_id).status, states.RULE_ISSUED)

    def test_illegal_transitions(self) -> None:
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v1())
        self.svc.submit_rule_version(actor=QE, version_id=v.version_id)
        with self.assertRaises(ConflictError):
            self.svc.submit_rule_version(actor=QE, version_id=v.version_id)


class ResolutionAndFreezeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()
        v1 = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A",
            rows=rows_v1(), change_summary="旧表")
        self.v1 = v1.version_id
        self.svc.issue_rule_version(actor=QE, version_id=self.v1, now=T0)

    def test_receipt_before_any_issued_version_fails(self) -> None:
        with self.assertRaises(NotFoundError):
            self.svc.receive_goods(
                actor=WH, batch_no="B-NEW", material_category="铸件", supplier_grade="B",
                supplier_id="S-9", lot_qty=100, received_at=T0)

    def test_unique_resolution_and_sample_size_explanation(self) -> None:
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-1001", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(days=1))
        self.assertEqual(plan.base_sample_size, 20)  # 51..200 行
        self.assertEqual(plan.final_sample_size, 20)
        self.assertEqual(plan.code_letter, "F")  # 91..150 / II
        self.assertEqual(plan.rule_version_id, self.v1)
        explanation = self.svc.explain_plan(plan.plan_id)
        steps = [t["step"] for t in explanation["rule_resolution"]]
        self.assertIn("resolve:effective_window", steps)
        self.assertIn("resolve:lot_row", steps)
        self.assertEqual(explanation["sample_size_calculation"]["final_sample_size"], 20)
        # 快照内容
        self.assertEqual(explanation["rule_version_snapshot"]["row"]["lot_min"], 51)

    def test_completed_plan_does_not_change_with_new_rules(self) -> None:
        # 旧表下完成一批 120 件 → 抽 20
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-OLD", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(days=1))
        self.svc.start_plan(actor=WH, plan_id=plan.plan_id, now=T0 + timedelta(days=1, hours=1))
        self.svc.record_result(
            actor=WH, plan_id=plan.plan_id, inspected_count=20, defect_count=0,
            defect_grades=[], disposition="合格入库", now=T0 + timedelta(days=2))

        # 质量部门发布新表并下达（旧版本自动关闭）
        v2 = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A",
            rows=rows_v2(), change_summary="加严抽样比例")
        switch_at = T0 + timedelta(days=10)
        self.svc.issue_rule_version(actor=QE, version_id=v2.version_id, now=switch_at)
        self.assertEqual(self.svc.repo.get_rule_version(self.v1).status, states.RULE_CLOSED)

        # 已完成计划：样本量、快照、状态全都不变
        frozen = self.svc.repo.get_plan(plan.plan_id)
        self.assertEqual(frozen.status, states.PLAN_CLOSED)
        self.assertEqual(frozen.final_sample_size, 20)
        self.assertEqual(frozen.rule_version_id, self.v1)
        with self.assertRaises(ImmutablePlanError):
            self.svc.request_override(
                actor=WH, plan_id=plan.plan_id, kind=OVERRIDE_TIGHTEN, reason="事后想加严")

        # 新收货按新表解析：120 件 → 抽 32
        plan2 = self.svc.receive_goods(
            actor=WH, batch_no="B-NEW", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=switch_at + timedelta(hours=1))
        self.assertEqual(plan2.base_sample_size, 32)
        self.assertEqual(plan2.rule_version_id, v2.version_id)
        self.assertNotEqual(plan2.rule_version_fingerprint, frozen.rule_version_fingerprint)

    def test_receipt_exactly_at_version_boundary(self) -> None:
        # 新表在 switch_at 下达；旧表窗 [issued, closed_at=switch_at) 为半开区间
        v2 = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v2())
        switch_at = T0 + timedelta(days=30)
        self.svc.issue_rule_version(actor=QE, version_id=v2.version_id, now=switch_at)
        at_boundary = self.svc.receive_goods(
            actor=WH, batch_no="B-EDGE", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=switch_at)
        self.assertEqual(at_boundary.rule_version_id, v2.version_id)

    def test_fingerprint_stable_and_stored(self) -> None:
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-1", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=60, received_at=T0)
        fp = plan.rule_version_fingerprint
        # 计划中固化的指纹与版本当前指纹一致；指纹是确定性的
        version = self.svc.repo.get_rule_version(self.v1)
        self.assertEqual(version.fingerprint(), fp)
        self.assertEqual(version.fingerprint(), fp)


class OverrideTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v1())
        self.svc.issue_rule_version(actor=QE, version_id=v.version_id, now=T0)
        self.plan = self.svc.receive_goods(
            actor=WH, batch_no="B-OV", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(days=1))

    def test_emergency_tightening_requires_approval_to_take_effect(self) -> None:
        ov = self.svc.request_override(
            actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_TIGHTEN,
            reason="客户投诉批次异常", multiply=1.5, now=T0 + timedelta(days=1, hours=2))
        self.assertEqual(self.plan.final_sample_size, 20)  # 未批准不生效
        self.svc.approve_override(
            actor=QE, override_id=ov.override_id, comment="同意加倍抽样",
            now=T0 + timedelta(days=1, hours=3))
        self.assertEqual(self.plan.final_sample_size, 30)  # ceil(20*1.5)
        explanation = self.svc.explain_plan(self.plan.plan_id)
        self.assertIn("紧急加严", explanation["overrides"][0]["effect"])
        self.assertEqual(explanation["overrides"][0]["sample_size_after_apply"], 30)
        last = explanation["sample_size_calculation"]["recompute_history"][-1]
        self.assertIn("ceil(20×1.5)+0=30", last["steps"][-1]["detail"])

    def test_exemption_sets_sample_to_zero_and_is_audited(self) -> None:
        ov = self.svc.request_override(
            actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_EXEMPT,
            reason="随货附第三方全检报告")
        self.svc.approve_override(actor=QE, override_id=ov.override_id, comment="报告有效，豁免")
        self.assertEqual(self.plan.final_sample_size, 0)
        trail = self.svc.explain_plan(self.plan.plan_id)["audit_trail"]
        actions = [e["action"] for e in trail]
        self.assertIn("override.request.exemption", actions)
        self.assertIn("override.approve.exemption", actions)
        # 豁免计划完成时实抽 0 是允许的
        self.svc.start_plan(actor=WH, plan_id=self.plan.plan_id)
        self.svc.record_result(
            actor=WH, plan_id=self.plan.plan_id, inspected_count=0, defect_count=0,
            defect_grades=[], disposition="合格入库")

    def test_revoke_restores_sample_size(self) -> None:
        ov = self.svc.request_override(
            actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_TIGHTEN,
            reason="临时加严", multiply=2.0)
        self.svc.approve_override(actor=QE, override_id=ov.override_id, comment="同意")
        self.assertEqual(self.plan.final_sample_size, 40)
        self.svc.revoke_override(actor=QE, override_id=ov.override_id, reason="信息核实为误报")
        self.assertEqual(self.plan.final_sample_size, 20)
        statuses = [o["status"] for o in self.svc.explain_plan(self.plan.plan_id)["overrides"]]
        self.assertEqual(statuses, ["已撤销"])

    def test_override_locked_after_inspection_starts(self) -> None:
        self.svc.start_plan(actor=WH, plan_id=self.plan.plan_id)
        with self.assertRaises(ImmutablePlanError):
            self.svc.request_override(
                actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_TIGHTEN, reason="迟来的加严")

    def test_tightening_multiplier_below_one_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.request_override(
                actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_TIGHTEN,
                reason="伪装加严", multiply=0.5)

    def test_double_approval_rejected(self) -> None:
        ov = self.svc.request_override(
            actor=WH, plan_id=self.plan.plan_id, kind=OVERRIDE_EXEMPT, reason="x")
        self.svc.approve_override(actor=QE, override_id=ov.override_id)
        with self.assertRaises(ConflictError):
            self.svc.approve_override(actor=QE, override_id=ov.override_id)


class SplitBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v1())
        self.svc.issue_rule_version(actor=QE, version_id=v.version_id, now=T0)
        self.parent = self.svc.receive_goods(
            actor=WH, batch_no="B-SPLIT", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=1000, received_at=T0 + timedelta(days=1))
        self.assertEqual(self.parent.final_sample_size, 50)

    def test_split_closes_parent_and_creates_children(self) -> None:
        children = self.svc.split_batch(
            actor=WH, parent_plan_id=self.parent.plan_id, child_lots=[120, 880])
        self.assertEqual(len(children), 2)
        parent = self.svc.repo.get_plan(self.parent.plan_id)
        self.assertEqual(parent.status, states.PLAN_CLOSED)
        self.assertEqual(parent.close_reason, states.CLOSE_SPLIT)
        self.assertEqual([c.lot_qty for c in children], [120, 880])
        self.assertEqual(children[0].base_sample_size, 20)   # 51..200
        self.assertEqual(children[1].base_sample_size, 50)   # 201..∞
        self.assertEqual(children[0].parent_plan_id, parent.plan_id)
        self.assertEqual(parent.child_plan_ids, [c.plan_id for c in children])
        # 父计划已冻结，不能再动
        with self.assertRaises(ImmutablePlanError):
            self.svc.start_plan(actor=WH, plan_id=parent.plan_id)

    def test_split_requires_quantities_to_sum(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.split_batch(
                actor=WH, parent_plan_id=self.parent.plan_id, child_lots=[100, 800])

    def test_batch_history_shows_lineage(self) -> None:
        self.svc.split_batch(
            actor=WH, parent_plan_id=self.parent.plan_id, child_lots=[120, 880])
        history = self.svc.batch_history("B-SPLIT")
        # 父批次 + 两个 B-SPLIT-S1/S2
        self.assertEqual(len(history["plans"]), 3)


class ResultIntegrityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A", rows=rows_v1())
        self.svc.issue_rule_version(actor=QE, version_id=v.version_id, now=T0)
        self.plan = self.svc.receive_goods(
            actor=WH, batch_no="B-R", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0)

    def test_inspected_count_must_match_plan(self) -> None:
        self.svc.start_plan(actor=WH, plan_id=self.plan.plan_id)
        with self.assertRaises(ValidationError):
            self.svc.record_result(
                actor=WH, plan_id=self.plan.plan_id, inspected_count=19, defect_count=0,
                defect_grades=[], disposition="合格入库")

    def test_flow_violation(self) -> None:
        with self.assertRaises(ImmutablePlanError):
            self.svc.record_result(
                actor=WH, plan_id=self.plan.plan_id, inspected_count=20, defect_count=0,
                defect_grades=[], disposition="合格入库")


if __name__ == "__main__":
    unittest.main()
