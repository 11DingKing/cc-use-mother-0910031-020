"""命令行启动短缺替代料审批后端服务。

用法：
    python3 tools/run_server.py --host 127.0.0.1 --port 8000 [--snapshot data/state.json]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from substitution.api import make_server  # noqa: E402
from substitution.service import Service  # noqa: E402


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="短缺替代料审批后端")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--snapshot", default=None, help="可选 JSON 快照持久化路径")
    args = parser.parse_args(argv)

    service = Service(snapshot_path=args.snapshot)
    service.store.load()
    httpd = make_server(args.host, args.port, service)
    print(f"短缺替代料审批服务已启动: http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
