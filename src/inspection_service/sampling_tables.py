"""抽样量字码表。

参照 GB/T 2828.1（等同 ISO 2859-1）的「样本量字码」与「样本量表」内置参考数据。
检验批是否合格的判定数（Ac/Re）由质量工程师在规则版本中显式维护并随版本固化，
服务只负责字码推导、时态解析与不可变留痕，不自行声称标准符合性。

表一旦被检验计划引用，计划会把字码、样本量与整张规则行一起快照，
此后即使内置表修订，历史计划的解释仍以快照为准。
"""
from __future__ import annotations

from .errors import ValidationError

TABLE_ID = "GB/T2828.1-样本量字码表-v1"

LEVELS = ("S-1", "S-2", "S-3", "S-4", "I", "II", "III")
DEFAULT_LEVEL = "II"

# (批量下限, 批量上限（None 表示开放区间）), 各检验水平对应字码
_CODE_LETTER_TABLE: list[tuple[int, int | None, dict[str, str]]] = [
    (2, 8, {"S-1": "A", "S-2": "A", "S-3": "A", "S-4": "A", "I": "A", "II": "A", "III": "B"}),
    (9, 15, {"S-1": "A", "S-2": "A", "S-3": "A", "S-4": "A", "I": "A", "II": "B", "III": "C"}),
    (16, 25, {"S-1": "A", "S-2": "A", "S-3": "B", "S-4": "B", "I": "B", "II": "C", "III": "D"}),
    (26, 50, {"S-1": "A", "S-2": "B", "S-3": "B", "S-4": "C", "I": "C", "II": "D", "III": "E"}),
    (51, 90, {"S-1": "B", "S-2": "B", "S-3": "C", "S-4": "C", "I": "C", "II": "E", "III": "F"}),
    (91, 150, {"S-1": "B", "S-2": "B", "S-3": "C", "S-4": "D", "I": "D", "II": "F", "III": "G"}),
    (151, 280, {"S-1": "B", "S-2": "C", "S-3": "D", "S-4": "E", "I": "E", "II": "G", "III": "H"}),
    (281, 500, {"S-1": "B", "S-2": "C", "S-3": "D", "S-4": "E", "I": "F", "II": "H", "III": "J"}),
    (501, 1200, {"S-1": "C", "S-2": "C", "S-3": "E", "S-4": "F", "I": "G", "II": "J", "III": "K"}),
    (1201, 3200, {"S-1": "C", "S-2": "D", "S-3": "E", "S-4": "G", "I": "H", "II": "K", "III": "L"}),
    (3201, 10000, {"S-1": "C", "S-2": "D", "S-3": "F", "S-4": "G", "I": "J", "II": "L", "III": "M"}),
    (10001, 35000, {"S-1": "C", "S-2": "D", "S-3": "F", "S-4": "H", "I": "K", "II": "M", "III": "N"}),
    (35001, 150000, {"S-1": "D", "S-2": "E", "S-3": "G", "S-4": "J", "I": "N", "II": "P", "III": "P"}),
    (150001, 500000, {"S-1": "D", "S-2": "E", "S-3": "G", "S-4": "J", "I": "P", "II": "P", "III": "Q"}),
    (500001, None, {"S-1": "D", "S-2": "E", "S-3": "G", "S-4": "K", "I": "P", "II": "Q", "III": "Q"}),
]

LETTER_SAMPLE_SIZE = {
    "A": 2, "B": 3, "C": 5, "D": 8, "E": 13, "F": 20, "G": 32, "H": 50,
    "J": 80, "K": 125, "L": 200, "M": 315, "N": 500, "P": 800, "Q": 1250, "R": 2000,
}


def resolve_code_letter(lot_qty: int, level: str) -> str:
    """按批量区间与检验水平返回样本量字码。"""
    if lot_qty is None or lot_qty < 1:
        raise ValidationError("批量必须为正整数", details={"lot_qty": lot_qty})
    if level not in LEVELS:
        raise ValidationError(f"不支持的检验水平：{level}", details={"supported": list(LEVELS)})
    if lot_qty < 2:
        # 单件批按首档处理
        return _CODE_LETTER_TABLE[0][2][level]
    for low, high, row in _CODE_LETTER_TABLE:
        if lot_qty >= low and (high is None or lot_qty <= high):
            return row[level]
    raise ValidationError("批量超出字码表覆盖范围", details={"lot_qty": lot_qty})


def sample_size_for_letter(letter: str) -> int:
    try:
        return LETTER_SAMPLE_SIZE[letter]
    except KeyError:
        raise ValidationError(f"未知样本量字码：{letter}") from None
