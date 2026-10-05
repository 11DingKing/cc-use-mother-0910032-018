"""抽样检验规则服务端。

模块划分：
* ``models``：规则版本、规则行（区间重叠检测）、不可变检验计划、人工覆盖
* ``sampling_tables``：样本量字码表
* ``store``：内存仓储与只追加审计事件流
* ``service``：用例编排（时态解析、收货冻结、覆盖审批、批次拆分）
* ``api``：标准库 HTTP 接口
"""
from .errors import (
    ConflictError,
    ImmutablePlanError,
    NotFoundError,
    ServiceError,
    ValidationError,
)
from .models import DefectClass, InspectionPlan, Override, RuleRow, RuleVersion, build_plan
from .service import InspectionService
from .store import Repository

__all__ = [
    "InspectionService",
    "Repository",
    "RuleVersion",
    "RuleRow",
    "InspectionPlan",
    "Override",
    "build_plan",
    "ServiceError",
    "NotFoundError",
    "ConflictError",
    "ValidationError",
    "ImmutablePlanError",
]
