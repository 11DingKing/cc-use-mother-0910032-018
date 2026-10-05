"""HTTP API 端到端测试：真实启动服务，走完整业务流程。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sampling_service.api import make_server
from sampling_service.service import SamplingService
from sampling_service.store import JsonStore

QE = "质量工程师"
WK = "仓储管理员"


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = make_server(SamplingService(JsonStore()), "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None,
             actor: str | None = None, role: str | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method)
        data = None
        payload = dict(body) if body is not None else None
        if payload is not None and actor:
            payload["actor"] = actor
        if payload is not None and role:
            payload["role"] = role
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, data=data) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_business_flow(self) -> None:
        # 健康检查
        status, body = self.call("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

        # 缺少操作者身份
        status, body = self.call("POST", "/rule-versions", {"title": "t", "rules": []})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "ACTOR_REQUIRED")

        # 角色不符：仓储管理员不能维护规则
        status, body = self.call("POST", "/rule-versions",
                                 {"title": "t", "rules": []}, actor="wh1", role=WK)
        self.assertEqual(status, 403)

        # 质量工程师发布规则版本
        rules = [{
            "material_category": "电子料", "supplier_level": "A",
            "lot_min": 1, "lot_max": None, "sample_ratio": 0.05,
            "min_sample": 10, "max_sample": 40,
            "defect_criteria": {"critical": {"ac": 0}, "major": {"ac": 1}},
        }]
        status, version = self.call("POST", "/rule-versions",
                                    {"title": "2026Q1 抽样规则", "rules": rules},
                                    actor="qe1", role=QE)
        self.assertEqual(status, 201)
        self.assertEqual(version["status"], "草拟")
        vid = version["version_id"]
        status, version = self.call("POST", f"/rule-versions/{vid}/submit", {}, actor="qe1", role=QE)
        self.assertEqual((status, version["status"]), (200, "待确认"))
        status, version = self.call("POST", f"/rule-versions/{vid}/release", {}, actor="qe1", role=QE)
        self.assertEqual((status, version["status"]), (200, "已下达"))

        # 仓储管理员登记收货，生成检验计划
        status, plan = self.call("POST", "/receipts", {
            "batch_id": "0910032-018-A", "material_category": "电子料",
            "supplier_level": "A", "quantity": 500,
        }, actor="wh1", role=WK)
        self.assertEqual(status, 201)
        self.assertEqual(plan["sample_size"], 25)
        self.assertEqual(plan["rule_version_id"], vid)
        pid = plan["plan_id"]

        # 重复登记同一批次被拒绝
        status, body = self.call("POST", "/receipts", {
            "batch_id": "0910032-018-A", "material_category": "电子料",
            "supplier_level": "A", "quantity": 500,
        }, actor="wh1", role=WK)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "DUPLICATE_BATCH")

        # 样本量计算解释
        status, explanation = self.call("GET", f"/plans/{pid}/explanation")
        self.assertEqual(status, 200)
        self.assertEqual(explanation["final_sample_size"], 25)
        self.assertEqual(explanation["steps"][0]["step"], "按比例计算")

        # 人工覆盖并留痕
        status, plan = self.call("POST", f"/plans/{pid}/overrides",
                                 {"new_sample_size": 30, "reason": "客户要求加严"},
                                 actor="qe2", role=QE)
        self.assertEqual((status, plan["sample_size"]), (200, 30))
        status, audit = self.call("GET", f"/plans/{pid}/audit")
        self.assertEqual(len(audit["overrides"]), 1)
        self.assertEqual(audit["overrides"][0]["reason"], "客户要求加严")

        # 完成后计划冻结，拒绝覆盖
        status, plan = self.call("POST", f"/plans/{pid}/complete", {"note": "判定合格"},
                                 actor="wh1", role=WK)
        self.assertEqual((status, plan["status"]), (200, "已关闭"))
        status, body = self.call("POST", f"/plans/{pid}/overrides",
                                 {"new_sample_size": 10, "reason": "x"}, actor="qe2", role=QE)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "PLAN_CLOSED")

    def test_unknown_route_and_missing_resource(self) -> None:
        status, body = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "ROUTE_NOT_FOUND")
        status, body = self.call("GET", "/plans/IP-9999")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "PLAN_NOT_FOUND")

    def test_invalid_json_body(self) -> None:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/receipts", method="POST",
            data=b"not-json", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                status, body = response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status, body = exc.code, json.loads(exc.read().decode("utf-8"))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "BODY_INVALID")


if __name__ == "__main__":
    unittest.main()
