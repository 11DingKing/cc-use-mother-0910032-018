"""HTTP API 端到端测试（标准库 http.client，真实起停服务）。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inspection_service.api import build_server
from inspection_service.service import InspectionService

QE_HEADERS = {"X-Actor": "质量工程师-张敏", "X-Role": "质量工程师"}
WH_HEADERS = {"X-Actor": "仓储管理员-李库", "X-Role": "warehouse"}

ROWS = [
    {"lot_min": 1, "lot_max": 50, "base_sample_size": 5, "inspection_level": "II",
     "accept_number": 0, "reject_number": 1},
    {"lot_min": 51, "lot_max": 200, "base_sample_size": 20, "inspection_level": "II",
     "accept_number": 1, "reject_number": 2},
    {"lot_min": 201, "lot_max": None, "base_sample_size": 50, "inspection_level": "II",
     "accept_number": 3, "reject_number": 4},
]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.svc = InspectionService()
        cls.server = build_server(0, cls.svc)  # 端口 0 = 系统分配
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        merged = {"Content-Type": "application/json", **(headers or {})}
        # HTTP 头只能是 latin-1：X-Actor/X-Role 的非 ASCII 值按 RFC 3986 编码
        for key in ("X-Actor", "X-Role"):
            if key in merged:
                merged[key] = quote(str(merged[key]), safe="")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        conn.request(method, path, body=data, headers=merged)
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8")
        conn.close()
        payload = json.loads(raw) if raw else {}
        return resp.status, payload

    def test_full_flow_with_role_boundaries(self) -> None:
        # 仓储不能建规则
        status, payload = self.call("POST", "/api/rule-versions",
                                    {"material_category": "电子元件", "supplier_grade": "A", "rows": ROWS},
                                    WH_HEADERS)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

        # 缺 actor
        status, _ = self.call("POST", "/api/rule-versions",
                              {"material_category": "X", "supplier_grade": "A", "rows": ROWS},
                              {"X-Role": "质量工程师"})
        self.assertEqual(status, 401)

        # 质量工程师建表并下达
        status, version = self.call("POST", "/api/rule-versions",
                                    {"material_category": "电子元件", "supplier_grade": "A",
                                     "rows": ROWS, "change_summary": "初始表"}, QE_HEADERS)
        self.assertEqual(status, 201)
        vid = version["version_id"]
        self.assertEqual(version["status"], "草拟")
        status, _ = self.call("POST", f"/api/rule-versions/{vid}",
                              {"action": "submit"}, QE_HEADERS)
        self.assertEqual(status, 200)
        status, issued = self.call("POST", f"/api/rule-versions/{vid}",
                                   {"action": "issue"}, QE_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(issued["status"], "已下达")

        # 区间重叠被 409 拒绝
        bad_rows = [dict(r) for r in ROWS]
        bad_rows[1] = {**bad_rows[1], "lot_min": 40}
        status, payload = self.call("POST", "/api/rule-versions",
                                    {"material_category": "紧固件", "supplier_grade": "B", "rows": bad_rows},
                                    QE_HEADERS)
        self.assertEqual(status, 409)
        self.assertTrue(payload["details"]["problems"])

        # 收货生成计划
        status, plan = self.call("POST", "/api/goods-receipts",
                                 {"batch_no": "B-API-1", "material_category": "电子元件",
                                  "supplier_grade": "A", "supplier_id": "S-1", "lot_qty": 120},
                                 WH_HEADERS)
        self.assertEqual(status, 201)
        pid = plan["plan_id"]
        self.assertEqual(plan["final_sample_size"], 20)
        self.assertEqual(plan["rule_version_id"], vid)

        # 加严申请（仓储）→ 批准（质量）→ 样本量变化
        status, ov = self.call("POST", f"/api/plans/{pid}",
                               {"action": "request-tightening", "reason": "客户投诉", "multiply": 1.5},
                               WH_HEADERS)
        self.assertEqual(status, 201)
        # 仓储不能批准
        status, _ = self.call("POST", f"/api/overrides/{ov['override_id']}",
                              {"action": "approve", "comment": "同意"}, WH_HEADERS)
        self.assertEqual(status, 401)
        status, approved = self.call("POST", f"/api/overrides/{ov['override_id']}",
                                     {"action": "approve", "comment": "同意加倍"}, QE_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(approved["status"], "已批准")

        status, plan2 = self.call("GET", f"/api/plans/{pid}")
        self.assertEqual(plan2["final_sample_size"], 30)

        # 解释接口
        status, explanation = self.call("GET", f"/api/plans/{pid}/explanation")
        self.assertEqual(status, 200)
        self.assertEqual(explanation["sample_size_calculation"]["final_sample_size"], 30)
        self.assertTrue(explanation["overrides"])
        self.assertTrue(explanation["audit_trail"])

        # 开始 → 完成
        self.assertEqual(self.call("POST", f"/api/plans/{pid}", {"action": "start"}, WH_HEADERS)[0], 200)
        status, done = self.call("POST", f"/api/plans/{pid}",
                                 {"action": "complete", "inspected_count": 30, "defect_count": 1,
                                  "defect_grades": ["轻微"], "disposition": "让步接收"}, WH_HEADERS)
        self.assertEqual(status, 200)
        self.assertEqual(done["status"], "已关闭")

        # 完成后再覆盖 → 409
        status, payload = self.call("POST", f"/api/plans/{pid}",
                                    {"action": "request-exemption", "reason": "事后豁免"}, WH_HEADERS)
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"], "plan_immutable")

    def test_split_flow(self) -> None:
        status, version = self.call("POST", "/api/rule-versions",
                                    {"material_category": "铸件", "supplier_grade": "B", "rows": ROWS},
                                    QE_HEADERS)
        vid = version["version_id"]
        self.call("POST", f"/api/rule-versions/{vid}", {"action": "issue"}, QE_HEADERS)
        _, plan = self.call("POST", "/api/goods-receipts",
                            {"batch_no": "B-SPLIT-API", "material_category": "铸件",
                             "supplier_grade": "B", "supplier_id": "S-7", "lot_qty": 1000},
                            WH_HEADERS)
        status, children = self.call("POST", f"/api/plans/{plan['plan_id']}",
                                     {"action": "split", "child_lots": [120, 880]}, WH_HEADERS)
        self.assertEqual(status, 201)
        self.assertEqual([c["lot_qty"] for c in children], [120, 880])
        self.assertEqual(children[0]["final_sample_size"], 20)
        self.assertEqual(children[1]["final_sample_size"], 50)

        status, history = self.call("GET", "/api/batches?batch_no=B-SPLIT-API")
        self.assertEqual(status, 200)
        self.assertEqual(len(history["plans"]), 3)

    def test_audit_stream(self) -> None:
        status, events = self.call("GET", "/api/audit")
        self.assertEqual(status, 200)
        self.assertIsInstance(events, list)
        seqs = [e["seq"] for e in events]
        self.assertEqual(seqs, sorted(seqs))


if __name__ == "__main__":
    unittest.main()
