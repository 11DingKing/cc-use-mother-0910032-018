"""基于标准库 http.server 的 REST API。

约定：
- 变更类请求需携带操作者与角色：优先取请求头 X-Actor-Name / X-Actor-Role，
  也可在 JSON 请求体中提供 "actor" / "role" 字段（中文角色名建议走请求体，避免头编码限制）；
- 错误响应统一为 {"error": {"code", "message", "details"}}。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import SamplingService
from .store import JsonStore


@dataclass
class Request:
    params: dict[str, str]
    body: dict[str, Any]
    query: dict[str, list[str]]
    actor: str
    role: str

    def q(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None


class SamplingAPI:
    """路由处理集合，每个方法返回 (HTTP 状态码, 响应体)。"""

    def __init__(self, service: SamplingService):
        self.service = service

    @staticmethod
    def _require_actor(req: Request) -> None:
        if not req.actor:
            raise ValidationError("缺少请求头 X-Actor-Name", code="ACTOR_REQUIRED")
        if not req.role:
            raise ValidationError("缺少请求头 X-Actor-Role", code="ROLE_REQUIRED")

    # ---- 基础 ----

    def health(self, req: Request):
        return 200, {"status": "ok"}

    # ---- 规则版本 ----

    def create_version(self, req: Request):
        self._require_actor(req)
        payload = self.service.create_rule_version(
            actor=req.actor, role=req.role,
            title=req.body.get("title"), rules=req.body.get("rules"),
        )
        return 201, payload

    def list_versions(self, req: Request):
        return 200, {"items": self.service.list_rule_versions(status=req.q("status"))}

    def get_version(self, req: Request):
        return 200, self.service.get_rule_version(req.params["version_id"])

    def submit_version(self, req: Request):
        self._require_actor(req)
        return 200, self.service.submit_rule_version(
            actor=req.actor, role=req.role, version_id=req.params["version_id"])

    def release_version(self, req: Request):
        self._require_actor(req)
        return 200, self.service.release_rule_version(
            actor=req.actor, role=req.role, version_id=req.params["version_id"])

    # ---- 收货与检验计划 ----

    def create_receipt(self, req: Request):
        self._require_actor(req)
        payload = self.service.create_receipt(
            actor=req.actor, role=req.role,
            batch_id=req.body.get("batch_id"),
            material_category=req.body.get("material_category"),
            supplier_level=req.body.get("supplier_level"),
            quantity=req.body.get("quantity"),
            received_at=req.body.get("received_at"),
        )
        return 201, payload

    def preview_plan(self, req: Request):
        self._require_actor(req)
        return 200, self.service.preview_plan(
            actor=req.actor, role=req.role,
            material_category=req.body.get("material_category"),
            supplier_level=req.body.get("supplier_level"),
            quantity=req.body.get("quantity"),
            at=req.body.get("at"),
        )

    def list_plans(self, req: Request):
        return 200, {"items": self.service.list_plans(batch_id=req.q("batch_id"), status=req.q("status"))}

    def get_plan(self, req: Request):
        return 200, self.service.get_plan(req.params["plan_id"])

    def explain_plan(self, req: Request):
        return 200, self.service.explain_plan(req.params["plan_id"])

    def plan_audit(self, req: Request):
        return 200, self.service.plan_audit(req.params["plan_id"])

    def start_plan(self, req: Request):
        self._require_actor(req)
        return 200, self.service.start_plan(actor=req.actor, role=req.role, plan_id=req.params["plan_id"])

    def complete_plan(self, req: Request):
        self._require_actor(req)
        return 200, self.service.complete_plan(
            actor=req.actor, role=req.role, plan_id=req.params["plan_id"], note=req.body.get("note"))

    def override_plan(self, req: Request):
        self._require_actor(req)
        return 200, self.service.override_plan(
            actor=req.actor, role=req.role, plan_id=req.params["plan_id"],
            new_sample_size=req.body.get("new_sample_size"), reason=req.body.get("reason"))

    def split_batch(self, req: Request):
        self._require_actor(req)
        return 201, self.service.split_batch(
            actor=req.actor, role=req.role, batch_id=req.params["batch_id"],
            quantities=req.body.get("quantities"))

    # ---- 紧急加严 ----

    def create_tightening(self, req: Request):
        self._require_actor(req)
        payload = self.service.create_tightening(
            actor=req.actor, role=req.role,
            material_category=req.body.get("material_category"),
            supplier_level=req.body.get("supplier_level"),
            multiplier=req.body.get("multiplier"),
            reason=req.body.get("reason"),
            expires_at=req.body.get("expires_at"),
        )
        return 201, payload

    def list_tightenings(self, req: Request):
        return 200, {"items": self.service.list_tightenings()}

    def revoke_tightening(self, req: Request):
        self._require_actor(req)
        return 200, self.service.revoke_tightening(
            actor=req.actor, role=req.role, tightening_id=req.params["tightening_id"])

    # ---- 豁免批准 ----

    def request_exemption(self, req: Request):
        self._require_actor(req)
        payload = self.service.request_exemption(
            actor=req.actor, role=req.role,
            material_category=req.body.get("material_category"),
            mode=req.body.get("mode"),
            reason=req.body.get("reason"),
            supplier_level=req.body.get("supplier_level"),
            batch_id=req.body.get("batch_id"),
            reduce_factor=req.body.get("reduce_factor"),
            expires_at=req.body.get("expires_at"),
        )
        return 201, payload

    def list_exemptions(self, req: Request):
        return 200, {"items": self.service.list_exemptions()}

    def approve_exemption(self, req: Request):
        self._require_actor(req)
        return 200, self.service.approve_exemption(
            actor=req.actor, role=req.role, exemption_id=req.params["exemption_id"])

    def reject_exemption(self, req: Request):
        self._require_actor(req)
        return 200, self.service.reject_exemption(
            actor=req.actor, role=req.role, exemption_id=req.params["exemption_id"])


ROUTES: list[tuple[str, str, str]] = [
    ("GET", r"/health", "health"),
    ("POST", r"/rule-versions", "create_version"),
    ("GET", r"/rule-versions", "list_versions"),
    ("GET", r"/rule-versions/(?P<version_id>[^/]+)", "get_version"),
    ("POST", r"/rule-versions/(?P<version_id>[^/]+)/submit", "submit_version"),
    ("POST", r"/rule-versions/(?P<version_id>[^/]+)/release", "release_version"),
    ("POST", r"/receipts", "create_receipt"),
    ("POST", r"/preview-plan", "preview_plan"),
    ("GET", r"/plans", "list_plans"),
    ("GET", r"/plans/(?P<plan_id>[^/]+)", "get_plan"),
    ("GET", r"/plans/(?P<plan_id>[^/]+)/explanation", "explain_plan"),
    ("GET", r"/plans/(?P<plan_id>[^/]+)/audit", "plan_audit"),
    ("POST", r"/plans/(?P<plan_id>[^/]+)/start", "start_plan"),
    ("POST", r"/plans/(?P<plan_id>[^/]+)/complete", "complete_plan"),
    ("POST", r"/plans/(?P<plan_id>[^/]+)/overrides", "override_plan"),
    ("POST", r"/batches/(?P<batch_id>[^/]+)/splits", "split_batch"),
    ("POST", r"/tightenings", "create_tightening"),
    ("GET", r"/tightenings", "list_tightenings"),
    ("POST", r"/tightenings/(?P<tightening_id>[^/]+)/revoke", "revoke_tightening"),
    ("POST", r"/exemptions", "request_exemption"),
    ("GET", r"/exemptions", "list_exemptions"),
    ("POST", r"/exemptions/(?P<exemption_id>[^/]+)/approve", "approve_exemption"),
    ("POST", r"/exemptions/(?P<exemption_id>[^/]+)/reject", "reject_exemption"),
]
COMPILED = [(method, re.compile(pattern), name) for method, pattern, name in ROUTES]


def make_server(service: SamplingService, host: str, port: int) -> ThreadingHTTPServer:
    app = SamplingAPI(service)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SamplingService/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # 保持测试输出干净
            pass

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValidationError("请求体不是合法 JSON", code="BODY_INVALID")
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是 JSON 对象", code="BODY_INVALID")
            return value

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _dispatch(self, method: str) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            try:
                for route_method, pattern, name in COMPILED:
                    if route_method != method:
                        continue
                    match = pattern.fullmatch(path)
                    if not match:
                        continue
                    body = self._read_body() if method == "POST" else {}
                    req = Request(
                        params=match.groupdict(),
                        body=body,
                        query=parse_qs(parsed.query),
                        actor=self.headers.get("X-Actor-Name") or str(body.get("actor", "")),
                        role=self.headers.get("X-Actor-Role") or str(body.get("role", "")),
                    )
                    status, payload = getattr(app, name)(req)
                    self._send(status, payload)
                    return
                self._send(404, {"error": {"code": "ROUTE_NOT_FOUND",
                                           "message": f"未匹配的路由：{method} {path}", "details": {}}})
            except DomainError as exc:
                self._send(exc.http_status, {"error": {
                    "code": exc.code, "message": exc.message, "details": exc.details}})
            except Exception as exc:  # noqa: BLE001 - 兜底，避免连接悬挂
                self._send(500, {"error": {"code": "INTERNAL", "message": str(exc), "details": {}}})

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def serve(host: str, port: int, data_path: str | None) -> None:
    service = SamplingService(JsonStore(data_path))
    server = make_server(service, host, port)
    print(f"抽样检验服务已启动：http://{host}:{server.server_address[1]}（数据文件：{data_path or '内存'}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
