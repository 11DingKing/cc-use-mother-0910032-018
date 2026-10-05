"""缺陷分级版本化与判定一致性测试。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inspection_service import InspectionService
from inspection_service.errors import ConflictError, ValidationError

QE = "质量工程师-张敏"
WH = "仓储管理员-李库"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)

ROWS = [
    {"lot_min": 1, "lot_max": 200, "base_sample_size": 20, "inspection_level": "II"},
    {"lot_min": 201, "lot_max": None, "base_sample_size": 50, "inspection_level": "II"},
]

# 旧分级：严重缺陷 Ac=1；新分级：严重缺陷 Ac=0（质量部门收紧分级）
DEFECTS_V1 = [
    {"code": "CR", "name": "致命", "accept_number": 0, "reject_number": 1},
    {"code": "MA", "name": "严重", "accept_number": 1, "reject_number": 2},
]
DEFECTS_V2 = [
    {"code": "CR", "name": "致命", "accept_number": 0, "reject_number": 1},
    {"code": "MA", "name": "严重", "accept_number": 0, "reject_number": 1},
]


class DefectClassificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = InspectionService()

    def _issue(self, defects) -> str:
        v = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A",
            rows=ROWS, defect_classes=defects, change_summary="分级调整")
        self.svc.issue_rule_version(actor=QE, version_id=v.version_id, now=T0)
        return v.version_id

    def test_default_classes_when_omitted(self) -> None:
        v = self.svc.create_rule_version(
            actor=QE, material_category="X", supplier_grade="A", rows=ROWS)
        self.assertEqual([d.code for d in v.defect_classes], ["CR", "MA", "MI"])

    def test_duplicate_class_code_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.create_rule_version(
                actor=QE, material_category="Y", supplier_grade="A", rows=ROWS,
                defect_classes=DEFECTS_V1 + [{"code": "CR", "name": "x",
                                              "accept_number": 0, "reject_number": 1}])

    def test_class_verdict_enforced_and_snapshotted(self) -> None:
        vid = self._issue(DEFECTS_V1)
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-D1", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(hours=1))
        # 计划快照带分级
        self.assertEqual([d["code"] for d in plan.defect_classes_snapshot], ["CR", "MA"])
        self.svc.start_plan(actor=WH, plan_id=plan.plan_id)

        # 1 个严重缺陷：旧分级 MA Ac=1 → 通过，允许合格入库
        done = self.svc.record_result(
            actor=WH, plan_id=plan.plan_id, inspected_count=20,
            defect_counts_by_class={"CR": 0, "MA": 1}, disposition="合格入库")
        verdicts = {v["code"]: v["verdict"] for v in done.class_verdicts}
        self.assertEqual(verdicts, {"CR": "通过", "MA": "通过"})

    def test_new_classification_applies_only_to_new_receipts(self) -> None:
        self._issue(DEFECTS_V1)
        # 旧分级下完成的批次（MA=1 合格入库）
        old = self.svc.receive_goods(
            actor=WH, batch_no="B-OLD", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(hours=1))
        self.svc.start_plan(actor=WH, plan_id=old.plan_id)
        self.svc.record_result(
            actor=WH, plan_id=old.plan_id, inspected_count=20,
            defect_counts_by_class={"CR": 0, "MA": 1}, disposition="合格入库")

        # 质量部门收紧严重分级并下达新版本
        v2 = self.svc.create_rule_version(
            actor=QE, material_category="电子元件", supplier_grade="A",
            rows=ROWS, defect_classes=DEFECTS_V2, change_summary="严重缺陷零容忍")
        switch = T0 + timedelta(days=5)
        self.svc.issue_rule_version(actor=QE, version_id=v2.version_id, now=switch)

        new = self.svc.receive_goods(
            actor=WH, batch_no="B-NEW", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=switch + timedelta(hours=1))
        self.assertEqual(new.defect_classes_snapshot, DEFECTS_V2)
        self.svc.start_plan(actor=WH, plan_id=new.plan_id)
        # 新分级下 MA=1 即不通过，禁止合格入库
        with self.assertRaises(ConflictError) as ctx:
            self.svc.record_result(
                actor=WH, plan_id=new.plan_id, inspected_count=20,
                defect_counts_by_class={"CR": 0, "MA": 1}, disposition="合格入库")
        self.assertIn("MA", str(ctx.exception.details))
        # 改为让步接收可以关闭
        done = self.svc.record_result(
            actor=WH, plan_id=new.plan_id, inspected_count=20,
            defect_counts_by_class={"CR": 0, "MA": 1}, disposition="让步接收")
        self.assertEqual(done.status, "已关闭")

        # 历史计划仍按旧分级留痕，未被新版本改写
        frozen = self.svc.repo.get_plan(old.plan_id)
        self.assertEqual(frozen.defect_classes_snapshot, DEFECTS_V1)

    def test_unknown_class_rejected(self) -> None:
        self._issue(DEFECTS_V1)
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-D2", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(hours=1))
        self.svc.start_plan(actor=WH, plan_id=plan.plan_id)
        with self.assertRaises(ValidationError):
            self.svc.record_result(
                actor=WH, plan_id=plan.plan_id, inspected_count=20,
                defect_counts_by_class={"CR": 0, "XX": 1}, disposition="全检")

    def test_total_must_match_class_breakdown(self) -> None:
        self._issue(DEFECTS_V1)
        plan = self.svc.receive_goods(
            actor=WH, batch_no="B-D3", material_category="电子元件", supplier_grade="A",
            supplier_id="S-1", lot_qty=120, received_at=T0 + timedelta(hours=1))
        self.svc.start_plan(actor=WH, plan_id=plan.plan_id)
        with self.assertRaises(ValidationError):
            self.svc.record_result(
                actor=WH, plan_id=plan.plan_id, inspected_count=20, defect_count=2,
                defect_counts_by_class={"CR": 0, "MA": 1}, disposition="全检")


if __name__ == "__main__":
    unittest.main()
