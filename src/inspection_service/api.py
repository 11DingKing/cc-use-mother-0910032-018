"""HTTP API（标准库实现，无第三方依赖）。

鉴权约定：请求头 ``X-Actor`` 为操作人，``X-Role`` 为角色。
* 规则版本写操作、覆盖批准：仅质量工程师
* 收货、开始/完成检验、批次拆分：仓储管理员
* 覆盖申请：仓储管理员发起，质量工程师批准（申请-批准职责分离）

启动：``python3 -m inspection_service.api --port 8000``
"""
from __future__ import annotations

import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import ServiceError
from .models import _parse_ts  # noqa: PLC2701 内部复用时间解析
from .service import InspectionService
from .states import normalize_role

ROLE_QE = "质量工程师"
ROLE_WH = "仓储管理员"


def _parse_body(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ServiceError("请求体不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ServiceError("请求体必须是 JSON 对象")
    return value


def _ts(body: dict, key: str = "received_at") -> datetime | None:
    value = body.pop(key, None)
    return _parse_ts(value) if value else None


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "InspectionService/0.1"

    # 由 ThreadingHTTPServer 注入
    service: InspectionService

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    # -- 框架 --------------------------------------------------------------

    def _send(self, status: int, payload: dict | list) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _actor(self) -> tuple[str, str | None]:
        # HTTP 头只能是 latin-1，中文名按 RFC 3986 percent-encode 后放入 X-Actor/X-Role
        actor = unquote(self.headers.get("X-Actor", "")).strip()
        role = normalize_role(unquote(self.headers.get("X-Role", "")))
        if not actor:
            from .errors import UnauthorizedError

            raise UnauthorizedError("缺少 X-Actor 请求头")
        return actor, role

    def _require_role(self, expected: str) -> str:
        actor, actual = self._actor()
        if actual != expected:
            from .errors import UnauthorizedError

            raise UnauthorizedError(
                f"该操作仅 {expected} 可执行",
                details={"required_role": expected, "actual_role": actual},
            )
        return actor

    def _handle(self, fn) -> None:
        try:
            status, payload = fn()
            self._send(status, payload)
        except ServiceError as exc:
            self._send(exc.http_status, exc.to_dict())
        except (KeyError, ValueError, TypeError) as exc:
            self._send(422, {"error": "validation_error", "message": f"请求字段缺失或类型错误：{exc}"})
        except Exception as exc:  # 兜底，保证输出结构化错误
            self._send(500, {"error": "internal_error", "message": str(exc)})

    # -- 路由 --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        parts = path.split("/") if path else []
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}

        def route():
            if parts == ["api", "rule-versions"]:
                return 200, [v.to_dict() for v in self.service.repo.all_rule_versions()]
            if len(parts) == 3 and parts[:2] == ["api", "rule-versions"]:
                return 200, self.service.repo.get_rule_version(parts[2]).to_dict()
            if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2] != "":
                return 200, self.service.repo.get_plan(parts[2]).to_dict()
            if len(parts) == 4 and parts[:2] == ["api", "plans"] and parts[3] == "explanation":
                return 200, self.service.explain_plan(parts[2])
            if parts == ["api", "batches"] and "batch_no" in query:
                return 200, self.service.batch_history(query["batch_no"])
            if parts == ["api", "audit"]:
                return 200, [e.to_dict() for e in self.service.repo.audit_trail()]
            if parts in (["api", "health"], ["health"]):
                return 200, {"status": "ok"}
            from .errors import NotFoundError

            raise NotFoundError("未知接口", details={"path": self.path})

        self._handle(route)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/") if path else []
        body = _parse_body(self)

        def route():
            svc = self.service

            if parts == ["api", "rule-versions"]:
                actor = self._require_role(ROLE_QE)
                version = svc.create_rule_version(
                    actor=actor,
                    material_category=body["material_category"],
                    supplier_grade=body["supplier_grade"],
                    rows=body["rows"],
                    defect_classes=body.get("defect_classes"),
                    change_summary=body.get("change_summary", ""),
                    now=_ts(body, "created_at"),
                )
                return 201, version.to_dict()

            if len(parts) == 3 and parts[:2] == ["api", "rule-versions"] and parts[2] != "":
                action = body.get("action")
                vid = parts[2]
                actor = self._require_role(ROLE_QE)
                if action == "submit":
                    return 200, svc.submit_rule_version(actor=actor, version_id=vid, now=_ts(body, "at")).to_dict()
                if action == "issue":
                    return 200, svc.issue_rule_version(actor=actor, version_id=vid, now=_ts(body, "at")).to_dict()
                if action == "close":
                    return 200, svc.close_rule_version(actor=actor, version_id=vid, now=_ts(body, "at")).to_dict()
                from .errors import ValidationError

                raise ValidationError("action 必须是 submit/issue/close", details={"action": action})

            if parts == ["api", "goods-receipts"]:
                actor = self._require_role(ROLE_WH)
                plan = svc.receive_goods(
                    actor=actor,
                    batch_no=body["batch_no"],
                    material_category=body["material_category"],
                    supplier_grade=body["supplier_grade"],
                    supplier_id=body["supplier_id"],
                    lot_qty=int(body["lot_qty"]),
                    received_at=_ts(body),
                    parent_plan_id=body.get("parent_plan_id"),
                )
                return 201, plan.to_dict()

            if len(parts) == 3 and parts[:2] == ["api", "plans"] and parts[2] != "":
                action = body.get("action")
                pid = parts[2]
                if action == "start":
                    actor = self._require_role(ROLE_WH)
                    return 200, svc.start_plan(actor=actor, plan_id=pid, now=_ts(body, "at")).to_dict()
                if action == "complete":
                    actor = self._require_role(ROLE_WH)
                    plan = svc.record_result(
                        actor=actor, plan_id=pid,
                        inspected_count=int(body["inspected_count"]),
                        defect_count=int(body["defect_count"]) if "defect_count" in body else None,
                        defect_grades=body.get("defect_grades", []),
                        defect_counts_by_class=body.get("defect_counts_by_class"),
                        disposition=body["disposition"],
                        now=_ts(body, "at"),
                    )
                    return 200, plan.to_dict()
                if action == "split":
                    actor = self._require_role(ROLE_WH)
                    children = svc.split_batch(
                        actor=actor, parent_plan_id=pid,
                        child_lots=[int(x) for x in body["child_lots"]],
                        now=_ts(body, "at"),
                    )
                    return 201, [p.to_dict() for p in children]
                if action == "request-tightening":
                    actor = self._require_role(ROLE_WH)
                    ov = svc.request_override(
                        actor=actor, plan_id=pid, kind="emergency_tightening",
                        reason=body["reason"],
                        multiply=float(body.get("multiply", 1.0)),
                        add=int(body.get("add", 0)),
                        now=_ts(body, "at"),
                    )
                    return 201, ov.to_dict()
                if action == "request-exemption":
                    actor = self._require_role(ROLE_WH)
                    ov = svc.request_override(
                        actor=actor, plan_id=pid, kind="exemption",
                        reason=body["reason"], now=_ts(body, "at"),
                    )
                    return 201, ov.to_dict()
                from .errors import ValidationError

                raise ValidationError(
                    "action 必须是 start/complete/split/request-tightening/request-exemption",
                    details={"action": action},
                )

            if len(parts) == 3 and parts[:2] == ["api", "overrides"] and parts[2] != "":
                action = body.get("action")
                oid = parts[2]
                if action == "approve":
                    actor = self._require_role(ROLE_QE)
                    return 200, svc.approve_override(
                        actor=actor, override_id=oid,
                        comment=body.get("comment", ""), now=_ts(body, "at"),
                    ).to_dict()
                if action == "revoke":
                    actor = self._require_role(ROLE_QE)
                    return 200, svc.revoke_override(
                        actor=actor, override_id=oid,
                        reason=body["reason"], now=_ts(body, "at"),
                    ).to_dict()
                from .errors import ValidationError

                raise ValidationError("action 必须是 approve/revoke", details={"action": action})

            from .errors import NotFoundError

            raise NotFoundError("未知接口", details={"path": self.path})

        self._handle(route)


def build_server(port: int = 8000, service: InspectionService | None = None) -> ThreadingHTTPServer:
    svc = service or InspectionService()

    class _Server(ThreadingHTTPServer):
        pass

    handler = ApiHandler
    handler.service = svc  # type: ignore[attr-defined]
    server = _Server(("0.0.0.0", port), handler)
    server.service = svc  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="抽样检验规则服务")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--seed", action="store_true", help="启动时写入演示种子数据")
    args = parser.parse_args(argv)

    server = build_server(args.port)
    if args.seed:
        from .seed import seed_demo

        seed_demo(server.service)  # type: ignore[attr-defined]
        print("已写入演示种子数据")
    print(f"抽样检验服务监听 http://0.0.0.0:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
