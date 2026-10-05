"""演示种子数据：电子元件类 × A 级供应商的一张抽样表。

区间覆盖 1..∞ 无缺口；判定数 Ac/Re 随版本固化（示例值，非标准符合性声明）。
"""
from __future__ import annotations

from .service import InspectionService

SEED_ROWS_V1 = [
    {"lot_min": 1, "lot_max": 8, "base_sample_size": 2, "inspection_level": "II",
     "accept_number": 0, "reject_number": 1},
    {"lot_min": 9, "lot_max": 15, "base_sample_size": 3, "inspection_level": "II",
     "accept_number": 0, "reject_number": 1},
    {"lot_min": 16, "lot_max": 25, "base_sample_size": 5, "inspection_level": "II",
     "accept_number": 0, "reject_number": 1},
    {"lot_min": 26, "lot_max": 50, "base_sample_size": 8, "inspection_level": "II",
     "accept_number": 0, "reject_number": 1},
    {"lot_min": 51, "lot_max": 90, "base_sample_size": 13, "inspection_level": "II",
     "accept_number": 1, "reject_number": 2},
    {"lot_min": 91, "lot_max": 150, "base_sample_size": 20, "inspection_level": "II",
     "accept_number": 1, "reject_number": 2},
    {"lot_min": 151, "lot_max": 280, "base_sample_size": 32, "inspection_level": "II",
     "accept_number": 2, "reject_number": 3},
    {"lot_min": 281, "lot_max": 500, "base_sample_size": 50, "inspection_level": "II",
     "accept_number": 3, "reject_number": 4},
    {"lot_min": 501, "lot_max": 1200, "base_sample_size": 80, "inspection_level": "II",
     "accept_number": 5, "reject_number": 6},
    {"lot_min": 1201, "lot_max": 3200, "base_sample_size": 125, "inspection_level": "II",
     "accept_number": 7, "reject_number": 8},
    {"lot_min": 3201, "lot_max": 10000, "base_sample_size": 200, "inspection_level": "II",
     "accept_number": 10, "reject_number": 11},
    {"lot_min": 10001, "lot_max": None, "base_sample_size": 315, "inspection_level": "II",
     "accept_number": 14, "reject_number": 15},
]


def seed_demo(service: InspectionService) -> dict:
    version = service.create_rule_version(
        actor="质量工程师-张敏",
        material_category="电子元件",
        supplier_grade="A",
        rows=SEED_ROWS_V1,
        change_summary="初始抽样表：按 GB/T 2828.1 一般检验水平 II",
    )
    service.submit_rule_version(actor="质量工程师-张敏", version_id=version.version_id)
    service.issue_rule_version(actor="质量工程师-张敏", version_id=version.version_id)
    return {"rule_version_id": version.version_id}
