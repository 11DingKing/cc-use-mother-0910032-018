"""抽样规则版本与检验计划服务的回归测试。

覆盖契约四条不变量：抽样规则时态、区间重叠检测、检验计划冻结、人工覆盖审计。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sampling_service.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from sampling_service.service import SamplingService, allocate_samples
from sampling_service.store import JsonStore

QE = "质量工程师"
WK = "仓储管理员"
PLANNER = "采购计划员"


class FakeClock:
    def __init__(self, start: str = "2026-01-01T08:00:00+00:00"):
        self.moment = datetime.fromisoformat(start)

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, **kwargs) -> None:
        self.moment += timedelta(**kwargs)


def standard_rules(ratio: float = 0.05) -> list[dict]:
    return [
        {"material_category": "电子料", "supplier_level": "A", "lot_min": 1, "lot_max": 100,
         "sample_ratio": 0.10, "min_sample": 5, "max_sample": 50,
         "defect_criteria": {"critical": {"ac": 0}, "major": {"ac": 1}}},
        {"material_category": "电子料", "supplier_level": "A", "lot_min": 101, "lot_max": 1000,
         "sample_ratio": ratio, "min_sample": 10, "max_sample": 40,
         "defect_criteria": {"critical": {"ac": 0}, "major": {"ac": 1}}},
        {"material_category": "电子料", "supplier_level": "A", "lot_min": 1001, "lot_max": None,
         "sample_ratio": 0.02, "min_sample": 20, "max_sample": 100,
         "defect_criteria": {"critical": {"ac": 0}}},
        {"material_category": "电子料", "supplier_level": "B", "lot_min": 1, "lot_max": None,
         "sample_ratio": 0.20, "min_sample": 10, "max_sample": None,
         "defect_criteria": {"critical": {"ac": 0}}},
    ]


class ServiceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.service = SamplingService(JsonStore(), clock=self.clock)

    def release_version(self, rules: list[dict] | None = None, title: str = "标准规则") -> dict:
        version = self.service.create_rule_version(
            actor="qe1", role=QE, title=title, rules=rules or standard_rules())
        self.service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
        return self.service.release_rule_version(actor="qe1", role=QE, version_id=version["version_id"])

    def receipt(self, batch_id: str = "B-1", quantity: int = 500,
                category: str = "电子料", level: str = "A", **kwargs) -> dict:
        return self.service.create_receipt(
            actor="wh1", role=WK, batch_id=batch_id, material_category=category,
            supplier_level=level, quantity=quantity, **kwargs)


class RuleVersionTest(ServiceTestCase):
    def test_version_lifecycle_keeps_single_released(self) -> None:
        v1 = self.release_version()
        self.assertEqual(v1["status"], "已下达")
        self.assertIsNotNone(v1["effective_from"])
        self.clock.advance(hours=1)
        v2 = self.release_version(standard_rules(0.10), title="加严版")
        self.assertEqual(v2["status"], "已下达")
        old = self.service.get_rule_version(v1["version_id"])
        self.assertEqual(old["status"], "已关闭")
        self.assertEqual(old["superseded_at"], v2["effective_from"])
        released = self.service.list_rule_versions(status="已下达")
        self.assertEqual([v["version_id"] for v in released], [v2["version_id"]])

    def test_submit_requires_draft(self) -> None:
        version = self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=standard_rules())
        self.service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
        self.assertEqual(ctx.exception.code, "VERSION_STATE_INVALID")

    def test_release_requires_pending(self) -> None:
        version = self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=standard_rules())
        with self.assertRaises(ConflictError):
            self.service.release_rule_version(actor="qe1", role=QE, version_id=version["version_id"])

    def test_overlap_rejected_on_submit(self) -> None:
        rules = standard_rules() + [
            {"material_category": "电子料", "supplier_level": "A", "lot_min": 50, "lot_max": 200,
             "sample_ratio": 0.5, "min_sample": 1, "max_sample": None,
             "defect_criteria": {"critical": {"ac": 0}}},
        ]
        version = self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=rules)
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
        self.assertEqual(ctx.exception.code, "RULE_OVERLAP")
        self.assertTrue(ctx.exception.details["conflicts"])

    def test_overlap_with_unbounded_range_rejected(self) -> None:
        rules = [
            {"material_category": "电子料", "supplier_level": "B", "lot_min": 1, "lot_max": None,
             "sample_ratio": 0.2, "min_sample": 1, "max_sample": None,
             "defect_criteria": {"critical": {"ac": 0}}},
            {"material_category": "电子料", "supplier_level": "B", "lot_min": 500, "lot_max": 600,
             "sample_ratio": 0.2, "min_sample": 1, "max_sample": None,
             "defect_criteria": {"critical": {"ac": 0}}},
        ]
        version = self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=rules)
        with self.assertRaises(ConflictError) as ctx:
            self.service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
        self.assertEqual(ctx.exception.code, "RULE_OVERLAP")

    def test_adjacent_ranges_are_allowed(self) -> None:
        self.release_version()  # 1-100 / 101-1000 / 1001-∞ 相邻不重叠，应正常下达

    def test_invalid_rule_fields_rejected(self) -> None:
        bad = [{"material_category": "电子料", "supplier_level": "A", "lot_min": 10, "lot_max": 5,
                "sample_ratio": 0.1, "min_sample": 1, "max_sample": None,
                "defect_criteria": {"critical": {"ac": 0}}}]
        with self.assertRaises(ValidationError):
            self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=bad)
        bad_ratio = standard_rules()
        bad_ratio[0]["sample_ratio"] = 1.5
        with self.assertRaises(ValidationError):
            self.service.create_rule_version(actor="qe1", role=QE, title="t", rules=bad_ratio)


class SampleCalculationTest(ServiceTestCase):
    def test_ratio_ceil_and_bounds(self) -> None:
        self.release_version()
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 25)   # ceil(500×0.05)
        self.assertEqual(self.receipt("B-2", 80)["sample_size"], 8)     # ceil(80×0.10)
        self.assertEqual(self.receipt("B-3", 5)["sample_size"], 5)      # 最小样本量约束
        self.assertEqual(self.receipt("B-4", 10000)["sample_size"], 100)  # 最大样本量约束
        self.assertEqual(self.receipt("B-5", 1000)["sample_size"], 40)  # ceil(50) 被 max 40 截断

    def test_sample_clamped_to_lot_size(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 3)  # min_sample 5 超过批量 3
        self.assertEqual(plan["sample_size"], 3)
        steps = [s["step"] for s in plan["steps"]]
        self.assertIn("不超过批量", steps)

    def test_explanation_shows_formula_and_version_window(self) -> None:
        version = self.release_version()
        plan = self.receipt("B-1", 500)
        explanation = self.service.explain_plan(plan["plan_id"])
        self.assertEqual(explanation["final_sample_size"], 25)
        self.assertEqual(explanation["rule_version_id"], version["version_id"])
        self.assertEqual(explanation["version_window"]["effective_from"], version["effective_from"])
        self.assertIsNone(explanation["version_window"]["superseded_at"])
        self.assertEqual(explanation["steps"][0]["formula"], "ceil(500 × 0.05)")
        self.assertEqual(explanation["inputs"]["lot_size"], 500)
        self.assertEqual(explanation["defect_criteria"], {"critical": {"ac": 0}, "major": {"ac": 1}})


class TemporalRuleTest(ServiceTestCase):
    def test_completed_plan_frozen_after_new_version(self) -> None:
        v1 = self.release_version(standard_rules(0.05))
        plan = self.receipt("B-1", 500)
        self.assertEqual(plan["sample_size"], 25)
        self.service.complete_plan(actor="wh1", role=WK, plan_id=plan["plan_id"])
        self.clock.advance(hours=1)
        self.release_version(standard_rules(0.10), "新比例")
        after = self.service.get_plan(plan["plan_id"])
        self.assertEqual(after["sample_size"], 25)
        self.assertEqual(after["rule_version_id"], v1["version_id"])
        self.assertEqual(after["status"], "已关闭")

    def test_new_receipt_uses_new_version(self) -> None:
        self.release_version(standard_rules(0.05))
        self.receipt("B-1", 500)
        self.clock.advance(hours=1)
        v2 = self.release_version(standard_rules(0.10), "新比例")
        plan = self.receipt("B-2", 500)
        self.assertEqual(plan["rule_version_id"], v2["version_id"])
        self.assertEqual(plan["sample_size"], 40)  # ceil(500×0.10)=50 被 max 40 截断

    def test_backdated_receipt_resolves_historical_version(self) -> None:
        v1 = self.release_version(standard_rules(0.05))
        t1 = self.clock.moment.isoformat()
        self.clock.advance(hours=2)
        self.release_version(standard_rules(0.10), "新比例")
        plan = self.receipt("B-1", 500, received_at=t1)
        self.assertEqual(plan["rule_version_id"], v1["version_id"])
        self.assertEqual(plan["sample_size"], 25)

    def test_receipt_before_any_version_rejected(self) -> None:
        with self.assertRaises(NotFoundError) as ctx:
            self.receipt("B-1", 500)
        self.assertEqual(ctx.exception.code, "NO_EFFECTIVE_VERSION")

    def test_rule_not_found(self) -> None:
        self.release_version()
        with self.assertRaises(NotFoundError) as ctx:
            self.receipt("B-1", 500, category="五金件")
        self.assertEqual(ctx.exception.code, "RULE_NOT_FOUND")

    def test_duplicate_batch_rejected(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 500)
        with self.assertRaises(ConflictError) as ctx:
            self.receipt("B-1", 500)
        self.assertEqual(ctx.exception.code, "DUPLICATE_BATCH")
        self.assertEqual(ctx.exception.details["plan_id"], plan["plan_id"])


class TighteningTest(ServiceTestCase):
    def test_tightening_applies_to_new_plans_only(self) -> None:
        self.release_version()
        before = self.receipt("B-1", 500)
        tightening = self.service.create_tightening(
            actor="qe1", role=QE, material_category="电子料", multiplier=2.0, reason="客户投诉")
        after = self.receipt("B-2", 500)
        self.assertEqual(before["sample_size"], 25)
        self.assertEqual(after["sample_size"], 50)
        self.assertEqual(after["tightening_ids"], [tightening["tightening_id"]])
        steps = [s["step"] for s in after["steps"]]
        self.assertIn("紧急加严", steps)
        # 已生成的计划不受加严影响
        self.assertEqual(self.service.get_plan(before["plan_id"])["sample_size"], 25)

    def test_tightening_revoke_stops_applying(self) -> None:
        self.release_version()
        tightening = self.service.create_tightening(
            actor="qe1", role=QE, material_category="电子料", multiplier=2.0, reason="r")
        self.service.revoke_tightening(actor="qe1", role=QE, tightening_id=tightening["tightening_id"])
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 25)

    def test_tightening_level_scoped(self) -> None:
        self.release_version()
        self.service.create_tightening(
            actor="qe1", role=QE, material_category="电子料", supplier_level="B",
            multiplier=2.0, reason="B 级供应商质量波动")
        self.assertEqual(self.receipt("B-1", 500, level="A")["sample_size"], 25)
        self.assertEqual(self.receipt("B-2", 500, level="B")["sample_size"], 200)

    def test_tightening_multiplier_must_exceed_one(self) -> None:
        with self.assertRaises(ValidationError):
            self.service.create_tightening(
                actor="qe1", role=QE, material_category="电子料", multiplier=0.5, reason="r")


class ExemptionTest(ServiceTestCase):
    def test_pending_exemption_not_applied_until_approved(self) -> None:
        self.release_version()
        exemption = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", supplier_level="A",
            mode="skip", reason="供应商免检认证")
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 25)
        self.service.approve_exemption(actor="qe2", role=QE, exemption_id=exemption["exemption_id"])
        plan = self.receipt("B-2", 500)
        self.assertEqual(plan["sample_size"], 0)
        self.assertEqual(plan["exemption_ids"], [exemption["exemption_id"]])
        self.assertIn("豁免批准", [s["step"] for s in plan["steps"]])

    def test_reduce_exemption(self) -> None:
        self.release_version()
        exemption = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", supplier_level="A",
            mode="reduce", reduce_factor=0.5, reason="过程能力稳定")
        self.service.approve_exemption(actor="qe2", role=QE, exemption_id=exemption["exemption_id"])
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 13)  # ceil(25×0.5)

    def test_expired_exemption_not_applied(self) -> None:
        self.release_version()
        expires = (self.clock.moment + timedelta(hours=1)).isoformat()
        exemption = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", supplier_level="A",
            mode="skip", reason="r", expires_at=expires)
        self.service.approve_exemption(actor="qe2", role=QE, exemption_id=exemption["exemption_id"])
        self.clock.advance(hours=2)
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 25)

    def test_batch_specific_exemption_wins(self) -> None:
        self.release_version()
        general = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", supplier_level="A",
            mode="reduce", reduce_factor=0.5, reason="类别减量")
        specific = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", batch_id="B-9",
            mode="skip", reason="该批次客户自验")
        for e in (general, specific):
            self.service.approve_exemption(actor="qe2", role=QE, exemption_id=e["exemption_id"])
        self.assertEqual(self.receipt("B-9", 500)["sample_size"], 0)
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 13)

    def test_self_approval_rejected(self) -> None:
        exemption = self.service.request_exemption(
            actor="qe1", role=QE, material_category="电子料", mode="skip", reason="r")
        with self.assertRaises(ConflictError) as ctx:
            self.service.approve_exemption(actor="qe1", role=QE, exemption_id=exemption["exemption_id"])
        self.assertEqual(ctx.exception.code, "EXEMPTION_SELF_APPROVAL")

    def test_rejected_exemption_not_applied(self) -> None:
        self.release_version()
        exemption = self.service.request_exemption(
            actor="p1", role=PLANNER, material_category="电子料", mode="skip", reason="r")
        self.service.reject_exemption(actor="qe2", role=QE, exemption_id=exemption["exemption_id"])
        self.assertEqual(self.receipt("B-1", 500)["sample_size"], 25)


class OverrideTest(ServiceTestCase):
    def test_override_is_audited(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 500)
        updated = self.service.override_plan(
            actor="qe1", role=QE, plan_id=plan["plan_id"], new_sample_size=7, reason="客户要求全检改抽检")
        self.assertEqual(updated["sample_size"], 7)
        audit = self.service.plan_audit(plan["plan_id"])
        self.assertEqual(len(audit["overrides"]), 1)
        record = audit["overrides"][0]
        self.assertEqual((record["old_sample_size"], record["new_sample_size"]), (25, 7))
        self.assertEqual(record["actor"], "qe1")
        explanation = self.service.explain_plan(plan["plan_id"])
        self.assertEqual(explanation["steps"][-1]["step"], "人工覆盖")
        self.assertEqual(explanation["final_sample_size"], 7)

    def test_override_bounds_validated(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 500)
        with self.assertRaises(ValidationError) as ctx:
            self.service.override_plan(actor="qe1", role=QE, plan_id=plan["plan_id"],
                                       new_sample_size=501, reason="r")
        self.assertEqual(ctx.exception.code, "OVERRIDE_OUT_OF_RANGE")
        with self.assertRaises(ValidationError):
            self.service.override_plan(actor="qe1", role=QE, plan_id=plan["plan_id"],
                                       new_sample_size=10, reason="  ")

    def test_override_rejected_after_close(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 500)
        self.service.complete_plan(actor="wh1", role=WK, plan_id=plan["plan_id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.override_plan(actor="qe1", role=QE, plan_id=plan["plan_id"],
                                       new_sample_size=10, reason="r")
        self.assertEqual(ctx.exception.code, "PLAN_CLOSED")

    def test_plan_lifecycle(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 500)
        self.assertEqual(plan["status"], "已下达")
        started = self.service.start_plan(actor="wh1", role=WK, plan_id=plan["plan_id"])
        self.assertEqual(started["status"], "履行中")
        closed = self.service.complete_plan(actor="qe1", role=QE, plan_id=plan["plan_id"], note="判定合格")
        self.assertEqual(closed["status"], "已关闭")
        self.assertEqual(closed["close_reason"], "判定合格")
        with self.assertRaises(ConflictError):
            self.service.complete_plan(actor="wh1", role=WK, plan_id=plan["plan_id"])


class SplitTest(ServiceTestCase):
    def test_split_allocates_parent_sample_and_inherits_version(self) -> None:
        v1 = self.release_version(standard_rules(0.05))
        plan = self.receipt("B-1", 1000)
        self.assertEqual(plan["sample_size"], 40)
        result = self.service.split_batch(actor="wh1", role=WK, batch_id="B-1", quantities=[600, 400])
        children = result["children"]
        self.assertEqual([c["batch_id"] for c in children], ["B-1-S1", "B-1-S2"])
        self.assertEqual([c["sample_size"] for c in children], [24, 16])
        self.assertEqual(sum(c["sample_size"] for c in children), 40)
        for child in children:
            self.assertEqual(child["rule_version_id"], v1["version_id"])
            self.assertEqual(child["parent_plan_id"], plan["plan_id"])
            self.assertEqual(child["steps"][0]["step"], "批次拆分继承")
        parent = self.service.get_plan(plan["plan_id"])
        self.assertEqual(parent["status"], "已关闭")
        self.assertEqual(parent["close_reason"], "批次拆分")
        # 新规则版本发布后，子计划仍冻结在原版本
        self.clock.advance(hours=1)
        self.release_version(standard_rules(0.10), "新比例")
        for child in children:
            self.assertEqual(self.service.get_plan(child["plan_id"])["sample_size"],
                             child["sample_size"])

    def test_split_quantity_must_sum_to_batch(self) -> None:
        self.release_version()
        self.receipt("B-1", 1000)
        with self.assertRaises(ValidationError) as ctx:
            self.service.split_batch(actor="wh1", role=WK, batch_id="B-1", quantities=[600, 300])
        self.assertEqual(ctx.exception.code, "SPLIT_QUANTITY_MISMATCH")

    def test_split_closed_plan_rejected(self) -> None:
        self.release_version()
        plan = self.receipt("B-1", 1000)
        self.service.complete_plan(actor="wh1", role=WK, plan_id=plan["plan_id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.split_batch(actor="wh1", role=WK, batch_id="B-1", quantities=[600, 400])
        self.assertEqual(ctx.exception.code, "PLAN_CLOSED")

    def test_allocate_samples_properties(self) -> None:
        self.assertEqual(allocate_samples(40, [600, 400]), [24, 16])
        self.assertEqual(allocate_samples(3, [1, 1, 1]), [1, 1, 1])
        alloc = allocate_samples(5, [2, 2, 2])
        self.assertEqual(sum(alloc), 5)
        self.assertTrue(all(a <= 2 for a in alloc))
        self.assertEqual(allocate_samples(0, [10, 10]), [0, 0])


class RoleTest(ServiceTestCase):
    def test_role_enforcement(self) -> None:
        with self.assertRaises(ForbiddenError):
            self.service.create_rule_version(actor="wh1", role=WK, title="t", rules=standard_rules())
        self.release_version()
        with self.assertRaises(ForbiddenError):
            self.service.create_receipt(actor="qe1", role=QE, batch_id="B-1",
                                        material_category="电子料", supplier_level="A", quantity=10)
        with self.assertRaises(ForbiddenError):
            self.service.create_receipt(actor="x", role="实习生", batch_id="B-1",
                                        material_category="电子料", supplier_level="A", quantity=10)
        plan = self.receipt("B-1", 500)
        with self.assertRaises(ForbiddenError):
            self.service.override_plan(actor="wh1", role=WK, plan_id=plan["plan_id"],
                                       new_sample_size=5, reason="r")


class PreviewAndPersistenceTest(ServiceTestCase):
    def test_preview_explains_without_persisting(self) -> None:
        version = self.release_version()
        preview = self.service.preview_plan(actor="qe1", role=QE, material_category="电子料",
                                            supplier_level="A", quantity=500)
        self.assertEqual(preview["sample_size"], 25)
        self.assertEqual(preview["rule_version_id"], version["version_id"])
        self.assertEqual(self.service.list_plans(), [])

    def test_store_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            service = SamplingService(JsonStore(path), clock=self.clock)
            version = service.create_rule_version(actor="qe1", role=QE, title="t", rules=standard_rules())
            service.submit_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
            service.release_rule_version(actor="qe1", role=QE, version_id=version["version_id"])
            plan = service.create_receipt(actor="wh1", role=WK, batch_id="B-1",
                                          material_category="电子料", supplier_level="A", quantity=500)
            reloaded = SamplingService(JsonStore(path), clock=self.clock)
            self.assertEqual(reloaded.get_plan(plan["plan_id"])["sample_size"], 25)
            self.assertEqual(reloaded.get_rule_version(version["version_id"])["status"], "已下达")


if __name__ == "__main__":
    unittest.main()
