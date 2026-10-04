"""短缺替代料审批 HTTP 接口（仅标准库）。

所有接口统一返回 {"accepted": bool, "items": [...], "data": {...}}，
items 逐项给出被接受或拒绝的原因；资源不存在返回 404，请求格式错误返回 400。
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .service import ServiceResult, SubstituteService

_BAD_JSON = {
    "accepted": False,
    "items": [
        {"item": "请求格式", "accepted": False, "blocking": True, "reasons": ["请求体不是合法 JSON 对象"]}
    ],
    "data": {},
}


def _payload(result: ServiceResult) -> tuple[int, dict]:
    status = 200
    if not result.accepted and any(i.item == "对象存在性" for i in result.items):
        status = 404
    return status, result.to_dict()


def _require(body: dict, keys: list[str]) -> dict | None:
    missing = [k for k in keys if k not in body]
    if not missing:
        return None
    return {
        "accepted": False,
        "items": [
            {
                "item": "请求参数",
                "accepted": False,
                "blocking": True,
                "reasons": [f"缺少参数 {k}" for k in missing],
            }
        ],
        "data": {},
    }


def _h_create_event(service, match, body):
    if err := _require(body, ["part_number", "demand_by_model", "needed_by", "actor", "role"]):
        return 400, err
    return _payload(
        service.register_event(
            part_number=body["part_number"],
            demand_by_model=body["demand_by_model"],
            needed_by=body["needed_by"],
            customers=tuple(body.get("customers", [])),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_list_events(service, match, body):
    return _payload(service.list_events())


def _h_get_event(service, match, body):
    return _payload(service.get_event(match.group("eid")))


def _h_add_candidate(service, match, body):
    if err := _require(body, ["substitute_part", "actor", "role"]):
        return 400, err
    return _payload(
        service.add_candidate(
            event_id=match.group("eid"),
            substitute_part=body["substitute_part"],
            ratio=body.get("ratio", 1.0),
            applicable_models=tuple(body.get("applicable_models", [])),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_submit(service, match, body):
    if err := _require(body, ["actor", "role"]):
        return 400, err
    return _payload(service.submit(event_id=match.group("eid"), actor=body["actor"], role=body["role"]))


def _h_evaluate(service, match, body):
    return _payload(service.evaluate(event_id=match.group("eid")))


def _h_get_evaluation(service, match, body):
    return _payload(service.get_evaluation(match.group("eid")))


def _h_publish(service, match, body):
    if err := _require(body, ["candidate_ids", "actor", "role"]):
        return 400, err
    return _payload(
        service.publish(
            event_id=match.group("eid"),
            candidate_ids=list(body["candidate_ids"]),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_get_basis(service, match, body):
    return _payload(service.get_basis(match.group("eid")))


def _h_sign(service, match, body):
    if err := _require(body, ["signer", "role"]):
        return 400, err
    return _payload(service.sign(event_id=match.group("eid"), signer=body["signer"], role=body["role"]))


def _h_withdraw(service, match, body):
    if err := _require(body, ["signer", "actor", "role"]):
        return 400, err
    return _payload(
        service.withdraw_signature(
            event_id=match.group("eid"),
            signer=body["signer"],
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_authorize_issue(service, match, body):
    if err := _require(body, ["candidate_id", "qty"]):
        return 400, err
    return _payload(
        service.authorize_issue(
            event_id=match.group("eid"), candidate_id=body["candidate_id"], qty=body["qty"]
        )
    )


def _h_record_issue(service, match, body):
    if err := _require(body, ["candidate_id", "qty", "actor", "role"]):
        return 400, err
    return _payload(
        service.record_issue(
            event_id=match.group("eid"),
            candidate_id=body["candidate_id"],
            qty=body["qty"],
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_close(service, match, body):
    if err := _require(body, ["actor", "role"]):
        return 400, err
    return _payload(service.close(event_id=match.group("eid"), actor=body["actor"], role=body["role"]))


def _h_set_fit(service, match, body):
    if err := _require(body, ["fit", "actor", "role"]):
        return 400, err
    return _payload(
        service.set_engineering_fit(
            candidate_id=match.group("cid"), fit=body["fit"], actor=body["actor"], role=body["role"]
        )
    )


def _h_set_supply(service, match, body):
    if err := _require(body, ["qty", "actor", "role"]):
        return 400, err
    return _payload(
        service.set_supply(
            candidate_id=match.group("cid"), qty=body["qty"], actor=body["actor"], role=body["role"]
        )
    )


def _h_set_applicability(service, match, body):
    if err := _require(body, ["applicable_models", "actor", "role"]):
        return 400, err
    return _payload(
        service.update_applicability(
            candidate_id=match.group("cid"),
            applicable_models=tuple(body["applicable_models"]),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_add_evidence(service, match, body):
    if err := _require(body, ["kind", "reference", "actor", "role"]):
        return 400, err
    return _payload(
        service.add_evidence(
            candidate_id=match.group("cid"),
            kind=body["kind"],
            reference=body["reference"],
            vehicle_models=tuple(body.get("vehicle_models", [])),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_invalidate_evidence(service, match, body):
    if err := _require(body, ["actor", "role"]):
        return 400, err
    return _payload(
        service.invalidate_evidence(
            evidence_id=match.group("evid"), actor=body["actor"], role=body["role"]
        )
    )


def _h_set_inventory(service, match, body):
    if err := _require(body, ["part", "qty", "actor", "role"]):
        return 400, err
    return _payload(
        service.set_inventory(part=body["part"], qty=body["qty"], actor=body["actor"], role=body["role"])
    )


def _h_set_restriction(service, match, body):
    if err := _require(body, ["customer", "vehicle_model", "original_part", "banned_substitutes", "actor", "role"]):
        return 400, err
    return _payload(
        service.set_customer_restriction(
            customer=body["customer"],
            vehicle_model=body["vehicle_model"],
            original_part=body["original_part"],
            banned_substitutes=tuple(body["banned_substitutes"]),
            note=body.get("note", ""),
            actor=body["actor"],
            role=body["role"],
        )
    )


def _h_health(service, match, body):
    return 200, {"accepted": True, "items": [], "data": {"service": "短缺替代料审批"}}


ROUTES = [
    ("GET", re.compile(r"/health"), _h_health),
    ("POST", re.compile(r"/events"), _h_create_event),
    ("GET", re.compile(r"/events"), _h_list_events),
    ("GET", re.compile(r"/events/(?P<eid>[^/]+)"), _h_get_event),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/candidates"), _h_add_candidate),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/submit"), _h_submit),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/evaluate"), _h_evaluate),
    ("GET", re.compile(r"/events/(?P<eid>[^/]+)/evaluation"), _h_get_evaluation),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/publish"), _h_publish),
    ("GET", re.compile(r"/events/(?P<eid>[^/]+)/basis"), _h_get_basis),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/sign"), _h_sign),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/withdraw"), _h_withdraw),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/issue/authorize"), _h_authorize_issue),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/issue"), _h_record_issue),
    ("POST", re.compile(r"/events/(?P<eid>[^/]+)/close"), _h_close),
    ("POST", re.compile(r"/candidates/(?P<cid>[^/]+)/fit"), _h_set_fit),
    ("POST", re.compile(r"/candidates/(?P<cid>[^/]+)/supply"), _h_set_supply),
    ("POST", re.compile(r"/candidates/(?P<cid>[^/]+)/applicability"), _h_set_applicability),
    ("POST", re.compile(r"/candidates/(?P<cid>[^/]+)/evidence"), _h_add_evidence),
    ("POST", re.compile(r"/evidence/(?P<evid>[^/]+)/invalidate"), _h_invalidate_evidence),
    ("POST", re.compile(r"/inventory"), _h_set_inventory),
    ("POST", re.compile(r"/restrictions"), _h_set_restriction),
]


def dispatch(service: SubstituteService, method: str, path: str, body: dict) -> tuple[int, dict]:
    """按方法与路径分发请求，返回 (状态码, 响应体)。"""
    for route_method, pattern, handler in ROUTES:
        if route_method != method:
            continue
        match = pattern.fullmatch(path)
        if match:
            return handler(service, match, body)
    return 404, {
        "accepted": False,
        "items": [
            {
                "item": "路由",
                "accepted": False,
                "blocking": True,
                "reasons": [f"接口不存在：{method} {path}"],
            }
        ],
        "data": {},
    }


def make_handler(service: SubstituteService):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SubstituteApproval/1.0"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = {}
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._reply(400, _BAD_JSON)
                if not isinstance(body, dict):
                    return self._reply(400, _BAD_JSON)
            status, payload = dispatch(service, method, self.path.split("?", 1)[0], body)
            self._reply(status, payload)

        def _reply(self, status: int, payload: dict):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # 静默访问日志
            pass

    return Handler


def build_server(
    service: SubstituteService | None = None, host: str = "127.0.0.1", port: int = 8080
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(service or SubstituteService()))
