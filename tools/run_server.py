"""启动短缺替代料审批 HTTP 服务。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitute_approval.http_api import build_server
from substitute_approval.service import SubstituteService

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="短缺替代料审批 HTTP 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = build_server(SubstituteService(), args.host, args.port)
    print(f"短缺替代料审批服务监听 http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
