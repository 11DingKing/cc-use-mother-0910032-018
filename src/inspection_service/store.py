"""内存仓储与审计事件流。

不依赖外部数据库：所有聚合按 ID 存放于字典，审计事件只追加。
生产环境可把 ``Repository`` 换成数据库实现而不动领域层。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from itertools import count

from .errors import NotFoundError
from .models import InspectionPlan, Override, RuleVersion, iso


@dataclass
class AuditEvent:
    seq: int
    at: datetime
    actor: str
    action: str
    target_type: str
    target_id: str
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "at": iso(self.at),
            "actor": self.actor,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "detail": self.detail,
        }


class Repository:
    def __init__(self) -> None:
        self.rule_versions: dict[str, RuleVersion] = {}
        self.plans: dict[str, InspectionPlan] = {}
        self.overrides: dict[str, Override] = {}
        # (material_category, supplier_grade) -> [version_id]
        self._key_index: dict[tuple[str, str], list[str]] = defaultdict(list)
        # batch_no -> [plan_id]（同批次允许因拆分出现多计划）
        self._batch_index: dict[str, list[str]] = defaultdict(list)
        self._events: list[AuditEvent] = []
        self._seq = count(1)

    # -- 规则版本 ----------------------------------------------------------

    def add_rule_version(self, version: RuleVersion) -> None:
        if version.version_id in self.rule_versions:
            raise NotFoundError("版本 ID 冲突")  # 内部错误，理论不会发生
        self.rule_versions[version.version_id] = version
        key = (version.material_category, version.supplier_grade)
        self._key_index[key].append(version.version_id)

    def get_rule_version(self, version_id: str) -> RuleVersion:
        try:
            return self.rule_versions[version_id]
        except KeyError:
            raise NotFoundError("规则版本不存在", details={"version_id": version_id}) from None

    def versions_for_key(self, material_category: str, supplier_grade: str) -> list[RuleVersion]:
        ids = self._key_index.get((material_category, supplier_grade), [])
        return [self.rule_versions[i] for i in ids]

    def all_rule_versions(self) -> list[RuleVersion]:
        return list(self.rule_versions.values())

    # -- 检验计划 ----------------------------------------------------------

    def add_plan(self, plan: InspectionPlan) -> None:
        self.plans[plan.plan_id] = plan
        self._batch_index[plan.batch_no].append(plan.plan_id)

    def get_plan(self, plan_id: str) -> InspectionPlan:
        try:
            return self.plans[plan_id]
        except KeyError:
            raise NotFoundError("检验计划不存在", details={"plan_id": plan_id}) from None

    def plans_for_batch(self, batch_no: str) -> list[InspectionPlan]:
        return [self.plans[i] for i in self._batch_index.get(batch_no, [])]

    def all_plans(self) -> list[InspectionPlan]:
        return list(self.plans.values())

    # -- 覆盖 --------------------------------------------------------------

    def add_override(self, override: Override) -> None:
        self.overrides[override.override_id] = override

    def get_override(self, override_id: str) -> Override:
        try:
            return self.overrides[override_id]
        except KeyError:
            raise NotFoundError("人工覆盖不存在", details={"override_id": override_id}) from None

    # -- 审计 --------------------------------------------------------------

    def record(
        self,
        *,
        at: datetime,
        actor: str,
        action: str,
        target_type: str,
        target_id: str,
        detail: dict | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            seq=next(self._seq),
            at=at,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail or {},
        )
        self._events.append(event)
        return event

    def audit_trail(self, target_type: str | None = None, target_id: str | None = None) -> list[AuditEvent]:
        events = self._events
        if target_type:
            events = [e for e in events if e.target_type == target_type]
        if target_id:
            events = [e for e in events if e.target_id == target_id]
        return list(events)
