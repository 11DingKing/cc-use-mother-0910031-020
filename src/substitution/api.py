"""基于标准库 ``http.server`` 的 HTTP/JSON 接口。

所有业务命令都返回 200，并用响应体的 ``accepted`` 与逐项 ``items``
表达接受/拒绝原因；仅在路由不存在(404)或 JSON 非法(400)时使用非 200。

路由（均以 JSON 作为请求体/响应体）：

    POST   /shortages                       登记短缺事件
    GET    /shortages/{id}
    POST   /candidates                      登记候选替代（含适用车型矩阵）
    GET    /candidates[?state=...]
    GET    /candidates/{id}
    POST   /candidates/{id}/applicability   局部适用矩阵变更（触发重评）
    POST   /candidates/{id}/trial           试算替代后需求/竞争/风险
    POST   /candidates/{id}/submit
    POST   /candidates/{id}/release
    POST   /candidates/{id}/effectuate      生效并冻结依据
    POST   /candidates/{id}/complete
    POST   /candidates/{id}/sign            {"role","signer"}
    POST   /candidates/{id}/withdraw
    POST   /candidates/{id}/evidence        {"kind","doc_id","note"}
    POST   /candidates/{id}/evidence/confirm {"kind","value"}
    POST   /candidates/{id}/warnings/resolve {"code","by","note"}
    POST   /quorum-rules                    {"rule_id","required_counts","min_roles"}
    POST   /stock/on-hand                   {"material","qty"}
    POST   /stock/inbound                   {"material","inbound_id","qty"}
    POST   /stock/inbound/remove            {"material","inbound_id"}
    GET    /health
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .service import Outcome, Service


class _Handler(BaseHTTPRequestHandler):
    service: Service  # 由 make_server 注入到类上

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静日志
        return

    # ---- 基础收发 ----
    def _send(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("body 必须是 JSON 对象")
            return value
        except (ValueError, json.JSONDecodeError) as exc:
            self._send({"accepted": False, "error": "INVALID_JSON", "message": str(exc)}, 400)
            return None

    # ---- 路由 ----
    def do_GET(self) -> None:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        query = parse_qs(parsed.query)
        if path == "health":
            self._send({"status": "ok"})
            return
        if path.startswith("shortages/"):
            self._outcome(self.service.get_shortage(path.split("/", 1)[1]))
            return
        if path == "candidates":
            state = query.get("state", [None])[0]
            self._outcome(self.service.list_candidates(state))
            return
        if path.startswith("candidates/"):
            self._outcome(self.service.get_candidate(path.split("/", 1)[1]))
            return
        self._send({"error": "NOT_FOUND", "path": self.path}, 404)

    def do_POST(self) -> None:
        body = self._body()
        if body is None:
            return
        from urllib.parse import urlparse
        path = urlparse(self.path).path.strip("/")
        parts = path.split("/")
        svc = self.service

        if path == "shortages":
            self._outcome(svc.register_shortage(
                body.get("shortage_id", ""), body.get("material", ""),
                body.get("plant", ""), body.get("raised_by", ""),
                body.get("demand", [])))
            return
        if path == "candidates":
            self._outcome(svc.register_candidate(
                body.get("candidate_id", ""), body.get("shortage_id", ""),
                body.get("substitute_material", ""), body.get("applicability", []),
                float(body.get("default_ratio", 1.0)),
                body.get("quorum_rule_id", "standard")))
            return
        if path == "stock/on-hand":
            self._outcome(svc.set_on_hand(body.get("material", ""), body.get("qty")))
            return
        if path == "stock/inbound":
            self._outcome(svc.add_inbound(body.get("material", ""),
                                          body.get("inbound_id", ""), body.get("qty")))
            return
        if path == "stock/inbound/remove":
            self._outcome(svc.remove_inbound(body.get("material", ""),
                                             body.get("inbound_id", "")))
            return
        if path == "quorum-rules":
            self._outcome(svc.add_quorum_rule(body.get("rule_id", ""),
                                              body.get("required_counts", {}),
                                              int(body.get("min_roles", 0))))
            return

        # /candidates/{id}/...
        if len(parts) >= 3 and parts[0] == "candidates":
            cid, action = parts[1], "/".join(parts[2:])
            simple: dict[str, Callable[[], Outcome]] = {
                "trial": lambda: svc.run_trial(cid),
                "submit": lambda: svc.submit(cid),
                "release": lambda: svc.release(cid),
                "abandon": lambda: svc.abandon_candidate(cid, str(body.get("reason", ""))),
                "effectuate": lambda: svc.effectuate(cid),
                "complete": lambda: svc.complete(cid, str(body.get("by", ""))),
                "applicability": lambda: svc.update_applicability(cid, body.get("applicability", [])),
                "sign": lambda: svc.sign(cid, body.get("role", ""), body.get("signer", "")),
                "withdraw": lambda: svc.withdraw_signature(cid, body.get("role", ""),
                                                            body.get("signer", "")),
                "evidence": lambda: svc.add_evidence_doc(cid, body.get("kind", ""),
                                                          body.get("doc_id", ""),
                                                          str(body.get("note", ""))),
                "evidence/confirm": lambda: svc.set_evidence_confirmation(
                    cid, body.get("kind", ""), bool(body.get("value"))),
                "warnings/resolve": lambda: svc.resolve_warning(
                    cid, body.get("code", ""), body.get("by", ""), str(body.get("note", ""))),
            }
            fn = simple.get(action)
            if fn is not None:
                self._outcome(fn())
                return

        self._send({"error": "NOT_FOUND", "path": self.path}, 404)

    def _outcome(self, outcome: Outcome) -> None:
        self._send(outcome.to_dict())


def make_server(host: str = "127.0.0.1", port: int = 8000,
                service: Service | None = None, snapshot_path: str | None = None) -> ThreadingHTTPServer:
    service = service or Service(snapshot_path=snapshot_path)
    handler = type("BoundHandler", (_Handler,), {"service": service})
    return ThreadingHTTPServer((host, port), handler)


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
