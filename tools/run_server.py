"""启动抽样检验服务：python3 tools/run_server.py [--host 127.0.0.1] [--port 8080] [--data data/sampling_store.json]"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sampling_service.api import serve

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="抽样检验规则版本服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--data", default=str(ROOT / "data" / "sampling_store.json"),
                        help="JSON 数据文件路径；传空字符串则仅使用内存")
    args = parser.parse_args()
    serve(args.host, args.port, args.data or None)
