"""JSON 文件持久化存储。无外部依赖，写盘采用临时文件原子替换。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonStore:
    COLLECTIONS = ("versions", "plans", "batches", "tightenings", "exemptions")

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.data: dict[str, Any] = {"seq": {}, **{name: {} for name in self.COLLECTIONS}}
        if self.path and self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for name in self.COLLECTIONS:
                self.data[name].update(loaded.get(name, {}))
            self.data["seq"].update(loaded.get("seq", {}))

    def next_id(self, prefix: str) -> str:
        """生成单调递增的业务编号，如 RV-0001。"""
        n = int(self.data["seq"].get(prefix, 0)) + 1
        self.data["seq"][prefix] = n
        return f"{prefix}-{n:04d}"

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        tmp.replace(self.path)
